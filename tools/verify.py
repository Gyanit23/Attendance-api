"""
Local test helper (NOT part of the submission - keep it out of the git repo or delete it).

  python tools/verify.py seed        # wipe DB, load edge cases + ~100k attendance records
  uvicorn app.main:app --port 8000   # (start the app AFTER seeding, like the grader does)
  python tools/verify.py check       # call the API and compare against an independent Python oracle

Env: MONGO_URI, MONGO_DB (same as the app), API_URL (default http://localhost:8000)
"""
import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRES = ("PRESENT", "WFH", "ON_DUTY")
MONTH = "2026-07"  # July 2026: 1st is a Wednesday


# ------------------------------------------------------------------ data builders (pure) ---
def ist(day: str, hm: str, plus_days: int = 0) -> datetime:
    y, m, d = map(int, day.split("-"))
    h, mi = map(int, hm.split(":"))
    return (datetime(y, m, d, h, mi, tzinfo=IST) + timedelta(days=plus_days)).astimezone(UTC)


def emp(code, dept, joined, start="09:30", end="18:30"):
    return {"emp_code": code, "name": f"Name {code}", "email": f"{code.lower()}@example.com", "department": dept,
            "shift_start": start, "shift_end": end, "joined_on": joined,
            "created_at": datetime(2026, 1, 1, tzinfo=UTC)}


def rec(code, day, status="PRESENT", pin=None, pout=None, out_next_day=False, late=0, ot=0, half=False, hist=True):
    """Hand-specified record: late/ot are given explicitly so edge cases are exact."""
    p_in = ist(day, pin) if pin else None
    p_out = ist(day, pout, 1 if out_next_day else 0) if pout else None
    wh = round((p_out - p_in).total_seconds() / 3600, 2) if (p_in and p_out) else None
    d = {"emp_code": code, "date": day, "status": status, "punch_in": p_in, "punch_out": p_out, "work_hours": wh,
         "late_minutes": late, "overtime_minutes": ot, "half_day": half}
    if hist:
        d["history"] = []
    return d


def build_edge():
    emps = [
        emp("EMP9001", "Eng", "2026-01-05"),
        emp("EMP9002", "Eng", "2026-07-13"),            # joins mid-month
        emp("EMP9003", "Sales", "2026-02-01"),
        emp("EMP9004", "Sales", "2026-02-01"),          # no logs at all
        emp("EMP9005", "Ops", "2026-03-10", "22:00", "06:00"),  # overnight
        emp("EMP9006", "Ops", "2026-08-03"),            # joins AFTER the month
        emp("EMP9007", "Eng", "2026-01-05"),
        emp("EMP9008", "QA", "2026-01-01"),             # department where nobody has logs
    ]
    logs = [
        rec("EMP9001", "2026-07-01", pin="09:30", pout="18:30"),
        rec("EMP9001", "2026-07-02", pin="09:50", pout="18:30", late=20),
        rec("EMP9001", "2026-07-03", pin="10:00", pout="19:10", late=30, ot=40),
        rec("EMP9001", "2026-07-04", pin="09:50", pout="14:00", late=20),          # Saturday record
        rec("EMP9001", "2026-07-06", pin="09:30", pout="13:00", half=True),
        rec("EMP9001", "2026-07-07", status="LEAVE"),
        rec("EMP9001", "2026-07-08", status="ABSENT"),                              # 07-09 intentionally missing
        rec("EMP9007", "2026-07-01", pin="10:10", pout="18:30", late=40),
        rec("EMP9007", "2026-07-02", pin="10:00", pout="18:30", late=30),
        rec("EMP9003", "2026-07-01", pin="09:55", pout="18:30", late=25),
        rec("EMP9003", "2026-07-02", status="WFH", pin="09:30", pout="18:30"),
        rec("EMP9002", "2026-07-13", pin="09:55", pout="18:30", late=25),
        rec("EMP9002", "2026-07-14", status="ON_DUTY", pin="09:30", pout="18:30"),
        rec("EMP9005", "2026-07-06", pin="22:05", pout="06:40", out_next_day=True, late=0, ot=40),
        rec("EMP9005", "2026-07-07", pin="22:20", pout="06:10", out_next_day=True, late=20, ot=0),
        rec("EMP9999", "2026-07-01", pin="12:00", pout="18:30", late=500),        # orphan log (no employee)
    ]
    legacy = rec("EMP9007", "2026-07-15", pin="09:30", pout="18:30", hist=False)  # no half_day/history
    del legacy["half_day"]
    logs.append(legacy)
    return emps, logs


