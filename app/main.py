"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (fallback to MONGODB_URI)
"""
import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional

from bson import json_util

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI") or os.getenv("MONGODB_URI") or "mongodb://localhost:27017"
MONGO_DB = os.getenv("MONGO_DB") or "attendance_db"

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def create_indexes():
    """Create all required indexes idempotently at startup."""
    # employees indexes
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING)])

    # attendance_logs indexes
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING)])


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    create_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def round_half_up(val: float, decimals: int) -> float:
    """R8 rounding: round half-up."""
    d = Decimal(str(val))
    q = Decimal("10") ** -decimals
    return float(d.quantize(q, rounding=ROUND_HALF_UP))


def serialize_mongo_obj(obj: Any) -> Any:
    """Serialize MongoDB BSON document (Timestamps, ObjectIds, Binary) to plain JSON-compatible types."""
    return json.loads(json_util.dumps(obj))


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert BSON datetime to epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def epoch_ms_to_dt_utc(ms: int) -> datetime:
    """Convert epoch milliseconds to whole-second truncated UTC datetime."""
    truncated_ms = (ms // 1000) * 1000
    return datetime.fromtimestamp(truncated_ms / 1000, tz=timezone.utc)


def compute_attendance_date(punch_in_ist: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1 Time zone and attendance date.
    Overnight shifts (shift_end <= shift_start, e.g. 22:00-06:00):
    a punch-in whose IST time is earlier than shift_end belongs to the shift that started the previous day.
    """
    is_overnight = shift_end <= shift_start
    if is_overnight and punch_in_ist.strftime("%H:%M") < shift_end:
        return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_ist: datetime, shift_start: str, att_date: date) -> int:
    """
    R2 Late:
    Grace period is 10 minutes. A punch-in is late only if it is strictly more than 10:00 after shift_start;
    then late_minutes = whole minutes (floor) elapsed since shift_start - not since the end of the grace period.
    """
    sh, sm = map(int, shift_start.split(":"))
    shift_start_dt = datetime.combine(att_date, time(sh, sm), tzinfo=IST)
    diff_seconds = (punch_in_ist - shift_start_dt).total_seconds()
    if diff_seconds > 600:
        return int(diff_seconds // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4 Work hours:
    (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_seconds = int((punch_out_utc - punch_in_utc).total_seconds())
    hours = diff_seconds / 3600.0
    return round_half_up(hours, 2)


def compute_overtime(punch_out_ist: datetime, shift_start: str, shift_end: str, att_date: date) -> int:
    """
    R3 Overtime:
    Whole minutes (floor) between shift_end and punch_out, counted only when that is >= 30; otherwise 0.
    For overnight shifts, shift_end is on the calendar day after att_date.
    """
    eh, em = map(int, shift_end.split(":"))
    is_overnight = shift_end <= shift_start
    end_date = att_date + timedelta(days=1) if is_overnight else att_date
    shift_end_dt = datetime.combine(end_date, time(eh, em), tzinfo=IST)
    diff_seconds = (punch_out_ist - shift_end_dt).total_seconds()
    minutes = int(diff_seconds // 60)
    return minutes if minutes >= 30 else 0


def format_history_entry_for_api(entry: Dict[str, Any]) -> Dict[str, Any]:
    changes_api = {}
    for k, v in entry.get("changes", {}).items():
        from_val = v.get("from")
        to_val = v.get("to")
        if k in ("punch_in", "punch_out"):
            if isinstance(from_val, datetime):
                from_val = dt_to_epoch_ms(from_val)
            if isinstance(to_val, datetime):
                to_val = dt_to_epoch_ms(to_val)
        changes_api[k] = {"from": from_val, "to": to_val}
    return {
        "at": dt_to_epoch_ms(entry["at"]),
        "by": entry["by"],
        "reason": entry["reason"],
        "changes": changes_api,
    }


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    doc.pop("_id", None)
    doc.pop("id", None)
    punch_in = doc.get("punch_in")
    punch_out = doc.get("punch_out")
    history = doc.get("history") or []

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(punch_in) if isinstance(punch_in, datetime) else punch_in,
        "punch_out": dt_to_epoch_ms(punch_out) if isinstance(punch_out, datetime) else punch_out,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [format_history_entry_for_api(h) for h in history],
    }


def format_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    doc.pop("_id", None)
    doc.pop("id", None)
    created_at = doc.get("created_at")
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(created_at) if isinstance(created_at, datetime) else created_at,
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
StatusType = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatusType = Literal["PRESENT", "WFH", "ON_DUTY"]


class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: PresenceStatusType = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeIn(BaseModel):
    status: Optional[StatusType] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(status_code=422, detail="shift_start must differ from shift_end")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now_utc

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return format_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [format_employee_doc(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        dt_utc = epoch_ms_to_dt_utc(body.punched_at)
    else:
        dt_utc = datetime.now(timezone.utc).replace(microsecond=0)

    dt_ist = dt_utc.astimezone(IST)
    date_str = compute_attendance_date(dt_ist, emp["shift_start"], emp["shift_end"])
    att_date = date.fromisoformat(date_str)
    late_minutes = compute_late_minutes(dt_ist, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": dt_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_utc = epoch_ms_to_dt_utc(body.punched_at)
    else:
        punch_out_utc = datetime.now(timezone.utc).replace(microsecond=0)

    # Closes the employee's most recent record whose punch_in is at or before punched_at
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_utc}},
        sort=[("punch_in", DESCENDING)],
    )

    if not rec:
        raise HTTPException(status_code=404, detail="no punch-in found")

    if rec.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="already punched out")

    punch_in_utc = rec["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    diff_seconds = (punch_out_utc - punch_in_utc).total_seconds()
    if diff_seconds <= 0 or diff_seconds > 86400:
        raise HTTPException(
            status_code=422,
            detail="punched_at must be strictly after punch_in and within 24 hours",
        )

    work_hours = compute_work_hours(punch_in_utc, punch_out_utc)
    half_day = work_hours < 4.50
    att_date = date.fromisoformat(rec["date"])
    punch_out_ist = punch_out_utc.astimezone(IST)
    overtime_minutes = compute_overtime(punch_out_ist, emp["shift_start"], emp["shift_end"], att_date)

    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "half_day": half_day,
                "overtime_minutes": overtime_minutes,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="already punched out")

    return format_attendance_doc(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusType] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be after date_to")

    q: Dict[str, Any] = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if status is not None:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    items = [format_attendance_doc(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date_str}")
def regularize_attendance(emp_code: str, date_str: str, body: RegularizeIn):
    try:
        att_date = date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not rec:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    # Setting status ABSENT or LEAVE clears both punch times, so supplying punch_in or punch_out with it is 422
    if body.status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(
            status_code=422,
            detail="Cannot supply punch_in or punch_out when status is ABSENT or LEAVE",
        )

    final_status = body.status if body.status is not None else rec["status"]

    if final_status in ("ABSENT", "LEAVE"):
        final_punch_in = None
        final_punch_out = None
        final_work_hours = None
        final_late_minutes = 0
        final_overtime_minutes = 0
        final_half_day = False
    else:
        # Presence status
        if body.punch_in is not None:
            final_punch_in = epoch_ms_to_dt_utc(body.punch_in)
        else:
            final_punch_in = rec.get("punch_in")
            if final_punch_in and final_punch_in.tzinfo is None:
                final_punch_in = final_punch_in.replace(tzinfo=timezone.utc)

        if final_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires a punch_in")

        # punch_in must stay on the record's attendance date (R1)
        pi_ist = final_punch_in.astimezone(IST)
        computed_date = compute_attendance_date(pi_ist, emp["shift_start"], emp["shift_end"])
        if computed_date != date_str:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in falls on attendance date {computed_date}, not {date_str}",
            )

        final_late_minutes = compute_late_minutes(pi_ist, emp["shift_start"], att_date)

        if body.punch_out is not None:
            final_punch_out = epoch_ms_to_dt_utc(body.punch_out)
        else:
            final_punch_out = rec.get("punch_out")
            if final_punch_out and final_punch_out.tzinfo is None:
                final_punch_out = final_punch_out.replace(tzinfo=timezone.utc)

        if final_punch_out is not None:
            diff_sec = (final_punch_out - final_punch_in).total_seconds()
            if diff_sec <= 0 or diff_sec > 86400:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be strictly after punch_in and within 24 hours",
                )
            final_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            final_half_day = final_work_hours < 4.50
            po_ist = final_punch_out.astimezone(IST)
            final_overtime_minutes = compute_overtime(po_ist, emp["shift_start"], emp["shift_end"], att_date)
        else:
            final_work_hours = None
            final_overtime_minutes = 0
            final_half_day = False

    # Compute changes
    changes = {}

    def get_orig_time(val):
        if val is None:
            return None
        return val if val.tzinfo is not None else val.replace(tzinfo=timezone.utc)

    orig_punch_in = get_orig_time(rec.get("punch_in"))
    orig_punch_out = get_orig_time(rec.get("punch_out"))

    if rec.get("status") != final_status:
        changes["status"] = {"from": rec.get("status"), "to": final_status}
    if orig_punch_in != final_punch_in:
        changes["punch_in"] = {"from": orig_punch_in, "to": final_punch_in}
    if orig_punch_out != final_punch_out:
        changes["punch_out"] = {"from": orig_punch_out, "to": final_punch_out}
    if rec.get("work_hours") != final_work_hours:
        changes["work_hours"] = {"from": rec.get("work_hours"), "to": final_work_hours}
    if rec.get("late_minutes", 0) != final_late_minutes:
        changes["late_minutes"] = {"from": rec.get("late_minutes", 0), "to": final_late_minutes}
    if rec.get("overtime_minutes", 0) != final_overtime_minutes:
        changes["overtime_minutes"] = {"from": rec.get("overtime_minutes", 0), "to": final_overtime_minutes}
    if rec.get("half_day", False) != final_half_day:
        changes["half_day"] = {"from": rec.get("half_day", False), "to": final_half_day}

    if not changes:
        raise HTTPException(status_code=422, detail="Request made no changes to the record")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    curr_history_len = len(rec.get("history") or [])
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            f"history.{curr_history_len}": {"$exists": False},
        },
        {
            "$set": {
                "status": final_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "work_hours": final_work_hours,
                "late_minutes": final_late_minutes,
                "overtime_minutes": final_overtime_minutes,
                "half_day": final_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Concurrent edit conflict; please retry")

    return format_attendance_doc(updated)


# --------------------------------------------------------------------------- #
# Analytics Pipelines
# --------------------------------------------------------------------------- #
def get_month_range(month: str):
    parts = month.split("-")
    if len(parts) != 2:
        raise HTTPException(status_code=422, detail="Invalid month format, expected YYYY-MM")
    try:
        y, m = int(parts[0]), int(parts[1])
        if not (1 <= m <= 12):
            raise ValueError()
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid month value")
    num_days = calendar.monthrange(y, m)[1]
    return f"{month}-01", f"{month}-{num_days:02d}", num_days


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    m_start, m_end, num_days = get_month_range(month)

    # Compute working_days (Mon-Fri) from max(joined_on, m_start) to m_end
    joined_on = emp["joined_on"]
    y, m = int(month.split("-")[0]), int(month.split("-")[1])
    working_days = 0
    if joined_on <= m_end:
        start_d = max(date(y, m, 1), date.fromisoformat(joined_on))
        end_d = date(y, m, num_days)
        cur = start_d
        while cur <= end_d:
            if cur.weekday() < 5:  # Monday to Friday
                working_days += 1
            cur += timedelta(days=1)

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": m_start, "$lte": m_end}}},
        {"$addFields": {"date_obj": {"$dateFromString": {"dateString": "$date"}}}},
        {"$addFields": {"day_of_week": {"$isoDayOfWeek": "$date_obj"}}},
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$lte": ["$day_of_week", 5]},
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]

    agg_res = list(db.attendance_logs.aggregate(pipeline))
    if agg_res:
        r = agg_res[0]
        present_days = float(r["present_days"])
        leave_days = int(r["leave_days"])
        late_count = int(r["late_count"])
        total_late_minutes = int(r["total_late_minutes"])
        total_overtime_minutes = int(r["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    attendance_pct = None
    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100.0, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(m_start: str, m_end: str, department: Optional[str] = None):
    match_emp: Dict[str, Any] = {"joined_on": {"$lte": m_end}}
    if department is not None:
        match_emp["department"] = department

    return [
        {"$match": match_emp},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", m_start]},
                                    {"$lte": ["$date", m_end]},
                                ]
                            }
                        }
                    },
                    {"$addFields": {"date_obj": {"$dateFromString": {"dateString": "$date"}}}},
                    {"$addFields": {"day_of_week": {"$isoDayOfWeek": "$date_obj"}}},
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "emp_codes": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$lte": ["$logs.day_of_week", 5]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            0.0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$gt": ["$logs.late_minutes", 0]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {"$ne": ["$logs", None]},
                            {"$ifNull": ["$logs.late_minutes", 0]},
                            0,
                        ]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "LEAVE"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "ON_DUTY"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {"$sort": {"_id": ASCENDING}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$emp_codes"},
                "present_days": 1,
                "work_hours_sum": 1,
                "work_hours_count": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    m_start, m_end, _ = get_month_range(month)
    pipeline = build_department_summary_pipeline(m_start, m_end, department)
    results = list(db.employees.aggregate(pipeline))

    items = []
    for r in results:
        headcount = r["headcount"]
        if headcount == 0:
            continue
        wh_cnt = r.pop("work_hours_count", 0)
        wh_sum = r.pop("work_hours_sum", 0.0)
        avg_wh = round_half_up(wh_sum / wh_cnt, 2) if wh_cnt > 0 else None
        items.append({
            "department": r["department"],
            "headcount": headcount,
            "present_days": float(r["present_days"]),
            "avg_work_hours": avg_wh,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })

    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(m_start: str, m_end: str, limit: int, department: Optional[str] = None):
    pipeline: List[Dict[str, Any]] = [
        {"$match": {"date": {"$gte": m_start, "$lte": m_end}, "late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": DESCENDING},
                "output": {
                    "rank": {"$rank": {}},
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
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
    ])
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    m_start, m_end, _ = get_month_range(month)
    pipeline = build_late_leaderboard_pipeline(m_start, m_end, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


def build_department_trend_pipeline(department: str, from_str: str, to_str: str):
    from_dt = datetime.fromisoformat(from_str).replace(tzinfo=timezone.utc)
    to_dt = datetime.fromisoformat(to_str).replace(tzinfo=timezone.utc)
    bounds_upper = to_dt + timedelta(days=1)

    dept_emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1}))
    emp_codes = [e["emp_code"] for e in dept_emps]

    return [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_str, "$lte": to_str},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0],
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, bounds_upper],
                },
            }
        },
        {
            "$addFields": {
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
                "day_of_week": {"$isoDayOfWeek": "$date"},
            }
        },
        {
            "$addFields": {
                "is_working_day": {"$lte": ["$day_of_week", 5]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"d": "$date_str"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$d"]},
                                ]
                            }
                        }
                    },
                    {"$count": "hc"},
                ],
                "as": "hc_arr",
            }
        },
        {
            "$addFields": {
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc_arr.hc", 0]}, 0]},
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": ASCENDING},
                "output": {
                    "moving_avg_7d_raw": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
        {
            "$addFields": {
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d_raw", None]},
                        {"$round": ["$moving_avg_7d_raw", 4]},
                        None,
                    ]
                }
            }
        },
        {"$sort": {"date": ASCENDING}},
        {
            "$project": {
                "_id": 0,
                "date": "$date_str",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
):
    if not db.employees.find_one({"department": department}):
        raise HTTPException(status_code=404, detail="Department not found")

    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    if d_to < d_from:
        raise HTTPException(status_code=422, detail="to date cannot be earlier than from date")
    if (d_to - d_from).days > 92:
        raise HTTPException(status_code=422, detail="Date range exceeds 92 days maximum")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    items = list(db.attendance_logs.aggregate(pipeline))

    # Apply R8 half-up rounding explicitly
    for item in items:
        if item["attendance_rate"] is not None:
            item["attendance_rate"] = round_half_up(item["attendance_rate"], 4)
        if item["moving_avg_7d"] is not None:
            item["moving_avg_7d"] = round_half_up(item["moving_avg_7d"], 4)

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
ExplainEndpointType = Literal[
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
]


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainEndpointType,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: Optional[int] = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusType] = None,
    from_param: Optional[str] = Query(None, alias="from"),
    to_param: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from cannot be after date_to")

        q: Dict[str, Any] = {}
        if emp_code is not None:
            q["emp_code"] = emp_code
        if status is not None:
            q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to

        skip = (page - 1) * page_size
        command = {
            "explain": {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        }
        res = db.command(command)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": serialize_mongo_obj(res)}

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(status_code=422, detail="emp_code is required for employee_monthly")
        if not month:
            raise HTTPException(status_code=422, detail="month is required for employee_monthly")

        m_start, m_end, _ = get_month_range(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": m_start, "$lte": m_end}}},
            {"$addFields": {"date_obj": {"$dateFromString": {"dateString": "$date"}}}},
            {"$addFields": {"day_of_week": {"$isoDayOfWeek": "$date_obj"}}},
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$lte": ["$day_of_week", 5]},
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    ]
                                },
                                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                }
            },
        ]
        command = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(command)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": serialize_mongo_obj(res)}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")
        m_start, m_end, _ = get_month_range(month)
        pipeline = build_department_summary_pipeline(m_start, m_end, department)
        command = {
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(command)
        return {"endpoint": endpoint, "collection": "employees", "explain": serialize_mongo_obj(res)}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")
        m_start, m_end, _ = get_month_range(month)
        pipeline = build_late_leaderboard_pipeline(m_start, m_end, limit or 10, department)
        command = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(command)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": serialize_mongo_obj(res)}

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(status_code=422, detail="department is required for department_trend")
        if not from_param or not to_param:
            raise HTTPException(status_code=422, detail="from and to are required for department_trend")

        pipeline = build_department_trend_pipeline(department, from_param, to_param)
        command = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(command)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": serialize_mongo_obj(res)}

    raise HTTPException(status_code=422, detail="Unknown explain endpoint")
