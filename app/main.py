"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB   (a local .env is loaded for convenience; real environment variables win)

Layout of this file (everything lives here on purpose, see the assignment):
  1. Pure helpers      - time zone, R1-R5 maths, rounding. Stdlib only, no database.
  2. Models            - request validation (pydantic).
  3. Database + indexes
  4. Serialisers       - BSON -> API shape (epoch milliseconds, no _id).
  5. Employees, attendance (punch-in / punch-out / list / regularize)
  6. Analytics         - three aggregation pipelines + monthly summary
  7. Explain endpoint
"""
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # override=False: variables already in the environment take priority
log = logging.getLogger("attendance")

# <<PURE-HELPERS-START>>
# --------------------------------------------------------------------------- #
# 1. Pure helpers (no database, no framework) - unit-tested in tests/test_logic.py
# --------------------------------------------------------------------------- #
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")  # R6
GRACE_SECONDS = 10 * 60                            # R2
MIN_OVERTIME_MINUTES = 30                          # R3
HALF_DAY_LIMIT = Decimal("4.50")                   # R5
MAX_PUNCH_SPAN = timedelta(hours=24)

_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def now_utc() -> datetime:
    """Current time, UTC-aware, truncated to whole seconds (R1)."""
    return datetime.now(UTC).replace(microsecond=0)


def utc_from_ms(ms: int) -> datetime:
    """Epoch milliseconds -> UTC-aware datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def as_utc(dt):
    """PyMongo can hand back naive datetimes (they are UTC). Make everything aware."""
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def to_ms(dt: datetime) -> int:
    """UTC datetime -> epoch milliseconds, using integer arithmetic only."""
    return (as_utc(dt) - EPOCH) // timedelta(milliseconds=1)


def round_half_up(value, places: int) -> float:
    """Round half-up (R4, R8). Python's round() is banker's rounding on binary floats."""
    if not isinstance(value, Decimal):
        value = Decimal(str(value))  # str() gives the shortest repr, i.e. what a human sees
    return float(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def parse_iso_date(value: str) -> date:
    """Strict YYYY-MM-DD (date.fromisoformat alone also accepts '20260706' on 3.11+)."""
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise ValueError("expected YYYY-MM-DD")
    return date.fromisoformat(value)


def parse_hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return parse_hhmm(shift_end) <= parse_hhmm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; an overnight shift's early-morning punch-in
    (earlier than shift_end) belongs to the shift that started the previous day."""
    local = as_utc(punch_in).astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end) and local.time() < parse_hhmm(shift_end):
        day -= timedelta(days=1)
    return day.isoformat()


def shift_start_at(date_str: str, shift_start: str) -> datetime:
    return datetime.combine(date.fromisoformat(date_str), parse_hhmm(shift_start), tzinfo=IST)


def shift_end_at(date_str: str, shift_start: str, shift_end: str) -> datetime:
    end = datetime.combine(date.fromisoformat(date_str), parse_hhmm(shift_end), tzinfo=IST)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)  # the shift ends on the next calendar day
    return end


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str) -> int:
    """R2: late only if STRICTLY more than 10:00 after shift start; then whole minutes since
    shift start (not since the end of the grace period). Compared in seconds, not minutes."""
    seconds = int((as_utc(punch_in) - shift_start_at(date_str, shift_start)).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, shift_end_dt: datetime) -> int:
    """R3: whole minutes from shift end to punch-out, counted only if >= 30."""
    seconds = int((as_utc(punch_out) - shift_end_dt).total_seconds())
    minutes = seconds // 60 if seconds > 0 else 0
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded half-up to 2 decimals (exact Decimal arithmetic)."""
    seconds = int((as_utc(punch_out) - as_utc(punch_in)).total_seconds())
    return round_half_up(Decimal(seconds) / Decimal(3600), 2)


def compute_half_day(work_hours: float) -> bool:
    """R5: on the ROUNDED work_hours."""
    return Decimal(str(work_hours)) < HALF_DAY_LIMIT


def derive_fields(status, punch_in, punch_out, date_str, shift_start, shift_end) -> dict:
    """The four stored derived fields for a record in a given final state (R2-R5)."""
    if status not in PRESENCE_STATUSES or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, date_str, shift_start)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(punch_in, punch_out)
    overtime = compute_overtime(punch_out, shift_end_at(date_str, shift_start, shift_end))
    return {"work_hours": hours, "late_minutes": late, "overtime_minutes": overtime,
            "half_day": compute_half_day(hours)}


