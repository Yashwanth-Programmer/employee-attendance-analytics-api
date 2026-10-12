# Employee Attendance & Analytics API

A RESTful API built with Python, FastAPI, and MongoDB for employee attendance management and workforce analytics.

## Features

- Employee management and attendance pagination.
- Punch-in/punch-out with business rules.
- Manual attendance corrections with an audit trail.
- Monthly summaries, department analytics, late-arrival leaderboard, and attendance trends.
- MongoDB aggregation pipelines and indexes.

## Tech Stack

Python 3.11+, FastAPI, MongoDB 6.0+, PyMongo, and Pydantic.

## Setup

Install dependencies:

```bash
pip install -r requirements.txt
```

Configure MongoDB using environment variables.

**Windows PowerShell:**

```powershell
$env:MONGO_URI="mongodb://localhost:27017"
$env:MONGO_DB="attendance_db"
```

Start the application from the project root:

```bash
python -m uvicorn app.main:app --port 8000
```

API documentation: http://localhost:8000/docs

## Business Rules

- Attendance dates follow IST, including overnight shifts.
- Unique indexes prevent duplicate employee attendance records.
- Manual corrections maintain history and detect conflicting updates.
- Analytics use MongoDB aggregation pipelines.
- Department trends support ranges up to 92 days.

## Performance

Latest local benchmark: approximately 100,000 attendance records and 200 employees on MongoDB 7.

| Endpoint | Median |
|---|---:|
| Attendance list, page 1 | 91 ms |
| Attendance list, page 500 | 214 ms |
| Employee date-range query | 7 ms |
| WFH filter | 22 ms |
| Monthly employee summary | 14 ms |
| Department summary | 367 ms |
| Late leaderboard | 81 ms |
| Department trend, 92 days | 229 ms |

All six explain-plan checks reported `IXSCAN=True` and `COLLSCAN=False` in the reported run.

## Documentation

See [DECISIONS.md](DECISIONS.md) for design choices and [REVIEW.md](REVIEW.md) for the starter-code review.
