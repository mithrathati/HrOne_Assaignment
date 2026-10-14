"""
Employee Attendance & Analytics API
Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import os
import calendar
import re
from datetime import datetime, timedelta, timezone, date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, StrictStr, field_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

EPOCH_MS_MIN = 100_000_000_000
EPOCH_MS_MAX = 4_102_444_800_000

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    seconds = ms // 1000
    return datetime.fromtimestamp(seconds, tz=UTC)


def dt_to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def truncate_to_seconds(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def ist_of(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(IST)


# --------------------------------------------------------------------------- #
# Half-up rounding
# --------------------------------------------------------------------------- #
def round_half_up(value: float, decimals: int = 2) -> float:
    if value is None:
        return None
    q = Decimal(10) ** -decimals
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Business rules
# --------------------------------------------------------------------------- #
def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start


def parse_hhmm(t: str):
    h, m = t.split(":")
    return int(h), int(m)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: compute the IST calendar attendance date for a punch-in instant."""
    ist = ist_of(punch_in)
    if is_overnight(shift_start, shift_end):
        eh, em = parse_hhmm(shift_end)
        ist_minutes = ist.hour * 60 + ist.minute
        end_minutes = eh * 60 + em
        if ist_minutes < end_minutes:
            return (ist.date() - timedelta(days=1)).isoformat()
    return ist.date().isoformat()


def shift_start_dt(att_date: str, shift_start: str) -> datetime:
    d = datetime.strptime(att_date, "%Y-%m-%d").date()
    sh, sm = parse_hhmm(shift_start)
    return datetime(d.year, d.month, d.day, sh, sm, 0, tzinfo=IST).astimezone(UTC)


def shift_end_dt(att_date: str, shift_start: str, shift_end: str) -> datetime:
    d = datetime.strptime(att_date, "%Y-%m-%d").date()
    eh, em = parse_hhmm(shift_end)
    if is_overnight(shift_start, shift_end):
        d = d + timedelta(days=1)
    return datetime(d.year, d.month, d.day, eh, em, 0, tzinfo=IST).astimezone(UTC)


