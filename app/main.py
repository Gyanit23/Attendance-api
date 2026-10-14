"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded, but real environment variables win)

Layout of this file
  1. configuration + Mongo client
  2. pure helpers: time zone, business rules R1-R5, rounding   (no I/O, easy to unit-test)
  3. request models + response serialisers
  4. index creation (runs at startup, idempotent)
  5. aggregation / query builders (shared by the real endpoints AND /admin/explain)
  6. endpoints
"""
import calendar
import json
import logging
import os
import re
from bisect import bisect_right
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Configuration
# --------------------------------------------------------------------------- #
load_dotenv()  # override=False (default): real environment variables take priority over .env

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

# tz_aware=True: PyMongo returns UTC-aware datetimes instead of naive ones, so we never mix
# naive and aware values (the starter crashed on exactly that).
client = MongoClient(
    MONGO_URI, tz_aware=True, tzinfo=UTC, serverSelectionTimeoutMS=5000, appname="attendance-api"
)
db = client[MONGO_DB]

# --------------------------------------------------------------------------- #
# 2. Pure helpers  (stdlib only; kept between the markers so they can be tested alone)
# --------------------------------------------------------------------------- #
# <<HELPERS-START>>
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MS_MIN, MS_MAX = 100_000_000_000, 4_102_444_800_000   # contract: below MS_MIN looks like seconds
GRACE_SECONDS = 10 * 60                               # R2
OVERTIME_MIN_MINUTES = 30                             # R3
HALF_DAY_LIMIT = Decimal("4.50")                      # R5
MAX_SHIFT_SECONDS = 24 * 3600                         # punch-out / regularize window
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")              # R6
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def as_utc(dt: datetime) -> datetime:
    """Treat a naive datetime as UTC (BSON dates are UTC) and return an aware one."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def dt_to_ms(dt: datetime) -> int:
    """UTC datetime -> epoch ms as an exact integer (no float round-trip)."""
    return (as_utc(dt) - EPOCH) // timedelta(milliseconds=1)


def opt_ms(dt: Optional[datetime]) -> Optional[int]:
    return None if dt is None else dt_to_ms(dt)


