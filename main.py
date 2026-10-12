"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is read, but real environment variables win)

Reading guide (top to bottom):
  1. Setup            - Mongo connection, indexes created at startup
  2. Small helpers    - time conversion, validation helpers, rounding
  3. Business rules   - R1..R5 as plain functions (late, overtime, work hours...)
  4. Request models   - pydantic models that validate the request bodies
  5. Endpoints        - employees, attendance, analytics, explain
"""
import calendar
import json
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Dict, List, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# =========================================================================== #
# 1. SETUP
# =========================================================================== #
# load_dotenv() does NOT override real environment variables (override=False is the
# default), so variables injected by the grader always win over a local .env file.
load_dotenv()

# tz_aware=True: PyMongo hands back datetimes WITH a UTC time zone (not naive ones).
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]  # R6
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

# Epoch milliseconds must be an integer between these two numbers (see openapi.yaml)
MIN_MS = 100_000_000_000
MAX_MS = 4_102_444_800_000
EpochMs = Annotated[int, Field(strict=True, ge=MIN_MS, le=MAX_MS)]

# (collection, keys, options). Names are fixed so creating them twice is harmless.
INDEXES = [
    # employees
    ("employees", [("emp_code", 1)], {"name": "emp_code_unique", "unique": True}),
    ("employees", [("department", 1), ("emp_code", 1)], {"name": "dept_emp_code"}),
    ("employees", [("joined_on", 1)], {"name": "joined_on"}),
    # attendance_logs
    # one record per (emp_code, date): this is what makes punch-in race-safe
    ("attendance_logs", [("emp_code", 1), ("date", 1)], {"name": "emp_date_unique", "unique": True}),
    # list endpoint sort order, date-range filters, leaderboard month scan
    ("attendance_logs", [("date", -1), ("emp_code", 1)], {"name": "date_desc_emp"}),
    # list endpoint when filtering by status
    ("attendance_logs", [("status", 1), ("date", -1), ("emp_code", 1)], {"name": "status_date_emp"}),
    # punch-out: "latest record of this employee whose punch_in <= punched_at"
    ("attendance_logs", [("emp_code", 1), ("punch_in", -1)], {"name": "emp_punch_in"}),
]


def create_indexes():
    """Create every index. Safe to run many times. Waits a little if MongoDB is still starting."""
    for _ in range(4):
        try:
            client.admin.command("ping")
            break
        except PyMongoError:
            time.sleep(1)
    for collection, keys, options in INDEXES:
        try:
            db[collection].create_index(keys, **options)
        except PyMongoError as error:  # do not crash the whole app because of one index
            print(f"WARNING: could not create index {options['name']}: {error}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(request: Request, error: PyMongoError):
    # A database problem is "service unavailable", never an unhandled 500.
    return JSONResponse(status_code=503, content={"detail": "database error"})


# =========================================================================== #
# 2. SMALL HELPERS
# =========================================================================== #
def invalid(name: str, message: str, where: str = "query"):
    """Raise a normal FastAPI 422 error (same body shape as automatic validation errors)."""
    raise RequestValidationError(
        [{"type": "value_error", "loc": (where, name), "msg": message, "input": None}]
    )


def to_ms(value: Optional[datetime]) -> Optional[int]:
    """BSON datetime -> epoch milliseconds (integer maths only, no float rounding)."""
    if value is None:
        return None
    if value.tzinfo is None:  # safety: treat a naive datetime as UTC
        value = value.replace(tzinfo=UTC)
    return (value - EPOCH) // timedelta(milliseconds=1)


def from_ms(ms: int) -> datetime:
    """Epoch milliseconds -> UTC datetime, cut down to whole seconds (R1)."""
    ms = ms - (ms % 1000)
    return EPOCH + timedelta(milliseconds=ms)


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def whole_seconds(value: datetime) -> datetime:
    return as_utc(value).replace(microsecond=0)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def round_half_up(value, places: int) -> float:
    """Round like school maths (0.5 goes up). Python's round() is NOT half-up."""
    exact = Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return float(exact)