def month_bounds(month: str) -> tuple:
    year, mon = int(month[:4]), int(month[5:7])
    return date(year, mon, 1), date(year, mon, calendar.monthrange(year, mon)[1])


def count_working_days(start: date, end: date) -> int:
    """R7: Monday-Friday, no holiday calendar."""
    days, day = 0, start
    while day <= end:
        if day.weekday() < 5:
            days += 1
        day += timedelta(days=1)
    return days
# <<PURE-HELPERS-END>>


# --------------------------------------------------------------------------- #
# 2. Models
# --------------------------------------------------------------------------- #
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
# strict=True: floats, strings and booleans are rejected (422); the bounds reject "seconds".
EpochMillis = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
HHMM = Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"


class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_joined_on(cls, value):
        parse_iso_date(value)  # raises ValueError -> 422
        return value

    @model_validator(mode="after")
    def _shift_times_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeRequest(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="before")
    @classmethod
    def _no_explicit_nulls(cls, data):
        # "Omitted fields are unchanged" - an explicit null is not the same thing, and the
        # schema does not allow it.
        if isinstance(data, dict):
            for key in ("status", "punch_in", "punch_out"):
                if key in data and data[key] is None:
                    raise ValueError(f"{key} must not be null")
        return data
    # Derived fields sent by a client are ignored: pydantic drops unknown keys by default.


def unprocessable(where: str, name: str, message: str) -> HTTPException:
    """422 with the same shape FastAPI uses for its own validation errors."""
    return HTTPException(422, [{"type": "value_error", "loc": [where, name], "msg": message}])


def date_or_422(value: str, where: str, name: str) -> date:
    try:
        return parse_iso_date(value)
    except ValueError:
        raise unprocessable(where, name, "must be a valid date in YYYY-MM-DD format")


def month_or_422(month: str) -> tuple:
    try:
        return month_bounds(month)
    except ValueError:  # e.g. year 0000, which the regex alone lets through
        raise unprocessable("query", "month", "invalid month")


# --------------------------------------------------------------------------- #
# 3. Database and indexes
# --------------------------------------------------------------------------- #
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True: every datetime read from MongoDB is UTC-aware instead of naive.
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]
employees_col = db["employees"]
logs_col = db["attendance_logs"]

# Index names double as hints, so explain output is deterministic.
EMP_CODE_UQ = "emp_code_uq"
EMP_DEPT_CODE = "emp_dept_code"
EMP_DEPT_JOINED = "emp_dept_joined_on"
EMP_JOINED = "emp_joined_on"
LOGS_EMP_DATE_UQ = "logs_emp_code_date_uq"
LOGS_DATE_EMP = "logs_date_emp_code"
LOGS_STATUS_DATE_EMP = "logs_status_date_emp_code"

_indexes_ready = False


def ensure_indexes() -> None:
    """Idempotent: creating an identical index again is a no-op."""
    global _indexes_ready
    employees_col.create_index([("emp_code", ASCENDING)], unique=True, name=EMP_CODE_UQ)
    employees_col.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name=EMP_DEPT_CODE)
    employees_col.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name=EMP_DEPT_JOINED)
    employees_col.create_index([("joined_on", ASCENDING)], name=EMP_JOINED)
    logs_col.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name=LOGS_EMP_DATE_UQ)
    logs_col.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name=LOGS_DATE_EMP)
    logs_col.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name=LOGS_STATUS_DATE_EMP
    )
    _indexes_ready = True