def build_bulk(n_emps=500, seed=7):
    rnd = random.Random(seed)
    depts = ["Engineering", "Sales", "Support", "HR", "Finance"]
    emps, logs = [], []
    for i in range(n_emps):
        code = f"EMP{1000 + i}"
        emps.append(emp(code, depts[i % 5], "2026-01-01"))
        d = date(2026, 1, 1)
        while d <= date(2026, 9, 30):
            if d.weekday() < 5 or rnd.random() < 0.02:
                ds, r = d.isoformat(), rnd.random()
                if r < 0.03:
                    logs.append(rec(code, ds, status="ABSENT"))
                elif r < 0.06:
                    logs.append(rec(code, ds, status="LEAVE"))
                else:
                    status = "WFH" if r < 0.12 else "ON_DUTY" if r < 0.15 else "PRESENT"
                    pin_min = 9 * 60 + 30 + rnd.choice([-20, -5, 0, 5, 10, 11, 25, 45])
                    dur = rnd.choice([3.5 * 60, 8 * 60, 9 * 60, 9 * 60 + 40, 10 * 60])
                    pin = f"{pin_min // 60:02d}:{pin_min % 60:02d}"
                    po = pin_min + int(dur)
                    late = (pin_min - 570) if pin_min - 570 > 10 else 0
                    ot = (po - 1110) if po - 1110 >= 30 else 0
                    logs.append(rec(code, ds, status, pin, f"{po // 60:02d}:{po % 60:02d}", late=late, ot=ot,
                                    half=dur < 4.5 * 60))
            d += timedelta(days=1)
    return emps, logs


