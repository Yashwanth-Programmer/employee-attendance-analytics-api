# Employee Attendance & Analytics API
Employee Attendance and Analytics API built with Python, FastAPI, and MongoDB
All code is in `app/main.py`. Parts A, B and C are done.


## Run
```bash
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```
On Windows PowerShell use `$env:MONGO_URI="..."` and `$env:MONGO_DB="..."`. A local `.env` also works, but real environment variables win. It needs MongoDB 6.0 or newer. All indexes are created at startup, nothing to do by hand.

## Things to know
- Trend `moving_avg_7d` is the average of the rounded (4 decimals) `attendance_rate` values.
- A trend range of more than 92 days returns 422.
- Punch-out closes the latest record with `punch_in` at or before `punched_at`. If there is none, it returns 404.
- A PATCH that loses a race with another change returns 409. Nothing is overwritten.
- Response models are in the code so the generated OpenAPI matches `openapi.yaml`.

## Testing
I wrote my own live tests (32 cases: punch rules, races, PATCH history, ties, no-log employees, weekends, mid-month joiners, overnight shifts, explain plans). They all passed against MongoDB 7. They are not in this repo.

## Speed check
I loaded about 100,000 attendance records (200 employees, 500 days) and timed the endpoints on my own machine with MongoDB 7, five runs each. The median times were between 6 and 115 ms. The slowest was the department summary at 115 ms. The very first request took about 2 seconds while the connection warmed up. All six explain plans use an index and none do a collection scan.
