# Employee Attendance & Analytics API

FastAPI + MongoDB backend for employee punch-in/punch-out, manual regularization with audit history, and attendance analytics.

## Requirements

- Python 3.11+
- MongoDB 6.0+ (local Docker or Atlas)

## Setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Set environment variables (a `.env` file is loaded for convenience, but real environment variables win):

```
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

## Run

```bash
uvicorn app.main:app --port 8000
```

All indexes are created at startup. Load the sample data (optional):

```bash
python candidate_kit/sample_seed.py
```

## Submitted files

- `app/main.py` — all application code
- `requirements.txt` — dependencies
- `REVIEW.md` — review of the starter code
- `DECISIONS.md` — design decisions

## Notes

- Instants in the API are epoch milliseconds; MongoDB stores BSON datetimes.
- Derived fields (`work_hours`, `late_minutes`, `overtime_minutes`, `half_day`) are computed by the API and read back by analytics.
- No `.env`, secrets, or Dockerfile are committed.