def compute_late_minutes(punch_in: datetime, att_date: str, shift_start: str) -> int:
    """R2: late only if strictly more than 10 min after shift_start."""
    start = shift_start_dt(att_date, shift_start)
    diff_seconds = (punch_in - start).total_seconds()
    if diff_seconds > 600:
        return int(diff_seconds // 60)
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> Optional[float]:
    """R4: (punch_out - punch_in) hours, 2 decimals half-up."""
    seconds = (punch_out - punch_in).total_seconds()
    wh = Decimal(str(seconds)) / Decimal(3600)
    return float(wh.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, att_date: str, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes from shift_end to punch_out, only if >= 30."""
    end = shift_end_dt(att_date, shift_start, shift_end)
    diff_minutes = (punch_out - end).total_seconds() / 60
    if diff_minutes >= 30:
        return int(diff_minutes)
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: half_day when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def compute_derived(punch_in, punch_out, att_date, shift_start, shift_end):
    late = compute_late_minutes(punch_in, att_date, shift_start) if punch_in else 0
    if punch_in and punch_out:
        wh = compute_work_hours(punch_in, punch_out)
        ot = compute_overtime(punch_out, att_date, shift_start, shift_end)
        hd = compute_half_day(wh)
    else:
        wh = None
        ot = 0
        hd = False
    return wh, late, ot, hd


# --------------------------------------------------------------------------- #
# Calendar helpers
# --------------------------------------------------------------------------- #
def month_bounds(month: str):
    y, m = map(int, month.split("-"))
    _, last = calendar.monthrange(y, m)
    return f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{last:02d}"


def count_working_days(month: str, joined_on: Optional[str] = None) -> int:
    y, m = map(int, month.split("-"))
    _, last = calendar.monthrange(y, m)
    count = 0
    join_date = date.fromisoformat(joined_on) if joined_on else None
    for day in range(1, last + 1):
        d = date(y, m, day)
        if d.weekday() < 5:
            if join_date is None or d >= join_date:
                count += 1
    return count


def valid_date(s: str) -> bool:
    if not DATE_RE.match(s):
        return False
    try:
        date.fromisoformat(s)
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# Indexes
# --------------------------------------------------------------------------- #
def ensure_indexes():
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    db.employees.create_index([("joined_on", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", 1)])
    db.attendance_logs.create_index([("date", 1)])


@app.on_event("startup")
def startup():
    ensure_indexes()


# --------------------------------------------------------------------------- #
# Pydantic models
# --------------------------------------------------------------------------- #
class EpochMs(StrictInt):
    pass


class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v):
        if not valid_date(v):
            raise ValueError("invalid date")
        return v


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, v):
        if v is not None and not (EPOCH_MS_MIN <= v <= EPOCH_MS_MAX):
            raise ValueError("out of range")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v not in PRESENCE_STATUSES:
            raise ValueError("invalid status")
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, v):
        if v is not None and not (EPOCH_MS_MIN <= v <= EPOCH_MS_MAX):
            raise ValueError("out of range")
        return v


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_epoch(cls, v):
        if v is not None and not (EPOCH_MS_MIN <= v <= EPOCH_MS_MAX):
            raise ValueError("out of range")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v is not None and v not in ALL_STATUSES:
            raise ValueError("invalid status")
        return v


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #
def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]),
    }


def serialize_history(history: list) -> list:
    out = []
    for h in history:
        changes = {}
        for field_name, change in h.get("changes", {}).items():
            frm = change.get("from")
            to = change.get("to")
            if field_name in ("punch_in", "punch_out"):
                if isinstance(frm, datetime):
                    frm = dt_to_ms(frm)
                if isinstance(to, datetime):
                    to = dt_to_ms(to)
            changes[field_name] = {"from": frm, "to": to}
        out.append({
            "at": dt_to_ms(h["at"]),
            "by": h["by"],
            "reason": h["reason"],
            "changes": changes,
        })
    return out


def serialize_attendance(doc: dict) -> dict:
    pi = doc.get("punch_in")
    po = doc.get("punch_out")
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(pi) if isinstance(pi, datetime) else pi,
        "punch_out": dt_to_ms(po) if isinstance(po, datetime) else po,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": serialize_history(doc.get("history", [])),
    }


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        return JSONResponse(status_code=503, content={"detail": "mongodb unavailable"})


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start must differ from shift_end")
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [serialize_employee(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Punch-in
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(404, "unknown employee")
    punched_at = ms_to_dt(body.punched_at) if body.punched_at else now_utc()
    punched_at = truncate_to_seconds(punched_at)
    att_date = attendance_date(punched_at, emp["shift_start"], emp["shift_end"])
    late = compute_late_minutes(punched_at, att_date, emp["shift_start"])
    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


# --------------------------------------------------------------------------- #
# Punch-out
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(404, "unknown employee")
    punched_at = ms_to_dt(body.punched_at) if body.punched_at else now_utc()
    punched_at = truncate_to_seconds(punched_at)

    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}},
        sort=[("punch_in", -1)],
    )
    if not record:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    pi = record["punch_in"]
    if punched_at <= pi:
        raise HTTPException(422, "punched_at must be after punch_in")
    if (punched_at - pi).total_seconds() > 24 * 3600:
        raise HTTPException(422, "punched_at more than 24 hours after punch_in")

    wh, late, ot, hd = compute_derived(pi, punched_at, record["date"], emp["shift_start"], emp["shift_end"])

    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": punched_at,
            "work_hours": wh,
            "overtime_minutes": ot,
            "half_day": hd,
        }},
        return_document=ReturnDocument.AFTER,
    )
    if result is None:
        raise HTTPException(409, "already punched out")
    return serialize_attendance(result)


# --------------------------------------------------------------------------- #
# List attendance
# --------------------------------------------------------------------------- #
@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and not valid_date(date_from):
        raise HTTPException(422, "invalid date_from")
    if date_to and not valid_date(date_to):
        raise HTTPException(422, "invalid date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    if status and status not in ALL_STATUSES:
        raise HTTPException(422, "invalid status")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    date_filter = {}
    if date_from:
        date_filter["$gte"] = date_from
    if date_to:
        date_filter["$lte"] = date_to
    if date_filter:
        q["date"] = date_filter
    if status:
        q["status"] = status

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_attendance(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Regularize (PATCH)
# --------------------------------------------------------------------------- #
@app.patch("/attendance/{emp_code}/{date}")
def regularize(
    emp_code: str,
    date: str,
    body: RegularizeRequest,
):
    if not valid_date(date):
        raise HTTPException(422, "invalid date")
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(404, "unknown employee")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "no attendance record for this date")

    new_status = body.status if body.status is not None else record["status"]
    new_punch_in_ms = body.punch_in
    new_punch_out_ms = body.punch_out

    is_absence = new_status in ("ABSENT", "LEAVE")

    if is_absence:
        if new_punch_in_ms is not None or new_punch_out_ms is not None:
            raise HTTPException(422, "punch times must not be supplied for ABSENT/LEAVE")
        final_punch_in = None
        final_punch_out = None
    else:
        final_punch_in_ms = new_punch_in_ms if new_punch_in_ms is not None else (
            dt_to_ms(record["punch_in"]) if isinstance(record.get("punch_in"), datetime) else None
        )
        final_punch_out_ms = new_punch_out_ms if new_punch_out_ms is not None else (
            dt_to_ms(record["punch_out"]) if isinstance(record.get("punch_out"), datetime) else None
        )
        if final_punch_in_ms is None:
            raise HTTPException(422, "presence status requires a punch_in")
        final_punch_in = ms_to_dt(final_punch_in_ms)
        if final_punch_out_ms is not None:
            final_punch_out = ms_to_dt(final_punch_out_ms)
        else:
            final_punch_out = None

        if new_punch_in_ms is not None:
            computed_date = attendance_date(final_punch_in, emp["shift_start"], emp["shift_end"])
            if computed_date != date:
                raise HTTPException(422, "punch_in does not fall on the record's attendance date")

        if final_punch_out is not None:
            if final_punch_out <= final_punch_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (final_punch_out - final_punch_in).total_seconds() > 24 * 3600:
                raise HTTPException(422, "punch_out more than 24 hours after punch_in")

    wh, late, ot, hd = compute_derived(
        final_punch_in, final_punch_out, date, emp["shift_start"], emp["shift_end"]
    )

    old_pi = record.get("punch_in")
    old_po = record.get("punch_out")
    old_wh = record.get("work_hours")
    old_late = record.get("late_minutes", 0)
    old_ot = record.get("overtime_minutes", 0)
    old_hd = record.get("half_day", False)
    old_status = record["status"]

    changes = {}
    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if _dt_changed(old_pi, final_punch_in):
        changes["punch_in"] = {"from": dt_to_ms(old_pi) if isinstance(old_pi, datetime) else None, "to": dt_to_ms(final_punch_in) if isinstance(final_punch_in, datetime) else None}
    if _dt_changed(old_po, final_punch_out):
        changes["punch_out"] = {"from": dt_to_ms(old_po) if isinstance(old_po, datetime) else None, "to": dt_to_ms(final_punch_out) if isinstance(final_punch_out, datetime) else None}
    if wh != old_wh:
        changes["work_hours"] = {"from": old_wh, "to": wh}
    if late != old_late:
        changes["late_minutes"] = {"from": old_late, "to": late}
    if ot != old_ot:
        changes["overtime_minutes"] = {"from": old_ot, "to": ot}
    if hd != old_hd:
        changes["half_day"] = {"from": old_hd, "to": hd}

    if not changes:
        raise HTTPException(422, "no changes to apply")

    history_len = len(record.get("history", []))
    entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": _store_changes(changes),
    }

    update = {"$set": {
        "status": new_status,
        "punch_in": final_punch_in,
        "punch_out": final_punch_out,
        "work_hours": wh,
        "late_minutes": late,
        "overtime_minutes": ot,
        "half_day": hd,
    }, "$push": {"history": entry}}

    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, history_len]}},
        update,
        return_document=ReturnDocument.AFTER,
    )
    if result is None:
        raise HTTPException(409, "concurrent modification")
    return serialize_attendance(result)


def _dt_changed(a, b) -> bool:
    if isinstance(a, datetime) and isinstance(b, datetime):
        return a != b
    return a != b


def _store_changes(changes: dict) -> dict:
    stored = {}
    for field_name, change in changes.items():
        frm = change["from"]
        to = change["to"]
        if field_name in ("punch_in", "punch_out"):
            if isinstance(frm, int):
                frm = ms_to_dt(frm)
            if isinstance(to, int):
                to = ms_to_dt(to)
        stored[field_name] = {"from": frm, "to": to}
    return stored


# --------------------------------------------------------------------------- #
# Analytics - employee monthly
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(404, "unknown employee")
    month_start, month_end = month_bounds(month)

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
        {"$addFields": {
            "half_day": {"$ifNull": ["$half_day", False]},
            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
            "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
            "is_present": {"$in": ["$status", list(PRESENCE_STATUSES)]},
            "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
        }},
        {"$addFields": {
            "present_value": {"$cond": [
                {"$and": [
                    {"$toBool": "$is_present"},
                    {"$in": ["$dow", [2, 3, 4, 5, 6]]},
                ]},
                {"$cond": [{"$toBool": "$half_day"}, 0.5, 1.0]},
                0
            ]},
        }},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": "$present_value"},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
        }},
    ]
    result = list(db.attendance_logs.aggregate(pipeline))
    if result:
        r = result[0]
        present_days = r["present_days"]
        leave_days = r["leave_days"]
        late_count = r["late_count"]
        total_late = r["total_late_minutes"]
        total_ot = r["total_overtime_minutes"]
    else:
        present_days = 0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    working_days = count_working_days(month, emp.get("joined_on"))
    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


# --------------------------------------------------------------------------- #
# Analytics - department summary
# --------------------------------------------------------------------------- #
@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    month_start, month_end = month_bounds(month)

    pipeline = [
        {"$match": {"joined_on": {"$lte": month_end}}},
    ]
    if department:
        pipeline[0]["$match"]["department"] = department

    pipeline += [
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"ec": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$ec"]},
                    {"$gte": ["$date", month_start]},
                    {"$lte": ["$date", month_end]},
                ]}}},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$addFields": {
            "half_day": {"$ifNull": ["$logs.half_day", False]},
            "late_minutes": {"$ifNull": ["$logs.late_minutes", 0]},
            "work_hours": "$logs.work_hours",
            "status": "$logs.status",
            "is_present": {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
            "dow": {"$cond": [
                {"$ifNull": ["$logs.date", False]},
                {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                0
            ]},
        }},
        {"$addFields": {
            "present_value": {"$cond": [
                {"$and": [
                    {"$toBool": "$is_present"},
                    {"$in": ["$dow", [2, 3, 4, 5, 6]]},
                ]},
                {"$cond": [{"$toBool": "$half_day"}, 0.5, 1.0]},
                0
            ]},
            "wh_for_avg": {"$cond": [
                {"$and": [{"$toBool": "$is_present"}, {"$ne": ["$work_hours", None]}]},
                "$work_hours",
                None
            ]},
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": "$present_value"},
            "wh_values": {"$push": "$wh_for_avg"},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$addFields": {
            "headcount": {"$size": "$headcount"},
            "avg_work_hours": {"$avg": "$wh_values"},
        }},
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"_id": 1}},
    ]

    items = []
    for r in db.employees.aggregate(pipeline):
        avg_wh = r.get("avg_work_hours")
        if avg_wh is not None:
            avg_wh = round_half_up(avg_wh, 2)
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": r["present_days"],
            "avg_work_hours": avg_wh,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })

    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Analytics - late leaderboard
# --------------------------------------------------------------------------- #
@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    month_start, month_end = month_bounds(month)

    pipeline = [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": 1},
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline += [
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ]

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Analytics - department trend
# --------------------------------------------------------------------------- #
@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(..., alias="to"),
):
    if not valid_date(from_):
        raise HTTPException(422, "invalid from")
    if not valid_date(to):
        raise HTTPException(422, "invalid to")
    if to < from_:
        raise HTTPException(422, "to must not be before from")
    from_dt = date.fromisoformat(from_)
    to_dt = date.fromisoformat(to)
    if (to_dt - from_dt).days > 92:
        raise HTTPException(422, "range exceeds 92 days")

    dept_exists = db.employees.find_one({"department": department}, {"_id": 1})
    if not dept_exists:
        raise HTTPException(404, "unknown department")

    from_bson = datetime(from_dt.year, from_dt.month, from_dt.day, tzinfo=UTC)
    # $densify upper bound is exclusive, so add one day to include the 'to' date
    to_bson = datetime(to_dt.year, to_dt.month, to_dt.day, tzinfo=UTC) + timedelta(days=1)

    pipeline = [
        {"$match": {"date": {"$gte": from_, "$lte": to}}},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {"$addFields": {
            "half_day": {"$ifNull": ["$half_day", False]},
            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
            "is_present": {"$in": ["$status", list(PRESENCE_STATUSES)]},
        }},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [
                {"$toBool": "$is_present"},
                {"$cond": [{"$toBool": "$half_day"}, 0.5, 1.0]},
                0
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
        }},
        {"$project": {
            "date": {"$dateFromString": {"dateString": "$_id"}},
            "present_count": 1,
            "late_count": 1,
            "_id": 0,
        }},
        {"$densify": {
            "field": "date",
            "range": {"step": 1, "unit": "day", "bounds": [from_bson, to_bson]},
        }},
        {"$addFields": {
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$lookup": {
            "from": "employees",
            "pipeline": [
                {"$match": {"department": department}},
                {"$project": {"_id": 0, "joined_on": 1}},
            ],
            "as": "dept_emps",
        }},
        {"$addFields": {
            "date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
            "dow": {"$dayOfWeek": "$date"},
        }},
        {"$addFields": {
            "is_working_day": {"$in": ["$dow", [2, 3, 4, 5, 6]]},
            "headcount": {"$size": {"$filter": {
                "input": "$dept_emps",
                "as": "e",
                "cond": {"$lte": ["$$e.joined_on", "$date_str"]},
            }}},
        }},
        {"$addFields": {
            "attendance_rate": {"$cond": [
                {"$and": [{"$toBool": "$is_working_day"}, {"$gt": ["$headcount", 0]}]},
                {"$divide": ["$present_count", "$headcount"]},
                None
            ]},
        }},
        {"$sort": {"date": 1}},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {
                "moving_avg_7d": {
                    "$avg": "$attendance_rate",
                    "window": {"documents": [-6, 0]},
                }
            },
        }},
        {"$project": {
            "date": "$date_str",
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": 1,
        }},
    ]

    items = []
    for r in db.attendance_logs.aggregate(pipeline):
        ar = r.get("attendance_rate")
        if ar is not None:
            ar = round_half_up(ar, 4)
        ma = r.get("moving_avg_7d")
        if ma is not None:
            ma = round_half_up(ma, 4)
        items.append({
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": r["headcount"],
            "present_count": r["present_count"],
            "late_count": r["late_count"],
            "attendance_rate": ar,
            "moving_avg_7d": ma,
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin - explain
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Optional[int] = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    valid_endpoints = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in valid_endpoints:
        raise HTTPException(422, "invalid endpoint")

    collection = "attendance_logs"

    if endpoint == "attendance_list":
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        df = {}
        if date_from:
            df["$gte"] = date_from
        if date_to:
            df["$lte"] = date_to
        if df:
            q["date"] = df
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }
        explain = db.command("explain", cmd, verbosity="executionStats")

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(422, "emp_code is required")
        if not month or not MONTH_RE.match(month):
            raise HTTPException(422, "valid month is required")
        month_start, month_end = month_bounds(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
            {"$addFields": {
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "is_present": {"$in": ["$status", list(PRESENCE_STATUSES)]},
                "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            }},
            {"$addFields": {
                "is_weekday": {"$in": ["$dow", [2, 3, 4, 5, 6]]},
                "present_value": {"$cond": [
                    {"$and": [{"$toBool": "$is_present"}, {"$toBool": "$is_weekday"}]},
                    {"$cond": [{"$toBool": "$half_day"}, 0.5, 1.0]},
                    0
                ]},
            }},
            {"$group": {
                "_id": None,
                "present_days": {"$sum": "$present_value"},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }},
        ]
        explain = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")

    elif endpoint == "department_summary":
        if not month or not MONTH_RE.match(month):
            raise HTTPException(422, "valid month is required")
        month_start, month_end = month_bounds(month)
        pipeline = [
            {"$match": {"joined_on": {"$lte": month_end}}},
        ]
        if department:
            pipeline[0]["$match"]["department"] = department
        pipeline += [
            {"$lookup": {
                "from": "attendance_logs",
                "let": {"ec": "$emp_code"},
                "pipeline": [
                    {"$match": {"$expr": {"$and": [
                        {"$eq": ["$emp_code", "$$ec"]},
                        {"$gte": ["$date", month_start]},
                        {"$lte": ["$date", month_end]},
                    ]}}},
                ],
                "as": "logs",
            }},
            {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
            {"$addFields": {
                "half_day": {"$ifNull": ["$logs.half_day", False]},
                "late_minutes": {"$ifNull": ["$logs.late_minutes", 0]},
                "work_hours": "$logs.work_hours",
                "status": "$logs.status",
                "is_present": {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                "dow": {"$cond": [
                    {"$ifNull": ["$logs.date", False]},
                    {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                    0
                ]},
            }},
            {"$addFields": {
                "is_weekday": {"$in": ["$dow", [2, 3, 4, 5, 6]]},
                "present_value": {"$cond": [
                    {"$and": [{"$toBool": "$is_present"}, {"$toBool": "$is_weekday"}]},
                    {"$cond": [{"$toBool": "$half_day"}, 0.5, 1.0]},
                    0
                ]},
            }},
            {"$group": {
                "_id": "$department",
                "headcount": {"$addToSet": "$emp_code"},
                "present_days": {"$sum": "$present_value"},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            }},
            {"$addFields": {"headcount": {"$size": "$headcount"}}},
            {"$match": {"headcount": {"$gt": 0}}},
            {"$sort": {"_id": 1}},
        ]
        collection = "employees"
        explain = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")

    elif endpoint == "late_leaderboard":
        if not month or not MONTH_RE.match(month):
            raise HTTPException(422, "valid month is required")
        month_start, month_end = month_bounds(month)
        pipeline = [
            {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}},
            {"$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {"$sum": 1},
            }},
            {"$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }},
            {"$unwind": "$emp"},
        ]
        if department:
            pipeline.append({"$match": {"emp.department": department}})
        pipeline += [
            {"$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }},
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
        ]
        explain = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(422, "department is required")
        if not from_ or not to:
            raise HTTPException(422, "from and to are required")
        if not valid_date(from_) or not valid_date(to):
            raise HTTPException(422, "invalid dates")
        from_dt = date.fromisoformat(from_)
        to_dt = date.fromisoformat(to)
        from_bson = datetime(from_dt.year, from_dt.month, from_dt.day, tzinfo=UTC)
        to_bson = datetime(to_dt.year, to_dt.month, to_dt.day, tzinfo=UTC) + timedelta(days=1)
        pipeline = [
            {"$match": {"date": {"$gte": from_, "$lte": to}}},
            {"$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }},
            {"$unwind": "$emp"},
            {"$match": {"emp.department": department}},
            {"$addFields": {
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "is_present": {"$in": ["$status", list(PRESENCE_STATUSES)]},
            }},
            {"$group": {
                "_id": "$date",
                "present_count": {"$sum": {"$cond": [
                    {"$toBool": "$is_present"},
                    {"$cond": [{"$toBool": "$half_day"}, 0.5, 1.0]},
                    0
                ]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            }},
            {"$project": {
                "date": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
                "_id": 0,
            }},
            {"$densify": {
                "field": "date",
                "range": {"step": 1, "unit": "day", "bounds": [from_bson, to_bson]},
            }},
            {"$addFields": {
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }},
            {"$lookup": {
                "from": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$project": {"_id": 0, "joined_on": 1}},
                ],
                "as": "dept_emps",
            }},
            {"$addFields": {
                "date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
                "dow": {"$dayOfWeek": "$date"},
            }},
            {"$addFields": {
                "is_working_day": {"$in": ["$dow", [2, 3, 4, 5, 6]]},
                "headcount": {"$size": {"$filter": {
                    "input": "$dept_emps",
                    "as": "e",
                    "cond": {"$lte": ["$$e.joined_on", "$date_str"]},
                }}},
            }},
            {"$addFields": {
                "attendance_rate": {"$cond": [
                    {"$and": [{"$toBool": "$is_working_day"}, {"$gt": ["$headcount", 0]}]},
                    {"$divide": ["$present_count", "$headcount"]},
                    None
                ]},
            }},
            {"$sort": {"date": 1}},
            {"$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }},
        ]
        explain = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")

    return {"endpoint": endpoint, "collection": collection, "explain": explain}