def now_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def now_millis() -> datetime:
    n = datetime.now(UTC)
    return n.replace(microsecond=(n.microsecond // 1000) * 1000)  # BSON keeps milliseconds


def hhmm_to_seconds(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 3600 + int(m) * 60


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hhmm_to_seconds(shift_end) <= hhmm_to_seconds(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    """R1: IST calendar date of the punch-in; overnight shifts before shift_end belong to the previous day."""
    local = as_utc(punch_in).astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end):
        seconds_of_day = local.hour * 3600 + local.minute * 60 + local.second
        if seconds_of_day < hhmm_to_seconds(shift_end):
            day -= timedelta(days=1)
    return day


def _at(day: date, hhmm: str) -> datetime:
    h, m = hhmm.split(":")
    return datetime.combine(day, time(int(h), int(m)), tzinfo=IST)


def compute_late_minutes(punch_in: datetime, day: date, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift_start; then floor(minutes since shift_start)."""
    delta = int((as_utc(punch_in) - _at(day, shift_start)).total_seconds())
    return delta // 60 if delta > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds/3600 rounded to 2 decimals HALF-UP, done in Decimal (not round(), not float)."""
    seconds = int((as_utc(punch_out) - as_utc(punch_in)).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, day: date, shift_start: str, shift_end: str) -> int:
    """R3: floor(minutes after shift_end), only if >= 30. Overnight shifts end on the next calendar day."""
    end = _at(day, shift_end)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    delta = int((as_utc(punch_out) - end).total_seconds())
    minutes = delta // 60 if delta > 0 else 0
    return minutes if minutes >= OVERTIME_MIN_MINUTES else 0


def compute_half_day(work_hours: float) -> bool:
    """R5: judged on the ROUNDED work_hours."""
    return Decimal(repr(work_hours)) < HALF_DAY_LIMIT


def derive_fields(status, punch_in, punch_out, day, shift_start, shift_end) -> dict:
    """Single place that produces work_hours / late / overtime / half_day (used by punch-in/out and PATCH)."""
    if status not in PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, day, shift_start)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(punch_out, day, shift_start, shift_end),
        "half_day": compute_half_day(hours),
    }


def half_up(value, places: int) -> float:
    """R8: half-up rounding of an exact Decimal (or int/str) value."""
    return float(Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def count_weekdays(start: date, end: date) -> int:
    """Mon-Fri days in [start, end] (R7). At most 31 iterations - this is calendar maths, not data."""
    n, d = 0, start
    while d <= end:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n
# <<HELPERS-END>>


def invalid(loc, msg: str) -> HTTPException:
    """A 422 shaped like FastAPI's own validation error."""
    return HTTPException(422, detail=[{"type": "value_error", "loc": list(loc), "msg": msg, "input": None}])


def parse_date_param(value: str, name: str, where: str = "query") -> date:
    if not DATE_RE.fullmatch(value or ""):
        raise invalid((where, name), "must be a valid date in YYYY-MM-DD format")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise invalid((where, name), "must be a valid calendar date")


def month_bounds(month: str) -> tuple[date, date]:
    try:
        y, m = int(month[:4]), int(month[5:7])
        return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
    except ValueError:
        raise invalid(("query", "month"), "must be a valid month in YYYY-MM format")


# --------------------------------------------------------------------------- #
# 3. Models and serialisers
# --------------------------------------------------------------------------- #
# strict=True: floats ("1.0"), strings and bools are rejected with 422; ge/le reject "seconds".
EpochMs = Annotated[int, Field(strict=True, ge=MS_MIN, le=MS_MAX)]
Hhmm = Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
MAX_PAGE = 10_000_000  # keeps skip inside int64


def _not_blank(v: str) -> str:
    if not v.strip():
        raise ValueError("must not be blank")
    return v


class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: Hhmm = "09:30"
    shift_end: Hhmm = "18:30"
    joined_on: str

    check_blank = field_validator("name", "department")(_not_blank)

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        if not DATE_RE.fullmatch(v):
            raise ValueError("must be YYYY-MM-DD")
        date.fromisoformat(v)  # ValueError -> 422 (e.g. 2026-02-30)
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeRequest(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    check_blank = field_validator("reason", "regularized_by")(_not_blank)

    @model_validator(mode="before")
    @classmethod
    def _no_explicit_null(cls, data):
        # "omitted = unchanged" - an explicit null is ambiguous, so reject it instead of guessing.
        if isinstance(data, dict):
            for key in ("status", "punch_in", "punch_out"):
                if key in data and data[key] is None:
                    raise ValueError(f"{key} must not be null; omit it to leave it unchanged")
        return data


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": opt_ms(doc.get("created_at")),
    }


def _history_out(entry: dict) -> dict:
    changes = {}
    for field, ft in (entry.get("changes") or {}).items():
        a, b = ft.get("from"), ft.get("to")
        if field in ("punch_in", "punch_out"):  # dates nested inside the object: easy to forget
            a, b = opt_ms(a), opt_ms(b)
        changes[field] = {"from": a, "to": b}
    return {"at": opt_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def record_out(doc: dict) -> dict:
    """Mongo document -> API record. Never exposes _id; fills defaults for legacy documents."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": opt_ms(doc.get("punch_in")),
        "punch_out": opt_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [_history_out(h) for h in (doc.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# 4. Indexes (created at startup; create_index is idempotent)
# --------------------------------------------------------------------------- #
_indexes_ready = False


def ensure_indexes() -> None:
    global _indexes_ready
    emp, logs = db.employees, db.attendance_logs
    # employees
    emp.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    emp.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code")
    emp.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="joined_dept")
    # attendance_logs
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_emp_date")
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp")
    logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp"
    )
    logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in")
    logs.create_index(
        [("date", ASCENDING), ("emp_code", ASCENDING), ("late_minutes", ASCENDING)],
        name="late_partial",
        partialFilterExpression={"late_minutes": {"$gt": 0}},
    )
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # keep the process up; /health reports 503 until Mongo is back
        log.error("index creation failed at startup: %s", exc)
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_: Request, exc: PyMongoError):
    log.exception("mongo error")
    return JSONResponse(status_code=503, content={"detail": "database unavailable or failed"})


# --------------------------------------------------------------------------- #
# 5. Query / pipeline builders  (the real endpoints and /admin/explain both call these)
# --------------------------------------------------------------------------- #
ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]

# Expressions reused inside pipelines. Stored derived values are authoritative, so we only READ them.
IS_PRESENT = {"$in": ["$status", list(PRESENCE)]}
IS_WEEKDAY = {
    "$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 5]
}
IS_HALF = {"$eq": [{"$ifNull": ["$half_day", False]}, True]}
# present weight x2 so everything stays an exact integer: full day = 2, half day = 1
PRESENT2 = {"$cond": [IS_PRESENT, {"$cond": [IS_HALF, 1, 2]}, 0]}
PRESENT2_WEEKDAY = {"$cond": [{"$and": [IS_PRESENT, IS_WEEKDAY]}, {"$cond": [IS_HALF, 1, 2]}, 0]}
LATE_MIN = {"$ifNull": ["$late_minutes", 0]}
IS_LATE = {"$gt": [LATE_MIN, 0]}


def build_attendance_filter(emp_code, date_from, date_to, status) -> dict:
    q: dict[str, Any] = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    d_from = parse_date_param(date_from, "date_from") if date_from is not None else None
    d_to = parse_date_param(date_to, "date_to") if date_to is not None else None
    if d_from and d_to and d_from > d_to:
        raise invalid(("query", "date_from"), "date_from must not be after date_to")
    if d_from or d_to:
        q["date"] = {}
        if d_from:
            q["date"]["$gte"] = d_from.isoformat()
        if d_to:
            q["date"]["$lte"] = d_to.isoformat()
    if status is not None:
        q["status"] = status
    return q


def pipeline_employee_monthly(emp_code: str, first: date, last: date) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {
            "$group": {
                "_id": None,
                "present2": {"$sum": PRESENT2_WEEKDAY},  # R6 + R7: Mon-Fri only, half day = 0.5
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [IS_LATE, 1, 0]}},  # all records, weekends too
                "total_late": {"$sum": LATE_MIN},
                "total_overtime": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def pipeline_department_summary(first: date, last: date, department: Optional[str]) -> list:
    """Driven from `employees` so people with zero logs still produce a row (R9); logs are joined in."""
    emp_match: dict[str, Any] = {"joined_on": {"$lte": last.isoformat()}}  # R9: joined on/before month end
    if department is not None:
        emp_match["department"] = department
    has_hours = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}
    return [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                # per-employee pre-aggregation: at most ONE row comes back per employee
                "pipeline": [
                    {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                    {
                        "$group": {
                            "_id": None,
                            "present2": {"$sum": PRESENT2_WEEKDAY},
                            # work hours as integer hundredths so the later average is exact
                            "hours_cents": {
                                "$sum": {"$cond": [has_hours, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]}
                            },
                            "hours_n": {"$sum": {"$cond": [has_hours, 1, 0]}},
                            "late_count": {"$sum": {"$cond": [IS_LATE, 1, 0]}},
                            "total_late": {"$sum": LATE_MIN},
                            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                        }
                    },
                ],
                "as": "m",
            }
        },
        {"$set": {"m": {"$arrayElemAt": ["$m", 0]}}},  # employee without logs -> m is missing
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},  # one per employee, logs or not
                "present2": {"$sum": {"$ifNull": ["$m.present2", 0]}},
                "hours_cents": {"$sum": {"$ifNull": ["$m.hours_cents", 0]}},
                "hours_n": {"$sum": {"$ifNull": ["$m.hours_n", 0]}},
                "late_count": {"$sum": {"$ifNull": ["$m.late_count", 0]}},
                "total_late": {"$sum": {"$ifNull": ["$m.total_late", 0]}},
                "leave_count": {"$sum": {"$ifNull": ["$m.leave_count", 0]}},
                "on_duty_count": {"$sum": {"$ifNull": ["$m.on_duty_count", 0]}},
            }
        },
        {"$sort": {"_id": 1}},
    ]


def pipeline_late_leaderboard(first: date, last: date, limit: int, department: Optional[str]) -> list:
    p: list[dict] = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs whose employee does not exist disappear here
    ]
    if department is not None:
        p.append({"$match": {"emp.department": department}})  # rank inside the department only
    p += [
        # $rank = standard competition ranking (1,2,2,4); the array position is never used as a rank
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit AFTER ranking, so a tie at the cutoff stays together
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return p


def load_department(department: str) -> tuple[list[str], list[str]]:
    """(emp_codes, sorted joined_on strings) of a department. Index: dept_emp_code."""
    docs = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    return [d["emp_code"] for d in docs], sorted(d["joined_on"] for d in docs)


def pipeline_department_trend(codes: list[str], joined: list[str], first: date, last: date) -> list:
    lo = datetime(first.year, first.month, first.day, tzinfo=UTC)
    hi = datetime(last.year, last.month, last.day, tzinfo=UTC) + timedelta(days=1)  # upper bound is exclusive
    rate_ok = {"$isNumber": "$rate_sc"}
    window = {"documents": [-6, 0]}  # current row + 6 preceding rows, only rows inside the range exist
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {"_id": "$date", "pc2": {"$sum": PRESENT2}, "late": {"$sum": {"$cond": [IS_LATE, 1, 0]}}}},
        {"$set": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$sort": {"d": 1}},
        # gap filling: one row per calendar day, weekends and empty days included
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [lo, hi]}}},
        {
            "$set": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}},
                "pc2": {"$ifNull": ["$pc2", 0]},
                "late": {"$ifNull": ["$late", 0]},
            }
        },
        {
            "$set": {
                "dow": {"$isoDayOfWeek": "$d"},
                # R9: employees of the department with joined_on <= that day
                "hc": {"$size": {"$filter": {"input": {"$literal": joined}, "as": "j", "cond": {"$lte": ["$$j", "$date"]}}}},
            }
        },
        {
            "$set": {
                # rate x 10000 rounded HALF-UP using integer maths only: floor((pc2*10000 + hc) / (2*hc))
                "rate_sc": {
                    "$cond": [
                        {"$and": [{"$lte": ["$dow", 5]}, {"$gt": ["$hc", 0]}]},
                        {"$trunc": [{"$divide": [{"$add": [{"$multiply": ["$pc2", 10000]}, "$hc"]}, {"$multiply": [2, "$hc"]}]}, 0]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"d": 1},
                "output": {
                    "sum_r": {"$sum": {"$ifNull": ["$rate_sc", 0]}, "window": window},
                    "n_r": {"$sum": {"$cond": [rate_ok, 1, 0]}, "window": window},
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": {"$lte": ["$dow", 5]},
                "headcount": "$hc",
                "present_count": {"$divide": ["$pc2", 2]},
                "late_count": "$late",
                "attendance_rate": {"$cond": [rate_ok, {"$divide": ["$rate_sc", 10000]}, None]},
                "moving_avg_7d": {
                    "$cond": [
                        {"$gt": ["$n_r", 0]},
                        {
                            "$divide": [
                                {"$trunc": [{"$divide": [{"$add": [{"$multiply": ["$sum_r", 2]}, "$n_r"]}, {"$multiply": ["$n_r", 2]}]}, 0]},
                                10000,
                            ]
                        },
                        None,
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


def trend_without_logs(joined: list[str], first: date, last: date) -> list[dict]:
    """Defensive fallback for a range that contains no log at all (<= 92 rows, no data scan)."""
    rows, rates = [], []
    for i in range((last - first).days + 1):
        d = first + timedelta(days=i)
        hc = bisect_right(joined, d.isoformat())
        working = d.weekday() < 5
        rate = 0.0 if working and hc > 0 else None
        rates.append(rate)
        has_rate = any(r is not None for r in rates[-7:])
        rows.append({"date": d.isoformat(), "is_working_day": working, "headcount": hc, "present_count": 0.0,
                     "late_count": 0, "attendance_rate": rate, "moving_avg_7d": 0.0 if has_rate else None})
    return rows


def run_explain(collection: str, *, find: Optional[dict] = None, pipeline: Optional[list] = None) -> dict:
    if pipeline is not None:
        inner = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    else:
        inner = {"find": collection, **(find or {})}
    out = db.command({"explain": inner, "verbosity": "executionStats"})
    # raw explain contains BSON types (Timestamp, Int64, dates) -> convert to plain JSON
    return json.loads(json_util.dumps(out, json_options=json_util.RELAXED_JSON_OPTIONS))


# --------------------------------------------------------------------------- #
# 6. Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB is not reachable")
    if not _indexes_ready:  # Mongo was down at startup
        try:
            ensure_indexes()
        except PyMongoError:
            log.exception("index creation failed")
    return {"status": "ok"}


# ---- employees ------------------------------------------------------------ #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = now_millis()
    try:
        db.employees.insert_one(doc)  # the UNIQUE index decides; no check-then-insert race
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = Query(None),
    page: int = Query(1, ge=1, le=MAX_PAGE),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {} if department is None else {"department": department}
    total = db.employees.count_documents(q)  # counts the FILTERED set
    cur = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


# ---- attendance ----------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_seconds()
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive_fields(body.status, ts, None, day, emp["shift_start"], emp["shift_end"]),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one concurrent insert wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_seconds()
    # most recent record whose punch_in <= punched_at (works across midnight: it is not date-based)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    p_in = as_utc(rec["punch_in"])
    seconds = (ts - p_in).total_seconds()
    if seconds <= 0 or seconds > MAX_SHIFT_SECONDS:
        raise invalid(("body", "punched_at"), "must be after punch_in and within 24 hours of it")
    d = derive_fields(rec["status"], p_in, ts, date.fromisoformat(rec["date"]), emp["shift_start"], emp["shift_end"])
    # atomic guard `punch_out: None`: of two simultaneous punch-outs only one matches
    updated = db.attendance_logs.find_one_and_update(
        {"emp_code": rec["emp_code"], "date": rec["date"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": d["work_hours"], "overtime_minutes": d["overtime_minutes"],
                  "half_day": d["half_day"]}},
        return_document=ReturnDocument.AFTER,
        projection={"_id": 0},
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[Status] = Query(None),
    page: int = Query(1, ge=1, le=MAX_PAGE),
    page_size: int = Query(20, ge=1, le=100),
):
    q = build_attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cur = db.attendance_logs.find(q, {"_id": 0}).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [record_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


_REG_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
_REG_DEFAULTS = {"status": None, "punch_in": None, "punch_out": None, "work_hours": None,
                 "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
_MAX_PATCH_ATTEMPTS = 5


def _optimistic_guard(rec: dict) -> dict:
    """Filter that only matches while the document is exactly as we read it."""
    g: dict[str, Any] = {"emp_code": rec["emp_code"], "date": rec["date"]}
    for f in _REG_FIELDS:
        g[f] = rec[f] if f in rec else {"$exists": False}
    h = rec.get("history")
    g["history"] = {"$size": len(h)} if isinstance(h, list) else {"$exists": False}
    return g


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeRequest):  # noqa: A002 - name fixed by the contract
    day = parse_date_param(date, "date", "path")
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    shift_start, shift_end = emp["shift_start"], emp["shift_end"]

    for _ in range(_MAX_PATCH_ATTEMPTS):
        rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": day.isoformat()})
        if rec is None:
            raise HTTPException(404, "no attendance record for that date")

        status = body.status or rec["status"]
        if status in ("ABSENT", "LEAVE"):
            if body.punch_in is not None or body.punch_out is not None:
                raise invalid(("body",), "ABSENT/LEAVE clear the punch times; do not supply punch_in/punch_out")
            p_in = p_out = None
        else:
            p_in = ms_to_dt(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
            p_out = ms_to_dt(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
            if p_in is None:
                raise invalid(("body", "punch_in"), "a presence status requires a punch_in")
            p_in = as_utc(p_in)
            if p_out is not None:
                p_out = as_utc(p_out)
            if body.punch_in is not None and attendance_date(p_in, shift_start, shift_end) != day:
                raise invalid(("body", "punch_in"), "punch_in must stay on the record's attendance date")
            if p_out is not None and not (0 < (p_out - p_in).total_seconds() <= MAX_SHIFT_SECONDS):
                raise invalid(("body", "punch_out"), "punch_out must be after punch_in and within 24 hours")

        new = {"status": status, "punch_in": p_in, "punch_out": p_out,
               **derive_fields(status, p_in, p_out, day, shift_start, shift_end)}
        old = {f: rec.get(f, _REG_DEFAULTS[f]) for f in _REG_FIELDS}
        for f in ("late_minutes", "overtime_minutes"):  # legacy docs may hold null: that means 0
            old[f] = old[f] or 0
        old["half_day"] = bool(old["half_day"])
        changes = {}
        for f in _REG_FIELDS:
            a, b = old[f], new[f]
            if isinstance(a, datetime):
                a = as_utc(a)
            if a != b:
                changes[f] = {"from": a, "to": b}  # datetimes stay BSON dates in storage
        if not changes:
            raise invalid(("body",), "the request changes nothing")

        entry = {"at": now_millis(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
        # compare-and-set: if anyone touched the record since we read it this matches nothing -> re-read & retry
        updated = db.attendance_logs.find_one_and_update(
            _optimistic_guard(rec),
            {"$set": new, "$push": {"history": entry}},  # $push never overwrites earlier history
            return_document=ReturnDocument.AFTER,
            projection={"_id": 0},
        )
        if updated is not None:
            return record_out(updated)
    raise HTTPException(409, "record was modified concurrently; please retry")


# ---- analytics ------------------------------------------------------------ #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=MONTH_PATTERN)):
    first, last = month_bounds(month)
    emp = db.employees.find_one({"emp_code": emp_code}, {"joined_on": 1})
    if emp is None:
        raise HTTPException(404, "employee not found")
    # R7: working days are counted from joined_on for mid-month joiners (0 if they join after the month)
    working_days = count_weekdays(max(first, date.fromisoformat(emp["joined_on"])), last)
    agg = next(iter(db.attendance_logs.aggregate(pipeline_employee_monthly(emp_code, first, last))), None) or {}
    present2 = int(agg.get("present2", 0))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present2 / 2,
        "leave_days": int(agg.get("leave_days", 0)),
        "late_count": int(agg.get("late_count", 0)),
        "total_late_minutes": int(agg.get("total_late", 0)),
        "total_overtime_minutes": int(agg.get("total_overtime", 0)),
        "attendance_pct": None if working_days == 0 else half_up(Decimal(present2) * 50 / Decimal(working_days), 2),
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(pattern=MONTH_PATTERN), department: Optional[str] = Query(None)):
    first, last = month_bounds(month)
    items = []
    for r in db.employees.aggregate(pipeline_department_summary(first, last, department)):
        n = int(r["hours_n"])
        avg = None if n == 0 else half_up(Decimal(int(r["hours_cents"])) / Decimal(100 * n), 2)
        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": int(r["present2"]) / 2,
            "avg_work_hours": avg,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    first, last = month_bounds(month)
    rows = db.attendance_logs.aggregate(pipeline_late_leaderboard(first, last, limit, department))
    items = [
        {"rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"], "department": r["department"],
         "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"])}
        for r in rows
    ]
    return {"month": month, "items": items}


def _trend_range(from_: str, to: str) -> tuple[date, date]:
    d_from, d_to = parse_date_param(from_, "from"), parse_date_param(to, "to")
    if d_to < d_from:
        raise invalid(("query", "to"), "to must not be before from")
    if (d_to - d_from).days + 1 > 92:
        raise invalid(("query", "to"), "range must not exceed 92 days")
    return d_from, d_to


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(alias="from"), to: str = Query()):
    first, last = _trend_range(from_, to)
    codes, joined = load_department(department)
    if not codes:
        raise HTTPException(404, "unknown department")
    rows = list(db.attendance_logs.aggregate(pipeline_department_trend(codes, joined, first, last)))
    if not rows:  # no log at all in the range
        rows = trend_without_logs(joined, first, last)
    return {"department": department, "items": rows}


# ---- admin / explain ------------------------------------------------------ #
def _require(**params) -> None:
    for name, value in params.items():
        if value is None:
            raise invalid(("query", name), "field required for this endpoint")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[Status] = Query(None),
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None),
    page: int = Query(1, ge=1, le=MAX_PAGE),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        coll = "attendance_logs"
        out = run_explain(coll, find={
            "filter": build_attendance_filter(emp_code, date_from, date_to, status),
            "sort": dict(ATTENDANCE_SORT), "skip": (page - 1) * page_size, "limit": page_size, "projection": {"_id": 0},
        })
    elif endpoint == "employee_monthly":
        _require(emp_code=emp_code, month=month)
        first, last = month_bounds(month)
        coll = "attendance_logs"
        out = run_explain(coll, pipeline=pipeline_employee_monthly(emp_code, first, last))
    elif endpoint == "department_summary":
        _require(month=month)
        first, last = month_bounds(month)
        coll = "employees"  # the pipeline starts on employees and $lookups into attendance_logs
        out = run_explain(coll, pipeline=pipeline_department_summary(first, last, department))
    elif endpoint == "late_leaderboard":
        _require(month=month)
        first, last = month_bounds(month)
        coll = "attendance_logs"
        out = run_explain(coll, pipeline=pipeline_late_leaderboard(first, last, limit, department))
    else:  # department_trend
        _require(department=department, **{"from": from_, "to": to})
        first, last = _trend_range(from_, to)
        codes, joined = load_department(department)
        if not codes:
            raise HTTPException(404, "unknown department")
        coll = "attendance_logs"
        out = run_explain(coll, pipeline=pipeline_department_trend(codes, joined, first, last))
    return {"endpoint": endpoint, "collection": coll, "explain": out}