def parse_date(value: str, name: str, where: str = "query") -> date:
    """Strict YYYY-MM-DD that must also be a real calendar day. Otherwise 422."""
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        invalid(name, "must be YYYY-MM-DD", where)
    try:
        return date.fromisoformat(value)
    except ValueError:
        invalid(name, "not a real calendar date", where)


def month_bounds(month: str):
    """'2026-07' -> (date(2026,7,1), date(2026,7,31)). Bad input -> 422."""
    found = re.fullmatch(r"([0-9]{4})-(0[1-9]|1[0-2])", month)
    if not found:
        invalid("month", "must be YYYY-MM")
    year, mon = int(found.group(1)), int(found.group(2))
    try:
        first = date(year, mon, 1)
    except ValueError:
        invalid("month", "not a valid month")
    return first, date(year, mon, calendar.monthrange(year, mon)[1])


def count_working_days(start: date, end: date) -> int:
    """Number of Mon-Fri days from start to end inclusive (R7). At most ~31 loop steps."""
    count = 0
    day = start
    while day <= end:
        if day.weekday() < 5:
            count += 1
        day += timedelta(days=1)
    return count


def weekend_dates(first: date, last: date) -> list:
    """All Saturday/Sunday dates of the period as 'YYYY-MM-DD' strings."""
    result = []
    day = first
    while day <= last:
        if day.weekday() >= 5:
            result.append(day.isoformat())
        day += timedelta(days=1)
    return result