# ------------------------------------------------------------------ oracle (pure, independent) ---
def q2(x, places=2):
    return float(Decimal(str(x)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def month_range(month):
    y, m = int(month[:4]), int(month[5:])
    first = date(y, m, 1)
    nxt = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
    return first, nxt - timedelta(days=1)


def weight(l):
    return (0.5 if l.get("half_day") else 1.0) if l["status"] in PRES else 0.0


def is_wd(ds):
    return date.fromisoformat(ds).weekday() < 5


def oracle_monthly(emps, logs, code, month):
    first, last = month_range(month)
    e = next(x for x in emps if x["emp_code"] == code)
    start = max(first, date.fromisoformat(e["joined_on"]))
    wd = sum(1 for i in range((last - start).days + 1) if (start + timedelta(i)).weekday() < 5) if start <= last else 0
    mine = [l for l in logs if l["emp_code"] == code and first.isoformat() <= l["date"] <= last.isoformat()]
    present = sum(weight(l) for l in mine if is_wd(l["date"]))
    return {"emp_code": code, "month": month, "working_days": wd, "present_days": present,
            "leave_days": sum(l["status"] == "LEAVE" for l in mine),
            "late_count": sum((l.get("late_minutes") or 0) > 0 for l in mine),
            "total_late_minutes": sum(l.get("late_minutes") or 0 for l in mine),
            "total_overtime_minutes": sum(l.get("overtime_minutes") or 0 for l in mine),
            "attendance_pct": None if wd == 0 else q2(Decimal(str(present)) * 100 / wd)}


def oracle_dept_summary(emps, logs, month, dept=None):
    first, last = month_range(month)
    by_dept = defaultdict(list)
    for e in emps:
        if e["joined_on"] <= last.isoformat() and (dept is None or e["department"] == dept):
            by_dept[e["department"]].append(e["emp_code"])
    out = []
    for d in sorted(by_dept):
        codes = set(by_dept[d])
        ls = [l for l in logs if l["emp_code"] in codes and first.isoformat() <= l["date"] <= last.isoformat()]
        hrs = [Decimal(str(l["work_hours"])) for l in ls if l["status"] in PRES and l.get("work_hours") is not None]
        out.append({"department": d, "headcount": len(codes),
                    "present_days": sum(weight(l) for l in ls if is_wd(l["date"])),
                    "avg_work_hours": q2(sum(hrs) / len(hrs)) if hrs else None,
                    "late_count": sum((l.get("late_minutes") or 0) > 0 for l in ls),
                    "total_late_minutes": sum(l.get("late_minutes") or 0 for l in ls),
                    "leave_count": sum(l["status"] == "LEAVE" for l in ls),
                    "on_duty_count": sum(l["status"] == "ON_DUTY" for l in ls)})
    return out


def oracle_leaderboard(emps, logs, month, limit, dept=None):
    first, last = month_range(month)
    info = {e["emp_code"]: e for e in emps}
    tot, cnt = defaultdict(int), defaultdict(int)
    for l in logs:
        if first.isoformat() <= l["date"] <= last.isoformat() and (l.get("late_minutes") or 0) > 0 and l["emp_code"] in info:
            if dept is None or info[l["emp_code"]]["department"] == dept:
                tot[l["emp_code"]] += l["late_minutes"]
                cnt[l["emp_code"]] += 1
    rows = sorted(tot, key=lambda c: (-tot[c], c))
    out = []
    for i, c in enumerate(rows):
        rank = 1 + sum(1 for o in rows if tot[o] > tot[c])
        if rank <= limit:
            out.append({"rank": rank, "emp_code": c, "name": info[c]["name"], "department": info[c]["department"],
                        "total_late_minutes": tot[c], "late_count": cnt[c]})
    return out


def oracle_trend(emps, logs, dept, d_from, d_to):
    codes = {e["emp_code"] for e in emps if e["department"] == dept}
    rows, rates = [], []
    d = date.fromisoformat(d_from)
    while d <= date.fromisoformat(d_to):
        ds = d.isoformat()
        hc = sum(1 for e in emps if e["department"] == dept and e["joined_on"] <= ds)
        day_logs = [l for l in logs if l["emp_code"] in codes and l["date"] == ds]
        present = sum(weight(l) for l in day_logs)
        wd = d.weekday() < 5
        rate = q2(Decimal(str(present)) / hc, 4) if wd and hc > 0 else None
        rates.append(rate)
        win = [r for r in rates[-7:] if r is not None]
        rows.append({"date": ds, "is_working_day": wd, "headcount": hc, "present_count": present,
                     "late_count": sum((l.get("late_minutes") or 0) > 0 for l in day_logs), "attendance_rate": rate,
                     "moving_avg_7d": q2(sum(Decimal(str(r)) for r in win) / len(win), 4) if win else None})
        d += timedelta(days=1)
    return rows


# ------------------------------------------------------------------ seed ---
def mongo():
    from pymongo import MongoClient
    c = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
    return c[os.getenv("MONGO_DB", "attendance_db")]


def cmd_seed():
    db = mongo()
    db.employees.drop()
    db.attendance_logs.drop()  # dropping also drops indexes: the app must recreate them at startup
    e1, l1 = build_edge()
    e2, l2 = build_bulk()
    db.employees.insert_many(e1 + e2)
    allogs = l1 + l2
    for i in range(0, len(allogs), 10_000):
        db.attendance_logs.insert_many([dict(x) for x in allogs[i:i + 10_000]])
    print(f"seeded {len(e1) + len(e2)} employees and {len(allogs)} attendance records into {db.name}")


# ------------------------------------------------------------------ check ---
API = os.getenv("API_URL", "http://localhost:8000")
FAILS = []


def call(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(API + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"   <-- {extra}"))
    if not cond:
        FAILS.append(name)


def same(a, b, tol=1e-9):
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return abs(a - b) <= tol
    return a == b


def ms(day, hm, plus_days=0, sec=0):
    return int(ist(day, hm, plus_days).timestamp()) * 1000 + sec * 1000


def parallel(n, fn):
    out, ts = [None] * n, []
    for i in range(n):
        t = threading.Thread(target=lambda i=i: out.__setitem__(i, fn(i)))
        ts.append(t)
    [t.start() for t in ts]
    [t.join() for t in ts]
    return out


def cmd_check():
    db = mongo()
    emps = list(db.employees.find({}, {"_id": 0}))
    logs = list(db.attendance_logs.find({}, {"_id": 0}))

    s, b = call("GET", "/health")
    check("health 200", s == 200 and b == {"status": "ok"}, (s, b))

    # ---- employees
    code = f"EMP{random.randint(100000, 999999)}"
    emp_body = {"emp_code": code, "name": "Test User", "email": "t@example.com", "department": "TestDept",
                "joined_on": "2026-01-01", "created_at": 5, "work_hours": 99}
    s, b = call("POST", "/employees", emp_body)
    check("create employee 201 (+defaults, ms created_at, no _id)", s == 201 and b["shift_start"] == "09:30"
          and isinstance(b["created_at"], int) and "_id" not in b and "id" not in b, (s, b))
    s, _ = call("POST", "/employees", emp_body)
    check("duplicate emp_code -> 409", s == 409)
    for label, patch in [("bad emp_code", {"emp_code": "abc"}), ("bad email", {"email": "nope"}),
                         ("same shifts", {"shift_start": "09:00", "shift_end": "09:00"}),
                         ("bad date", {"joined_on": "2026-02-30"}), ("bad shift fmt", {"shift_start": "9:30"})]:
        s, _ = call("POST", "/employees", {**emp_body, "emp_code": "EMP7777", **patch})
        check(f"create employee 422: {label}", s == 422)
    rc = [f"EMP{random.randint(100000, 999999)}"] * 1
    res = parallel(15, lambda i: call("POST", "/employees", {**emp_body, "emp_code": rc[0]})[0])
    check("15 parallel creates -> exactly one 201", sorted(res).count(201) == 1 and res.count(409) == 14, res)

    s, b = call("GET", "/employees?department=Eng&page=1&page_size=2")
    check("employees: filtered total + sorted + page", s == 200 and b["total"] == 3 and
          [e["emp_code"] for e in b["items"]] == sorted(e["emp_code"] for e in b["items"]) and len(b["items"]) == 2, b)
    s, b2 = call("GET", "/employees?department=Eng&page=2&page_size=2")
    check("employees: page 2 differs from page 1", b2["items"] and b2["items"][0]["emp_code"] > b["items"][-1]["emp_code"], b2)
    for q in ["page=0", "page_size=0", "page_size=101"]:
        check(f"employees 422: {q}", call("GET", f"/employees?{q}")[0] == 422)

    # ---- punch-in rules (R1, R2) on a fresh employee (shift 09:30)
    cases = [("2027-03-01", "09:40", 0, 0), ("2027-03-02", "09:40", 1, 10), ("2027-03-03", "10:15", 59, 45),
             ("2027-03-04", "09:00", 0, 0)]
    for day, hm, sec, want in cases:
        s, b = call("POST", "/attendance/punch-in", {"emp_code": code, "punched_at": ms(day, hm, 0, sec) + 900})
        check(f"R2 {hm}:{sec:02d} -> late {want} (and ms truncated)", s == 201 and b["late_minutes"] == want
              and b["punch_in"] % 1000 == 0 and b["date"] == day, (s, b))
    s, b = call("POST", "/attendance/punch-in", {"emp_code": "EMP999999"})
    check("punch-in unknown employee -> 404", s == 404)
    for label, body in [("seconds", {"punched_at": 1783312500}), ("float", {"punched_at": 1783312500000.5}),
                        ("string", {"punched_at": "1783312500000"}), ("ABSENT", {"status": "ABSENT"})]:
        s, _ = call("POST", "/attendance/punch-in", {"emp_code": code, **body})
        check(f"punch-in 422: {label}", s == 422)
    res = parallel(12, lambda i: call("POST", "/attendance/punch-in", {"emp_code": code, "punched_at": ms("2027-03-10", "09:30")})[0])
    check("12 parallel punch-ins -> one 201, rest 409", res.count(201) == 1 and res.count(409) == 11, res)

    # ---- punch-out (R3-R5)
    out = lambda day, hm, **kw: call("POST", "/attendance/punch-out", {"emp_code": code, "punched_at": ms(day, hm, **kw)})
    s, b = out("2027-03-10", "13:00")
    check("punch-out: 3.5h => half_day, no overtime", s == 200 and b["work_hours"] == 3.5 and b["half_day"] is True
          and b["overtime_minutes"] == 0 and b["history"] == [], b)
    s, _ = out("2027-03-10", "14:00")
    check("second punch-out -> 409", s == 409)
    s, b = call("POST", "/attendance/punch-out", {"emp_code": code, "punched_at": ms("2027-03-04", "09:00")})
    check("punch-out == punch_in -> 422", s == 422, (s, b))
    s, b = call("POST", "/attendance/punch-out", {"emp_code": code, "punched_at": ms("2027-03-02", "09:41", 1)})
    check("punch-out >24h after -> 422", s == 422, (s, b))
    s, b = out("2027-03-01", "19:05")
    check("overtime 35 min counted", s == 200 and b["overtime_minutes"] == 35 and b["work_hours"] == 9.42, b)
    s, b = out("2027-03-03", "18:59")
    check("overtime 29 min NOT counted (R3)", s == 200 and b["overtime_minutes"] == 0, b)
    s, _ = call("POST", "/attendance/punch-out", {"emp_code": "EMP999999"})
    check("punch-out unknown employee -> 404", s == 404)
    c2 = f"EMP{random.randint(100000, 999999)}"
    call("POST", "/employees", {**emp_body, "emp_code": c2})
    s, _ = call("POST", "/attendance/punch-out", {"emp_code": c2, "punched_at": ms("2027-04-01", "18:00")})
    check("punch-out without punch-in -> 404", s == 404)
    call("POST", "/attendance/punch-in", {"emp_code": c2, "punched_at": ms("2027-04-01", "09:30")})
    res = parallel(10, lambda i: call("POST", "/attendance/punch-out", {"emp_code": c2, "punched_at": ms("2027-04-01", "18:30")})[0])
    check("10 parallel punch-outs -> exactly one 200", res.count(200) == 1 and res.count(409) == 9, res)

    # ---- overnight (R1): in 05:30 IST on the 2nd belongs to the 1st
    c3 = f"EMP{random.randint(100000, 999999)}"
    call("POST", "/employees", {**emp_body, "emp_code": c3, "shift_start": "22:00", "shift_end": "06:00"})
    s, b = call("POST", "/attendance/punch-in", {"emp_code": c3, "punched_at": ms("2027-05-02", "05:30")})
    check("overnight: 05:30 belongs to previous day", s == 201 and b["date"] == "2027-05-01", b)
    s, b = call("POST", "/attendance/punch-out", {"emp_code": c3, "punched_at": ms("2027-05-02", "06:45")})
    check("overnight punch-out: overtime 45", s == 200 and b["overtime_minutes"] == 45, b)
    s, b = call("POST", "/attendance/punch-in", {"emp_code": c3, "punched_at": ms("2027-05-02", "21:55")})
    check("overnight: 21:55 same day, not late", s == 201 and b["date"] == "2027-05-02" and b["late_minutes"] == 0, b)

    # ---- PATCH
    base = f"/attendance/{code}/2027-03-02"  # in 09:40:01 (late 10), out missing
    s, b = call("PATCH", base, {"punch_in": ms("2027-03-02", "09:28"), "reason": "biometric glitch", "regularized_by": "hr"})
    ch = b["history"][0]["changes"] if s == 200 else {}
    check("PATCH recalculates + history entry", s == 200 and b["late_minutes"] == 0 and len(b["history"]) == 1
          and ch.get("late_minutes") == {"from": 10, "to": 0} and isinstance(ch["punch_in"]["from"], int), (s, b))
    s, _ = call("PATCH", base, {"punch_in": ms("2027-03-02", "09:28"), "reason": "same again", "regularized_by": "hr"})
    check("PATCH that changes nothing -> 422", s == 422)
    s, _ = call("PATCH", base, {"status": "LEAVE", "punch_in": ms("2027-03-02", "09:00"), "reason": "conflict", "regularized_by": "hr"})
    check("PATCH LEAVE + punch time -> 422", s == 422)
    s, _ = call("PATCH", base, {"punch_in": ms("2027-03-03", "09:00"), "reason": "wrong day", "regularized_by": "hr"})
    check("PATCH punch_in on another date -> 422", s == 422)
    s, _ = call("PATCH", base, {"punch_out": ms("2027-03-02", "09:00"), "reason": "before in", "regularized_by": "hr"})
    check("PATCH punch_out before punch_in -> 422", s == 422)
    s, _ = call("PATCH", f"/attendance/{code}/2030-01-01", {"status": "LEAVE", "reason": "no record", "regularized_by": "hr"})
    check("PATCH missing record -> 404", s == 404)
    s, _ = call("PATCH", f"/attendance/EMP999999/2027-03-02", {"status": "LEAVE", "reason": "no emp", "regularized_by": "hr"})
    check("PATCH unknown employee -> 404", s == 404)
    s, b = call("PATCH", base, {"status": "LEAVE", "reason": "approved leave", "regularized_by": "hr"})
    check("PATCH to LEAVE clears times", s == 200 and b["punch_in"] is None and b["work_hours"] is None and len(b["history"]) == 2, b)
    pb = f"/attendance/{code}/2027-03-01"
    res = parallel(8, lambda i: call("PATCH", pb, {"punch_out": ms("2027-03-01", "18:40", 0, i + 1),
                                                   "reason": f"concurrent {i}", "regularized_by": "hr"})[0])
    s, b = call("GET", f"/attendance?emp_code={code}&date_from=2027-03-01&date_to=2027-03-01")
    n_hist = len(b["items"][0]["history"])
    check("concurrent PATCH: history length == number of 200s", n_hist == res.count(200) and res.count(200) >= 1
          and set(res) <= {200, 409}, (res, n_hist))

    # ---- list attendance
    s, b = call("GET", "/attendance?page_size=100")
    keys = [(i["date"], i["emp_code"]) for i in b["items"]]
    check("list: sorted date desc, emp_code asc, ms ints, no id", s == 200 and len(keys) == 100 and
          all(keys[i][0] > keys[i + 1][0] or (keys[i][0] == keys[i + 1][0] and keys[i][1] < keys[i + 1][1]) for i in range(99))
          and "id" not in b["items"][0], keys[:3])
    s, b = call("GET", "/attendance?emp_code=EMP9007&date_from=2026-07-01&date_to=2026-07-31")
    legacy = [i for i in b["items"] if i["date"] == "2026-07-15"]
    check("list: legacy record gets half_day=false and history=[]", legacy and legacy[0]["half_day"] is False
          and legacy[0]["history"] == [], b)
    expect = sum(1 for l in logs if l["emp_code"] == "EMP9001" and l["status"] == "LEAVE")
    s, b = call("GET", "/attendance?emp_code=EMP9001&status=LEAVE")
    check("list: total reflects filters", s == 200 and b["total"] == expect, b)
    for q in ["date_from=2026-07-10&date_to=2026-07-01", "date_from=2026-7-1", "status=NOPE", "page=0", "page_size=500"]:
        check(f"list 422: {q}", call("GET", f"/attendance?{q}")[0] == 422)
    t0 = time.time()
    call("GET", "/attendance?page=50&page_size=100")
    check(f"list on big collection fast ({time.time() - t0:.2f}s)", time.time() - t0 < 1.5)

    # ---- analytics vs oracle (data = what is in Mongo right now)
    emps = list(db.employees.find({}, {"_id": 0}))
    logs = list(db.attendance_logs.find({}, {"_id": 0}))
    for c in ["EMP9001", "EMP9002", "EMP9003", "EMP9004", "EMP9005", "EMP9006", "EMP9007", "EMP1000"]:
        for mo in ["2026-07", "2026-02", "2026-09"]:
            s, b = call("GET", f"/analytics/employees/{c}/monthly?month={mo}")
            want = oracle_monthly(emps, logs, c, mo)
            check(f"monthly {c} {mo}", s == 200 and same(b, want), (b, want))
    check("monthly unknown employee -> 404", call("GET", "/analytics/employees/EMP999999/monthly?month=2026-07")[0] == 404)
    check("monthly bad month -> 422", call("GET", "/analytics/employees/EMP9001/monthly?month=2026-13")[0] == 422)
    for mo in ["2026-07", "2026-01", "2026-09"]:
        s, b = call("GET", f"/analytics/departments/summary?month={mo}")
        check(f"dept summary {mo}", s == 200 and same(b["items"], oracle_dept_summary(emps, logs, mo)),
              (b["items"][:2], oracle_dept_summary(emps, logs, mo)[:2]))
    s, b = call("GET", "/analytics/departments/summary?month=2026-07&department=QA")
    check("dept summary: QA headcount 1, avg null", s == 200 and b["items"] and b["items"][0]["headcount"] == 1
          and b["items"][0]["avg_work_hours"] is None, b)
    s, b = call("GET", "/analytics/departments/summary?month=2026-07")
    ops = next(i for i in b["items"] if i["department"] == "Ops")
    check("dept summary: Ops headcount excludes future joiner", ops["headcount"] == 1, ops)
    for lim in [1, 3, 10]:
        s, b = call("GET", f"/analytics/leaderboard/late?month=2026-07&limit={lim}")
        want = oracle_leaderboard(emps, logs, "2026-07", lim)
        check(f"leaderboard limit={lim} ({len(want)} rows, ties kept)", s == 200 and same(b["items"], want), (b["items"][:6], want[:6]))
    s, b = call("GET", "/analytics/leaderboard/late?month=2026-07&limit=1&department=Eng")
    check("leaderboard per department", same(b["items"], oracle_leaderboard(emps, logs, "2026-07", 1, "Eng")), b)
    check("leaderboard limit=0 -> 422", call("GET", "/analytics/leaderboard/late?month=2026-07&limit=0")[0] == 422)
    for dept, f, t in [("Eng", "2026-06-29", "2026-07-20"), ("Eng", "2026-07-12", "2026-07-16"), ("QA", "2026-07-01", "2026-07-10"),
                       ("Engineering", "2026-07-01", "2026-09-30"), ("Ops", "2026-07-30", "2026-08-05"), ("Eng", "2026-12-01", "2026-12-05")]:
        s, b = call("GET", f"/analytics/departments/{dept}/trend?from={f}&to={t}")
        want = oracle_trend(emps, logs, dept, f, t)
        check(f"trend {dept} {f}..{t} ({len(want)} rows)", s == 200 and same(b["items"], want),
              next(((x, y) for x, y in zip(b["items"], want) if not same(x, y)), (s, str(b)[:200])) if s == 200 else (s, b))
    check("trend unknown dept -> 404", call("GET", "/analytics/departments/Nope/trend?from=2026-07-01&to=2026-07-02")[0] == 404)
    check("trend to<from -> 422", call("GET", "/analytics/departments/Eng/trend?from=2026-07-05&to=2026-07-01")[0] == 422)
    check("trend 93 days -> 422", call("GET", "/analytics/departments/Eng/trend?from=2026-07-01&to=2026-10-01")[0] == 422)
    check("trend 92 days -> 200", call("GET", "/analytics/departments/Eng/trend?from=2026-07-01&to=2026-09-30")[0] == 200)

    # ---- explain
    ex = [("attendance_list", "emp_code=EMP1000&date_from=2026-02-01&date_to=2026-02-28&status=PRESENT"),
          ("attendance_list", "page_size=50"), ("attendance_list", "status=LEAVE"),
          ("employee_monthly", "emp_code=EMP1000&month=2026-02"), ("department_summary", "month=2026-02"),
          ("department_summary", "month=2026-02&department=Sales"), ("late_leaderboard", "month=2026-02&limit=5"),
          ("late_leaderboard", "month=2026-02&department=Sales"),
          ("department_trend", "department=Engineering&from=2026-02-01&to=2026-03-15")]
    for name, q in ex:
        s, b = call("GET", f"/admin/explain/{name}?{q}")
        txt = json.dumps(b)
        check(f"explain {name}?{q[:40]}: IXSCAN and no COLLSCAN", s == 200 and "IXSCAN" in txt and "COLLSCAN" not in txt,
              (s, "COLLSCAN" in txt, "IXSCAN" in txt))
    check("explain missing param -> 422", call("GET", "/admin/explain/employee_monthly?month=2026-02")[0] == 422)

    print(f"\n{'ALL CHECKS PASSED' if not FAILS else str(len(FAILS)) + ' CHECK(S) FAILED'}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    {"seed": cmd_seed, "check": cmd_check}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: print(__doc__))()