def try_ensure_indexes() -> bool:
    try:
        ensure_indexes()
    except PyMongoError as exc:
        log.warning("could not create indexes yet: %s", exc)
    return _indexes_ready


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try_ensure_indexes()  # if MongoDB is down now, /health retries later
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_request, exc):
    log.error("database error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    if not _indexes_ready:
        try_ensure_indexes()
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# 4. Serialisers (BSON -> API)
# --------------------------------------------------------------------------- #
def api_value(value):
    return to_ms(value) if isinstance(value, datetime) else value


def serialize_history(entry: dict) -> dict:
    changes = {
        field: {"from": api_value(change.get("from")), "to": api_value(change.get("to"))}
        for field, change in (entry.get("changes") or {}).items()
    }
    return {"at": api_value(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"),
            "changes": changes}


def serialize_attendance(doc: dict) -> dict:
    """Explicit field list: _id can never leak, and legacy records get the documented defaults."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": api_value(doc.get("punch_in")),
        "punch_out": api_value(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day")),
        "history": [serialize_history(h) for h in (doc.get("history") or [])],
    }


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"], "name": doc["name"], "email": doc["email"],
        "department": doc["department"], "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"], "joined_on": doc["joined_on"],
        "created_at": api_value(doc.get("created_at")),
    }


def get_employee_or_404(emp_code: str) -> dict:
    emp = employees_col.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


def shift_of(emp: dict) -> tuple:
    return emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")


MAX_SKIP = 2**31  # beyond this there is nothing to return; avoids int overflow in the driver


def skip_for(page: int, page_size: int) -> int:
    return (page - 1) * page_size


# --------------------------------------------------------------------------- #
# 5a. Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = {
        "emp_code": body.emp_code, "name": body.name, "email": body.email,
        "department": body.department, "shift_start": body.shift_start,
        "shift_end": body.shift_end, "joined_on": body.joined_on, "created_at": now_utc(),
    }
    try:
        employees_col.insert_one(dict(doc))  # a copy: insert_one would add _id to the dict
    except DuplicateKeyError:                # the unique index decides, not a prior find()
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {} if department is None else {"department": department}
    total = employees_col.count_documents(query)
    skip = skip_for(page, page_size)
    items = []
    if skip < MAX_SKIP:
        cursor = employees_col.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
        items = [serialize_employee(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# 5b. Punch-in / punch-out
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = get_employee_or_404(body.emp_code)
    shift_start, shift_end = shift_of(emp)
    ts = utc_from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, shift_start, shift_end)  # R1
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, day, shift_start),  # R2
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        logs_col.insert_one(doc)  # unique (emp_code, date) index: one winner, the rest get 409
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = get_employee_or_404(body.emp_code)
    shift_start, shift_end = shift_of(emp)
    ts = utc_from_ms(body.punched_at) if body.punched_at is not None else now_utc()

    # The employee's most recent record that started at or before the punch-out time.
    # Records with no punch_in (ABSENT / LEAVE) never match the $lte.
    rec = logs_col.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    started = as_utc(rec["punch_in"])
    if ts <= started:
        raise unprocessable("body", "punched_at", "must be after the punch-in")
    if ts - started > MAX_PUNCH_SPAN:
        raise unprocessable("body", "punched_at", "must be within 24 hours of the punch-in")

    hours = compute_work_hours(started, ts)                                          # R4
    overtime = compute_overtime(ts, shift_end_at(rec["date"], shift_start, shift_end))  # R3
    # Compare-and-set: only succeeds while the record is still open and unchanged. Of two
    # simultaneous punch-outs exactly one matches; the other gets None -> 409.
    updated = logs_col.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None, "punch_in": rec["punch_in"]},
        {"$set": {"punch_out": ts, "work_hours": hours, "overtime_minutes": overtime,
                  "half_day": compute_half_day(hours)}},  # R5
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return serialize_attendance(updated)


# --------------------------------------------------------------------------- #
# 5c. Listing attendance
# --------------------------------------------------------------------------- #
ATTENDANCE_SORT_DOC = {"date": -1, "emp_code": 1}
ATTENDANCE_SORT = list(ATTENDANCE_SORT_DOC.items())


def attendance_filter(emp_code, date_from, date_to, status) -> tuple:
    """Shared by GET /attendance and the explain endpoint. Returns (filter, index hint)."""
    start = date_or_422(date_from, "query", "date_from") if date_from is not None else None
    end = date_or_422(date_to, "query", "date_to") if date_to is not None else None
    if start and end and start > end:
        raise unprocessable("query", "date_from", "date_from must not be after date_to")
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if start or end:
        query["date"] = {}
        if start:
            query["date"]["$gte"] = date_from
        if end:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    # Pick the index that matches the filter shape (see DECISIONS.md).
    if emp_code is not None:
        hint = LOGS_EMP_DATE_UQ
    elif status is not None:
        hint = LOGS_STATUS_DATE_EMP
    else:
        hint = LOGS_DATE_EMP
    return query, hint


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query, hint = attendance_filter(emp_code, date_from, date_to, status)
    # An unfiltered count is answered from collection metadata instead of scanning 100k docs.
    total = logs_col.estimated_document_count() if not query else logs_col.count_documents(query, hint=hint)
    skip = skip_for(page, page_size)
    items = []
    if skip < MAX_SKIP:
        cursor = (logs_col.find(query, {"_id": 0}).sort(ATTENDANCE_SORT)
                  .skip(skip).limit(page_size).hint(hint))
        items = [serialize_attendance(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# 5d. Regularization (manual correction with audit trail)
# --------------------------------------------------------------------------- #
CHANGE_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes",
                 "overtime_minutes", "half_day")


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeRequest):
    date_or_422(date, "path", "date")
    emp = get_employee_or_404(emp_code)
    rec = logs_col.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")
    shift_start, shift_end = shift_of(emp)

    old = {
        "status": rec["status"],
        "punch_in": as_utc(rec.get("punch_in")),
        "punch_out": as_utc(rec.get("punch_out")),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day")),
    }
    given_in = utc_from_ms(body.punch_in) if body.punch_in is not None else None
    given_out = utc_from_ms(body.punch_out) if body.punch_out is not None else None
    status = body.status or old["status"]

    # ---- validate the FINAL state, not just the request ----
    if status in ("ABSENT", "LEAVE"):
        if given_in is not None or given_out is not None:
            raise unprocessable("body", "status", "ABSENT/LEAVE clear the punch times; do not send them")
        new_in = new_out = None
    else:
        new_in = given_in if given_in is not None else old["punch_in"]
        new_out = given_out if given_out is not None else old["punch_out"]
        if new_in is None:
            raise unprocessable("body", "punch_in", "a presence status requires a punch_in")
        if given_in is not None and attendance_date(new_in, shift_start, shift_end) != date:
            raise unprocessable("body", "punch_in", "punch_in must stay on the record's attendance date")
        if (given_in is not None or given_out is not None) and new_out is not None:
            if new_out <= new_in:
                raise unprocessable("body", "punch_out", "punch_out must be after punch_in")
            if new_out - new_in > MAX_PUNCH_SPAN:
                raise unprocessable("body", "punch_out", "punch_out must be within 24 hours of punch_in")

    new = {"status": status, "punch_in": new_in, "punch_out": new_out}
    new.update(derive_fields(status, new_in, new_out, date, shift_start, shift_end))  # recompute R2-R5

    # A request that changes nothing is 422. Judged on what the client can change; stale
    # derived values in old data must not turn a no-op into a 200.
    if not any(old[key] != new[key] for key in ("status", "punch_in", "punch_out")):
        raise unprocessable("body", "status", "the request changes nothing")

    changes = {f: {"from": old[f], "to": new[f]} for f in CHANGE_FIELDS if old[f] != new[f]}
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: the update only applies if the record is still exactly as we read
    # it (same status/times, same number of history entries). A concurrent PATCH or punch-out
    # makes this match nothing, so nobody's history entry is silently overwritten -> 409.
    updated = logs_col.find_one_and_update(
        {
            "_id": rec["_id"],
            "status": rec["status"],
            "punch_in": rec.get("punch_in"),
            "punch_out": rec.get("punch_out"),
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(rec.get("history") or [])]},
        },
        {"$set": {k: new[k] for k in ("status", "punch_in", "punch_out", "work_hours",
                                      "late_minutes", "overtime_minutes", "half_day")},
         "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently, please retry")
    return serialize_attendance(updated)


# --------------------------------------------------------------------------- #
# 6. Analytics - aggregation pipeline builders
#    Each builder returns (collection name, pipeline, index hint). The real endpoint and the
#    explain endpoint both call the SAME builder, so explain shows the real query.
# --------------------------------------------------------------------------- #
def _is_presence() -> dict:
    return {"$in": ["$status", list(PRESENCE_STATUSES)]}


def _is_weekday() -> dict:
    # `date` is a "YYYY-MM-DD" string; parse it as a UTC date and test Mon-Fri (ISO day 1-5).
    return {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 5]}


def _half_day() -> dict:
    return {"$eq": [{"$ifNull": ["$half_day", False]}, True]}  # missing means false


def _is_late() -> dict:
    return {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}


def _present_days_expr() -> dict:
    """R5-R7: presence status, Mon-Fri only, a half day counts 0.5."""
    return {"$cond": [{"$and": [_is_presence(), _is_weekday()]},
                      {"$cond": [_half_day(), 0.5, 1]}, 0]}


def _count_if(condition: dict) -> dict:
    return {"$sum": {"$cond": [condition, 1, 0]}}


def monthly_plan(emp_code: str, month: str) -> tuple:
    first, last = month_or_422(month)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": _present_days_expr()},
            "leave_days": _count_if({"$eq": ["$status", "LEAVE"]}),
            "late_count": _count_if(_is_late()),
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    return "attendance_logs", pipeline, LOGS_EMP_DATE_UQ


def summary_plan(month: str, department: Optional[str]) -> tuple:
    """Starts from `employees` so people with zero logs still count (R9)."""
    first, last = month_or_422(month)
    match = {"joined_on": {"$lte": last.isoformat()}}  # not yet joined -> not counted
    if department is not None:
        match["department"] = department
    # Work in whole hundredths so the average is exact: sum(cents) / n / 100, rounded half-up
    # in Python. Averaging doubles in the database could land a hair under a .xx5 tie.
    has_hours = {"$and": [_is_presence(), {"$isNumber": "$work_hours"}]}
    per_employee = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": _present_days_expr()},
            "leave_count": _count_if({"$eq": ["$status", "LEAVE"]}),
            "on_duty_count": _count_if({"$eq": ["$status", "ON_DUTY"]}),
            "late_count": _count_if(_is_late()),
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "hours_cents": {"$sum": {"$cond": [has_hours,
                                               {"$toLong": {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}}, 0]}},
            "hours_n": _count_if(has_hours),
        }},
    ]
    pipeline = [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                     "pipeline": per_employee, "as": "s"}},
        # one row per employee; an employee with no logs has s == [] and is kept
        {"$unwind": {"path": "$s", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            **{field: {"$sum": {"$ifNull": [f"$s.{field}", 0]}} for field in (
                "present_days", "leave_count", "on_duty_count", "late_count",
                "total_late_minutes", "hours_cents", "hours_n")},
        }},
        {"$sort": {"_id": 1}},
    ]
    hint = EMP_DEPT_JOINED if department is not None else EMP_JOINED
    return "employees", pipeline, hint


def leaderboard_plan(month: str, limit: int, department: Optional[str]) -> tuple:
    first, last = month_or_422(month)
    pipeline = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()},
                    "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs whose employee does not exist are dropped here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking: ties share a rank, the next rank is skipped.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return "attendance_logs", pipeline, LOGS_DATE_EMP


def trend_plan(department: str, start: date, end: date) -> tuple:
    """One output row per calendar day, built in the database:
    1. collect the department's emp_codes and join dates into one document (index on department);
    2. $lookup the per-day totals of all those employees' logs (index on emp_code, date);
    3. $range + $dateAdd generate one row per day - weekends and empty days included;
    4. headcount, rate and the 7-day window are computed per row."""
    n_days = (end - start).days + 1
    start_dt = datetime.combine(start, time.min, tzinfo=UTC)
    day_totals = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$group": {
            "_id": "$date",
            "present_halves": {"$sum": {"$cond": [_is_presence(), {"$cond": [_half_day(), 1, 2]}, 0]}},
            "late_count": _count_if(_is_late()),
        }},
    ]
    pipeline = [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "codes": {"$push": "$emp_code"}, "joined": {"$push": "$joined_on"}}},
        # an array in localField matches any of its elements
        {"$lookup": {"from": "attendance_logs", "localField": "codes", "foreignField": "emp_code",
                     "pipeline": day_totals, "as": "days"}},
        {"$project": {"_id": 0, "joined": 1, "days": 1, "i": {"$range": [0, n_days]}}},
        {"$unwind": "$i"},
        {"$addFields": {"d": {"$dateAdd": {"startDate": start_dt, "unit": "day", "amount": "$i"}}}},
        {"$addFields": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}},
                        "dow": {"$isoDayOfWeek": "$d"}}},
        {"$addFields": {"stat": {"$arrayElemAt": [
            {"$filter": {"input": "$days", "as": "x", "cond": {"$eq": ["$$x._id", "$date"]}}}, 0]}}},
        {"$project": {
            "date": 1,
            "is_working_day": {"$lte": ["$dow", 5]},
            "headcount": {"$size": {"$filter": {"input": "$joined", "as": "j",
                                                 "cond": {"$lte": ["$$j", "$date"]}}}},  # R9
            "present_halves": {"$ifNull": ["$stat.present_halves", 0]},
            "late_count": {"$ifNull": ["$stat.late_count", 0]},
        }},
        # Rate in integer ten-thousandths, rounded half-up with integer maths:
        #   floor(halves/2/headcount * 10000 + 0.5) == floor((10000*halves + headcount) / (2*headcount))
        {"$addFields": {"rate_x10000": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [{"$add": [{"$multiply": ["$present_halves", 10000]}, "$headcount"]},
                                    {"$multiply": ["$headcount", 2]}]}},
            None]}}},
        # current row + up to 6 previous rows, inside the requested range only; $avg skips nulls
        {"$setWindowFields": {"sortBy": {"date": 1},
                              "output": {"avg_x10000": {"$avg": "$rate_x10000",
                                                        "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"date": 1}},
    ]
    return "employees", pipeline, EMP_DEPT_CODE


def run_aggregate(plan: tuple) -> list:
    collection, pipeline, hint = plan
    return list(db[collection].aggregate(pipeline, hint=hint))


# --------------------------------------------------------------------------- #
# 6b. Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    plan = monthly_plan(emp_code, month)  # validates the month first -> 422 before 404
    emp = get_employee_or_404(emp_code)
    first, last = month_bounds(month)
    rows = run_aggregate(plan)
    stats = rows[0] if rows else {}
    start = max(first, parse_iso_date(emp["joined_on"]))  # R7: count from the join date
    working_days = count_working_days(start, last) if start <= last else 0
    present = float(stats.get("present_days", 0))
    pct = None if working_days == 0 else round_half_up(Decimal(str(present)) / Decimal(working_days) * 100, 2)
    return {
        "emp_code": emp_code, "month": month, "working_days": working_days,
        "present_days": present, "leave_days": int(stats.get("leave_days", 0)),
        "late_count": int(stats.get("late_count", 0)),
        "total_late_minutes": int(stats.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(stats.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    items = []
    for row in run_aggregate(summary_plan(month, department)):
        n = int(row["hours_n"])
        avg = None if n == 0 else round_half_up(Decimal(int(row["hours_cents"])) / Decimal(n) / Decimal(100), 2)
        items.append({
            "department": row["_id"], "headcount": int(row["headcount"]),
            "present_days": float(row["present_days"]), "avg_work_hours": avg,
            "late_count": int(row["late_count"]), "total_late_minutes": int(row["total_late_minutes"]),
            "leave_count": int(row["leave_count"]), "on_duty_count": int(row["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    items = run_aggregate(leaderboard_plan(month, limit, department))
    return {"month": month, "items": [
        {"rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"],
         "department": r["department"], "total_late_minutes": int(r["total_late_minutes"]),
         "late_count": int(r["late_count"])} for r in items]}


def trend_range_or_422(date_from: str, date_to: str) -> tuple:
    start = date_or_422(date_from, "query", "from")
    end = date_or_422(date_to, "query", "to")
    if end < start:
        raise unprocessable("query", "to", "to must not be before from")
    if (end - start).days + 1 > 92:
        raise unprocessable("query", "to", "the range must not exceed 92 days")
    return start, end


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    start, end = trend_range_or_422(from_, to)
    if employees_col.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    items = []
    for row in run_aggregate(trend_plan(department, start, end)):
        rate = row["rate_x10000"]
        avg = row["avg_x10000"]
        items.append({
            "date": row["date"],
            "is_working_day": bool(row["is_working_day"]),
            "headcount": int(row["headcount"]),
            "present_count": float(Decimal(int(row["present_halves"])) / 2),
            "late_count": int(row["late_count"]),
            "attendance_rate": None if rate is None else round_half_up(Decimal(int(rate)) / Decimal(10000), 4),
            "moving_avg_7d": None if avg is None else round_half_up(Decimal(str(avg)) / Decimal(10000), 4),
        })
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 7. Explain endpoint
# --------------------------------------------------------------------------- #
ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary",
                        "late_leaderboard", "department_trend"]


def require(value, name: str):
    if value is None:
        raise unprocessable("query", name, "field required")
    return value


def run_explain(command: dict) -> dict:
    raw = db.command({"explain": command, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        query, hint = attendance_filter(emp_code, date_from, date_to, status)
        collection = "attendance_logs"
        command = {"find": collection, "filter": query, "sort": ATTENDANCE_SORT_DOC,
                   "skip": skip_for(page, page_size), "limit": page_size,
                   "projection": {"_id": 0}, "hint": hint}
    else:
        if endpoint == "employee_monthly":
            plan = monthly_plan(require(emp_code, "emp_code"), require(month, "month"))
        elif endpoint == "department_summary":
            plan = summary_plan(require(month, "month"), department)
        elif endpoint == "late_leaderboard":
            plan = leaderboard_plan(require(month, "month"), limit, department)
        else:  # department_trend
            start, end = trend_range_or_422(require(from_, "from"), require(to, "to"))
            plan = trend_plan(require(department, "department"), start, end)
        collection, pipeline, hint = plan
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}, "hint": hint}
    return {"endpoint": endpoint, "collection": collection, "explain": run_explain(command)}