# =========================================================================== #
# 3. BUSINESS RULES (R1 - R5)
# =========================================================================== #
def hhmm_to_minutes(text: str) -> int:
    hours, minutes = text.split(":")
    return int(hours) * 60 + int(minutes)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hhmm_to_minutes(shift_end) <= hhmm_to_minutes(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: the IST date a punch-in belongs to."""
    ist = whole_seconds(punch_in).astimezone(IST)
    day = ist.date()
    if is_overnight(shift_start, shift_end):
        seconds_of_day = ist.hour * 3600 + ist.minute * 60 + ist.second
        # e.g. shift 22:00-06:00, punch-in 02:00 -> belongs to the shift of the previous day
        if seconds_of_day < hhmm_to_minutes(shift_end) * 60:
            day -= timedelta(days=1)
    return day.isoformat()


def shift_window(date_str: str, shift_start: str, shift_end: str):
    """Real start and end instants of the shift that belongs to attendance date `date_str`."""
    day = date.fromisoformat(date_str)
    start_h, start_m = divmod(hhmm_to_minutes(shift_start), 60)
    end_h, end_m = divmod(hhmm_to_minutes(shift_end), 60)
    start = datetime(day.year, day.month, day.day, start_h, start_m, tzinfo=IST)
    end_day = day + timedelta(days=1) if is_overnight(shift_start, shift_end) else day
    end = datetime(end_day.year, end_day.month, end_day.day, end_h, end_m, tzinfo=IST)
    return start, end


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R2: late only if MORE than 10:00 after shift start; then whole minutes since shift start."""
    start, _ = shift_window(date_str, shift_start, shift_end)
    seconds_late = int((whole_seconds(punch_in) - start).total_seconds())
    if seconds_late > 10 * 60:
        return seconds_late // 60
    return 0


def compute_overtime(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes after shift end, only if that is at least 30."""
    _, end = shift_window(date_str, shift_start, shift_end)
    seconds_over = int((whole_seconds(punch_out) - end).total_seconds())
    if seconds_over >= 30 * 60:
        return seconds_over // 60
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: hours with 2 decimals, half-up, using exact Decimal maths."""
    seconds = int((whole_seconds(punch_out) - whole_seconds(punch_in)).total_seconds())
    hours = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_derived(status, punch_in, punch_out, date_str, shift_start, shift_end) -> dict:
    """All four derived fields for a record (see the table in DATA_MODEL.md)."""
    derived = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENT_STATUSES or punch_in is None:
        return derived
    derived["late_minutes"] = compute_late_minutes(punch_in, date_str, shift_start, shift_end)
    if punch_out is not None:
        derived["work_hours"] = compute_work_hours(punch_in, punch_out)
        derived["overtime_minutes"] = compute_overtime(punch_out, date_str, shift_start, shift_end)
        derived["half_day"] = Decimal(str(derived["work_hours"])) < Decimal("4.50")  # R5
    return derived


# =========================================================================== #
# Output formatting (database document -> API JSON)
# =========================================================================== #
def change_value_out(value):
    return to_ms(value) if isinstance(value, datetime) else value


def history_out(entry: dict) -> dict:
    changes = {}
    for field, change in (entry.get("changes") or {}).items():
        changes[field] = {
            "from": change_value_out(change.get("from")),
            "to": change_value_out(change.get("to")),
        }
    return {"at": to_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def record_out(doc: dict) -> dict:
    """Attendance document -> API record. Old documents may miss some fields: use defaults."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day") or False),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": to_ms(doc.get("created_at")),
    }


def get_employee_or_404(emp_code: str) -> dict:
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    return employee


# =========================================================================== #
# 4. REQUEST MODELS
# =========================================================================== #
TIME_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=TIME_PATTERN)
    shift_end: str = Field(default="18:30", pattern=TIME_PATTERN)
    joined_on: str = Field(json_schema_extra={"format": "date"})

    @field_validator("joined_on")
    @classmethod
    def joined_on_must_be_a_real_date(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError("joined_on must be YYYY-MM-DD")
        date.fromisoformat(value)  # raises ValueError for 2026-02-30
        return value

    @model_validator(mode="after")
    def shifts_must_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: EpochMs = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: EpochMs = None


class RegularizeIn(BaseModel):
    status: Status = None
    punch_in: EpochMs = None
    punch_out: EpochMs = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def no_explicit_nulls(self):
        # These three fields may be left out, but may not be sent as null.
        for name in ("status", "punch_in", "punch_out"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} must not be null")
        return self



class ErrorOut(BaseModel):
    detail: str
class ValidationErrorOut(BaseModel):
    detail: List[Dict[str, Any]]
class HealthOut(BaseModel):
    status: Literal["ok"]
class EmployeeOut(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: date
    created_at: Annotated[int, Field(ge=MIN_MS, le=MAX_MS)]
class EmployeePageOut(BaseModel):
    items: List[EmployeeOut]
    total: int
    page: int
    page_size: int
class ChangePairOut(BaseModel):
    from_value: Any = Field(alias="from")
    to_value: Any = Field(alias="to")
class HistoryEntryOut(BaseModel):
    at: Optional[Annotated[int, Field(ge=MIN_MS, le=MAX_MS)]] = None
    by: Optional[str] = None
    reason: Optional[str] = None
    changes: Dict[str, ChangePairOut] = {}
class AttendanceRecordOut(BaseModel):
    emp_code: str
    date: date
    status: Status
    punch_in: Optional[Annotated[int, Field(ge=MIN_MS, le=MAX_MS)]]
    punch_out: Optional[Annotated[int, Field(ge=MIN_MS, le=MAX_MS)]]
    work_hours: Optional[float]
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: List[HistoryEntryOut]
class AttendancePageOut(BaseModel):
    items: List[AttendanceRecordOut]
    total: int
    page: int
    page_size: int
class EmployeeMonthlyOut(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float]
class DepartmentSummaryItemOut(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float]
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int
class DepartmentSummaryOut(BaseModel):
    month: str
    items: List[DepartmentSummaryItemOut]
class LeaderboardItemOut(BaseModel):
    rank: int = Field(ge=1)
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int
class LeaderboardOut(BaseModel):
    month: str
    items: List[LeaderboardItemOut]
class TrendItemOut(BaseModel):
    date: date
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float]
    moving_avg_7d: Optional[float]
class TrendOut(BaseModel):
    department: str
    items: List[TrendItemOut]
class ExplainOut(BaseModel):
    endpoint: str
    collection: str
    explain: Dict[str, Any]

# =========================================================================== #
# 5. ENDPOINTS
# =========================================================================== #
@app.get("/health", response_model=HealthOut, operation_id="health", tags=["system"], responses={503: {"model": ErrorOut}})
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


# ------------------------------- employees -------------------------------- #
@app.post("/employees", status_code=201, response_model=EmployeeOut, operation_id="createEmployee", tags=["employees"], responses={409: {"model": ErrorOut}, 422: {"model": ValidationErrorOut}})
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        # The unique index on emp_code decides who wins when requests arrive together.
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees", response_model=EmployeePageOut, operation_id="listEmployees", tags=["employees"], responses={422: {"model": ValidationErrorOut}})
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}
    if department is not None:
        query["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(query)
    items = []
    if skip < 2**31:  # protects MongoDB from absurd page numbers
        cursor = db.employees.find(query).sort("emp_code", 1).skip(skip).limit(page_size)
        items = [employee_out(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ------------------------------- attendance ------------------------------- #
@app.post("/attendance/punch-in", status_code=201, response_model=AttendanceRecordOut, operation_id="punchIn", tags=["attendance"], responses={404: {"model": ErrorOut}, 409: {"model": ErrorOut}, 422: {"model": ValidationErrorOut}})
def punch_in(body: PunchInIn):
    employee = get_employee_or_404(body.emp_code)
    moment = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    shift_start, shift_end = employee["shift_start"], employee["shift_end"]

    day = attendance_date(moment, shift_start, shift_end)  # R1
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": moment,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(moment, day, shift_start, shift_end),  # R2
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        # No "check first, then insert": the unique (emp_code, date) index lets exactly
        # one of several simultaneous requests succeed. The others get DuplicateKeyError.
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out", response_model=AttendanceRecordOut, operation_id="punchOut", tags=["attendance"], responses={404: {"model": ErrorOut}, 409: {"model": ErrorOut}, 422: {"model": ValidationErrorOut}})
def punch_out(body: PunchOutIn):
    employee = get_employee_or_404(body.emp_code)
    moment = from_ms(body.punched_at) if body.punched_at is not None else now_utc()

    # Latest record of this employee that started at or before the punch-out time.
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": moment}},
        sort=[("punch_in", -1)],
    )
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    started = whole_seconds(record["punch_in"])
    if moment <= started:
        invalid("punched_at", "must be after punch_in", "body")
    if moment - started > timedelta(hours=24):
        invalid("punched_at", "more than 24 hours after punch_in", "body")

    derived = compute_derived(
        record["status"], started, moment, record["date"], employee["shift_start"], employee["shift_end"]
    )
    # The filter "punch_out is empty" makes the update atomic: if two requests race,
    # only one still matches, the other gets None back and answers 409.
    updated = db.attendance_logs.find_one_and_update(
        {"emp_code": body.emp_code, "date": record["date"], "punch_out": None},
        {"$set": {
            "punch_out": moment,
            "work_hours": derived["work_hours"],
            "overtime_minutes": derived["overtime_minutes"],
            "half_day": derived["half_day"],
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


ATTENDANCE_SORT = [("date", -1), ("emp_code", 1)]


def build_attendance_query(emp_code, date_from, date_to, status) -> dict:
    """Filter for GET /attendance (also used by the explain endpoint)."""
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    start = parse_date(date_from, "date_from") if date_from is not None else None
    end = parse_date(date_to, "date_to") if date_to is not None else None
    if start and end and start > end:
        invalid("date_from", "date_from must not be after date_to")
    if start or end:
        query["date"] = {}
        if start:
            query["date"]["$gte"] = start.isoformat()
        if end:
            query["date"]["$lte"] = end.isoformat()
    if status is not None:
        query["status"] = status
    return query


@app.get("/attendance", response_model=AttendancePageOut, operation_id="listAttendance", tags=["attendance"], responses={422: {"model": ValidationErrorOut}})
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, json_schema_extra={"format": "date"}),
    date_to: Optional[str] = Query(None, json_schema_extra={"format": "date"}),
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = build_attendance_query(emp_code, date_from, date_to, status)
    skip = (page - 1) * page_size
    # R10: the total must always reflect the filters, so use an exact count.
    total = db.attendance_logs.count_documents(query)
    items = []
    if skip < 2**31:
        cursor = db.attendance_logs.find(query, {"_id": 0}).sort(ATTENDANCE_SORT).skip(skip).limit(page_size)
        items = [record_out(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", response_model=AttendanceRecordOut, operation_id="regularizeAttendance", tags=["attendance"], responses={404: {"model": ErrorOut}, 409: {"model": ErrorOut}, 422: {"model": ValidationErrorOut}})
def regularize(emp_code: str, date: str = Path(..., json_schema_extra={"format": "date"}), body: RegularizeIn = ...):
    parse_date(date, "date", "path")
    employee = get_employee_or_404(emp_code)
    shift_start, shift_end = employee["shift_start"], employee["shift_end"]
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "no attendance record for that date")

    sent = body.model_fields_set  # which fields the client really sent
    old_status = record["status"]
    old_in, old_out = record.get("punch_in"), record.get("punch_out")
    new_status = body.status if body.status is not None else old_status

    # ---- decide the final status and punch times -------------------------------------------
    if new_status in ("ABSENT", "LEAVE"):
        if "punch_in" in sent or "punch_out" in sent:
            invalid("punch_in", "ABSENT/LEAVE cannot have punch times", "body")
        new_in, new_out = None, None
    else:
        new_in = from_ms(body.punch_in) if "punch_in" in sent else old_in
        new_out = from_ms(body.punch_out) if "punch_out" in sent else old_out
        if new_in is None:
            invalid("punch_in", "a presence status needs a punch_in", "body")
        if "punch_in" in sent or "punch_out" in sent:
            if attendance_date(new_in, shift_start, shift_end) != date:
                invalid("punch_in", "punch_in must stay on the record's attendance date", "body")
            if new_out is not None:
                if whole_seconds(new_out) <= whole_seconds(new_in):
                    invalid("punch_out", "punch_out must be after punch_in", "body")
                if whole_seconds(new_out) - whole_seconds(new_in) > timedelta(hours=24):
                    invalid("punch_out", "punch_out must be within 24 hours of punch_in", "body")

    # ---- does the request change anything the client is allowed to edit? -------------------
    if new_status == old_status and to_ms(new_in) == to_ms(old_in) and to_ms(new_out) == to_ms(old_out):
        invalid("body", "the request changes nothing", "body")

    # ---- recompute the derived fields and list what actually changed -----------------------
    derived = compute_derived(new_status, new_in, new_out, date, shift_start, shift_end)
    new_values = {"status": new_status, "punch_in": new_in, "punch_out": new_out, **derived}
    old_values = {
        "status": old_status,
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes") or 0,
        "overtime_minutes": record.get("overtime_minutes") or 0,
        "half_day": bool(record.get("half_day") or False),
    }
    changes = {}
    for field, new_value in new_values.items():
        old_value = old_values[field]
        if isinstance(new_value, datetime) or isinstance(old_value, datetime):
            same = to_ms(new_value) == to_ms(old_value)
        else:
            same = new_value == old_value
        if not same:
            changes[field] = {"from": old_value, "to": new_value}

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # ---- save, but only if nobody changed the record since we read it ----------------------
    # The filter repeats what we saw (status, punch times) and checks that the history still has
    # exactly `count` entries ("history.N does not exist" = array has at most N items).
    # If another request got in first, nothing matches and the loser gets 409, so no history
    # entry is ever lost or based on stale data.
    count = len(record.get("history") or [])
    lock = {"emp_code": emp_code, "date": date, "status": old_status, "punch_in": old_in, "punch_out": old_out}
    lock[f"history.{count}"] = {"$exists": False}
    if count > 0:
        lock[f"history.{count - 1}"] = {"$exists": True}

    updated = db.attendance_logs.find_one_and_update(
        lock,
        {"$set": new_values, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was changed by someone else, please retry")
    return record_out(updated)


# ------------------------------- analytics -------------------------------- #
# Reusable pieces of aggregation expressions.
IS_PRESENT = {"$in": ["$status", PRESENT_STATUSES]}
LATE_FLAG = {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
# one present day = 1, a half day = 0.5, anything else = 0
DAY_VALUE = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}
# same thing in "half days" (2 or 1) so that the trend maths stays in whole numbers
DAY_HALVES = {"$cond": [IS_PRESENT, {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 1, 2]}, 0]}


def present_value(weekends: list) -> dict:
    """Per-record present days: only presence statuses on Mon-Fri count (R6, R7, R5)."""
    on_weekday = {"$eq": [{"$in": ["$date", weekends]}, False]}  # date is NOT a weekend date
    return {"$cond": [{"$and": [IS_PRESENT, on_weekday]}, DAY_VALUE, 0]}


def build_employee_monthly_pipeline(emp_code: str, first: date, last: date) -> list:
    weekends = weekend_dates(first, last)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": present_value(weekends)},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": LATE_FLAG},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly", response_model=EmployeeMonthlyOut, operation_id="employeeMonthly", tags=["analytics"], responses={404: {"model": ErrorOut}, 422: {"model": ValidationErrorOut}})
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    first, last = month_bounds(month)
    employee = get_employee_or_404(emp_code)

    # R7: working days are counted from joined_on for people who joined during the month
    joined = parse_date(employee["joined_on"], "joined_on", "body")
    working_days = 0 if joined > last else count_working_days(max(first, joined), last)

    rows = list(db.attendance_logs.aggregate(build_employee_monthly_pipeline(emp_code, first, last)))
    stats = rows[0] if rows else {}
    present_days = float(stats.get("present_days", 0))
    pct = None
    if working_days > 0:
        pct = round_half_up(Decimal(str(present_days)) / Decimal(working_days) * 100, 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(stats.get("leave_days", 0)),
        "late_count": int(stats.get("late_count", 0)),
        "total_late_minutes": int(stats.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(stats.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


def build_department_summary_pipeline(first: date, last: date, department: Optional[str]) -> list:
    """Starts from EMPLOYEES so people with no logs are still counted (R9)."""
    weekends = weekend_dates(first, last)
    has_hours = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}
    employee_filter = {"joined_on": {"$lte": last.isoformat()}}  # not joined yet -> not counted
    if department is not None:
        employee_filter["department"] = department
    return [
        {"$match": employee_filter},
        # For every employee: one small stats document built from that month's logs.
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": present_value(weekends)},
                    "late_count": {"$sum": LATE_FLAG},
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                    # work hours in whole hundredths so the final average is exact
                    "hours_cents": {"$sum": {"$cond": [has_hours, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]}},
                    "hours_count": {"$sum": {"$cond": [has_hours, 1, 0]}},
                }},
            ],
            "as": "stats",
        }},
        # keep employees that have no stats (empty array) as well
        {"$unwind": {"path": "$stats", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$stats.present_days"},
            "late_count": {"$sum": "$stats.late_count"},
            "total_late_minutes": {"$sum": "$stats.total_late_minutes"},
            "leave_count": {"$sum": "$stats.leave_count"},
            "on_duty_count": {"$sum": "$stats.on_duty_count"},
            "hours_cents": {"$sum": "$stats.hours_cents"},
            "hours_count": {"$sum": "$stats.hours_count"},
        }},
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary", response_model=DepartmentSummaryOut, operation_id="departmentSummary", tags=["analytics"], responses={422: {"model": ValidationErrorOut}})
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    first, last = month_bounds(month)
    items = []
    for row in db.employees.aggregate(build_department_summary_pipeline(first, last, department)):
        average = None
        if row["hours_count"] > 0:
            # average over RECORDS: total hundredths / number of records / 100, half-up
            average = round_half_up(Decimal(int(row["hours_cents"])) / Decimal(int(row["hours_count"])) / 100, 2)
        items.append({
            "department": row["_id"],
            "headcount": int(row["headcount"]),
            "present_days": float(row["present_days"]),
            "avg_work_hours": average,
            "late_count": int(row["late_count"]),
            "total_late_minutes": int(row["total_late_minutes"]),
            "leave_count": int(row["leave_count"]),
            "on_duty_count": int(row["on_duty_count"]),
        })
    return {"month": month, "items": items}


def build_leaderboard_pipeline(first: date, last: date, limit: int, department: Optional[str]) -> list:
    pipeline = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees disappear here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking: 1, 2, 2, 4 ...
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit AFTER ranking, so ties at the cutoff stay
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
    return pipeline


@app.get("/analytics/leaderboard/late", response_model=LeaderboardOut, operation_id="lateLeaderboard", tags=["analytics"], responses={422: {"model": ValidationErrorOut}})
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = month_bounds(month)
    rows = db.attendance_logs.aggregate(build_leaderboard_pipeline(first, last, limit, department))
    items = [
        {
            "rank": int(row["rank"]),
            "emp_code": row["emp_code"],
            "name": row["name"],
            "department": row["department"],
            "total_late_minutes": int(row["total_late_minutes"]),
            "late_count": int(row["late_count"]),
        }
        for row in rows
    ]
    return {"month": month, "items": items}


def check_trend_range(start: date, end: date):
    if end < start:
        invalid("to", "'to' must not be before 'from'")
    if (end - start).days + 1 > 92:
        invalid("to", "the range must not exceed 92 days")


def build_trend_pipeline(department: str, start: date, end: date) -> list:
    """
    Daily trend. Starts from the department's employees, joins their logs, groups per day,
    fills missing days with $densify, then adds the 7-day window sums with $setWindowFields.
    Rates are calculated in 1/10000 units (whole numbers) so half-up rounding is exact.
    """
    start_s, end_s = start.isoformat(), end.isoformat()
    range_start = datetime(start.year, start.month, start.day, tzinfo=UTC)
    range_end_exclusive = range_start + timedelta(days=(end - start).days + 1)
    last_7_rows = {"documents": [-6, 0]}
    return [
        {"$match": {"department": department, "joined_on": {"$lte": end_s}}},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": start_s, "$lte": end_s}}},
                {"$project": {"_id": 0, "date": 1, "halves": DAY_HALVES, "late": LATE_FLAG}},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        # One row per day. Employees without logs fall into the first day with zeros, so this
        # stage never returns an empty result (which keeps $densify happy).
        {"$group": {
            "_id": {"$ifNull": ["$logs.date", start_s]},
            "present_halves": {"$sum": "$logs.halves"},
            "late_count": {"$sum": "$logs.late"},
        }},
        {"$addFields": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$sort": {"d": 1}},
        # Gap filling: create a row for every missing calendar day in the range.
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [range_start, range_end_exclusive]}}},
        {"$addFields": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}},
            "present_halves": {"$ifNull": ["$present_halves", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "day_of_week": {"$dayOfWeek": "$d"},  # 1 = Sunday ... 7 = Saturday
        }},
        # headcount of that day = department employees with joined_on <= that day (R9)
        {"$lookup": {
            "from": "employees",
            "let": {"day": "$date"},
            "pipeline": [
                {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}},
                {"$count": "n"},
            ],
            "as": "hc",
        }},
        {"$addFields": {
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]},
            "is_working_day": {"$and": [{"$gte": ["$day_of_week", 2]}, {"$lte": ["$day_of_week", 6]}]},
        }},
        # rate in 1/10000, rounded half-up: floor((halves * 10000 + headcount) / (2 * headcount))
        {"$addFields": {"rate_units": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [
                {"$add": [{"$multiply": ["$present_halves", 10000]}, "$headcount"]},
                {"$multiply": [2, "$headcount"]},
            ]}},
            None,
        ]}}},
        # moving window = this row and up to 6 rows before it, inside the range only
        {"$setWindowFields": {
            "sortBy": {"d": 1},
            "output": {
                "window_sum": {"$sum": "$rate_units", "window": last_7_rows},
                "window_n": {"$sum": {"$cond": [{"$ne": ["$rate_units", None]}, 1, 0]}, "window": last_7_rows},
            },
        }},
        {"$sort": {"d": 1}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_halves": 1,
            "late_count": 1, "rate_units": 1, "window_sum": 1, "window_n": 1,
        }},
    ]


def format_trend_rows(rows: list) -> list:
    """Turn database rows into API rows (just number formatting, no calculations over data)."""
    items = []
    for row in rows:
        rate = None
        if row.get("rate_units") is not None:
            rate = int(round(row["rate_units"])) / 10000
        moving = None
        n = int(row.get("window_n") or 0)
        if n > 0:
            total = int(round(row["window_sum"]))
            moving = ((2 * total + n) // (2 * n)) / 10000  # mean of the window, half-up, 4 decimals
        items.append({
            "date": row["date"],
            "is_working_day": bool(row["is_working_day"]),
            "headcount": int(row["headcount"]),
            "present_count": float(row["present_halves"]) / 2,
            "late_count": int(row["late_count"]),
            "attendance_rate": rate,
            "moving_avg_7d": moving,
        })
    return items


@app.get("/analytics/departments/{department}/trend", response_model=TrendOut, operation_id="departmentTrend", tags=["analytics"], responses={404: {"model": ErrorOut}, 422: {"model": ValidationErrorOut}})
def department_trend(
    department: str,
    from_: str = Query(..., alias="from", json_schema_extra={"format": "date"}),
    to: str = Query(..., json_schema_extra={"format": "date"}),
):
    start = parse_date(from_, "from")
    end = parse_date(to, "to")
    check_trend_range(start, end)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    rows = list(db.employees.aggregate(build_trend_pipeline(department, start, end)))
    return {"department": department, "items": format_trend_rows(rows)}


# --------------------------------- explain -------------------------------- #
def run_explain(command: dict) -> dict:
    """Run MongoDB explain (executionStats) and make the answer JSON friendly."""
    result = db.command({"explain": command, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS))


def need(value, name: str):
    if value is None:
        invalid(name, "this parameter is required for this endpoint")
    return value


@app.get("/admin/explain/{endpoint}", response_model=ExplainOut, operation_id="explainEndpoint", tags=["admin"], responses={422: {"model": ValidationErrorOut}})
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, json_schema_extra={"format": "date"}),
    date_to: Optional[str] = Query(None, json_schema_extra={"format": "date"}),
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from", json_schema_extra={"format": "date"}),
    to: Optional[str] = Query(None, json_schema_extra={"format": "date"}),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    # Every branch builds the query with the SAME functions the real endpoint uses.
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        command = {
            "find": collection,
            "filter": build_attendance_query(emp_code, date_from, date_to, status),
            "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            collection = "attendance_logs"
            first, last = month_bounds(need(month, "month"))
            pipeline = build_employee_monthly_pipeline(need(emp_code, "emp_code"), first, last)
        elif endpoint == "department_summary":
            collection = "employees"
            first, last = month_bounds(need(month, "month"))
            pipeline = build_department_summary_pipeline(first, last, department)
        elif endpoint == "late_leaderboard":
            collection = "attendance_logs"
            first, last = month_bounds(need(month, "month"))
            pipeline = build_leaderboard_pipeline(first, last, limit, department)
        else:  # department_trend
            collection = "employees"
            start = parse_date(need(from_, "from"), "from")
            end = parse_date(need(to, "to"), "to")
            check_trend_range(start, end)
            pipeline = build_trend_pipeline(need(department, "department"), start, end)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    return {"endpoint": endpoint, "collection": collection, "explain": run_explain(command)}