"""Integration tests - these need a real MongoDB (6.0+). Run:  pytest tests/test_api.py -v

They use their own database (default "attendance_test") and WIPE its two collections before each
test. The suite refuses to run against a database whose name does not contain "test".

Analytics expectations are worked out by hand from the small dataset in `seed_analytics` (see the
comment above it), not by calling the code under test."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

os.environ["MONGO_DB"] = os.getenv("TEST_MONGO_DB", "attendance_test")  # before importing the app

from app import main as m  # noqa: E402

assert "test" in m.MONGO_DB, "refusing to run tests against a database that is not a test database"
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


def ms(y, mo, d, h=0, mi=0, s=0):
    """Epoch milliseconds of an IST wall-clock time."""
    return int(datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp()) * 1000


@pytest.fixture(scope="session")
def client():
    with TestClient(m.app) as c:  # runs the lifespan: indexes are created at startup
        yield c


@pytest.fixture(autouse=True)
def clean(client):
    m.employees_col.delete_many({})
    m.logs_col.delete_many({})


def add_employee(client, code="EMP0001", dept="Eng", start="09:30", end="18:30", joined="2026-01-05"):
    r = client.post("/employees", json={"emp_code": code, "name": f"Name {code}", "email": "a@b.co",
                                        "department": dept, "shift_start": start, "shift_end": end,
                                        "joined_on": joined})
    assert r.status_code == 201, r.text
    return r.json()


def parallel(fn, n=8):
    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(lambda i: fn(i), range(n)))


# --------------------------------------------------------------------------- health / indexes
def test_health_and_indexes(client):
    assert client.get("/health").json() == {"status": "ok"}
    names = {i["name"] for i in m.logs_col.list_indexes()} | {i["name"] for i in m.employees_col.list_indexes()}
    for expected in (m.EMP_CODE_UQ, m.LOGS_EMP_DATE_UQ, m.LOGS_DATE_EMP, m.LOGS_STATUS_DATE_EMP):
        assert expected in names
    m.ensure_indexes()  # idempotent: a second call must not raise


# --------------------------------------------------------------------------- employees
def test_create_employee_ok_and_shape(client):
    e = add_employee(client)
    assert set(e) == {"emp_code", "name", "email", "department", "shift_start", "shift_end",
                      "joined_on", "created_at"}
    assert isinstance(e["created_at"], int) and e["created_at"] > 10**12  # epoch milliseconds
    assert (e["shift_start"], e["shift_end"]) == ("09:30", "18:30")


@pytest.mark.parametrize("patch", [
    {"emp_code": "E1"}, {"emp_code": "EMP12"}, {"emp_code": "EMP1234567"}, {"emp_code": "emp0001"},
    {"email": "not-an-email"}, {"name": ""}, {"department": ""}, {"joined_on": "2026-02-30"},
    {"joined_on": "20260105"}, {"shift_start": "25:00"}, {"shift_end": "9:30"},
    {"shift_start": "09:30", "shift_end": "09:30"},
])
def test_create_employee_validation_422(client, patch):
    body = {"emp_code": "EMP0001", "name": "A", "email": "a@b.co", "department": "Eng",
            "joined_on": "2026-01-05", **patch}
    assert client.post("/employees", json=body).status_code == 422


def test_duplicate_employee_409_and_race(client):
    add_employee(client)
    body = {"emp_code": "EMP0001", "name": "A", "email": "a@b.co", "department": "Eng", "joined_on": "2026-01-05"}
    assert client.post("/employees", json=body).status_code == 409
    body["emp_code"] = "EMP0002"
    codes = sorted(parallel(lambda i: TestClient(m.app).post("/employees", json=body).status_code))
    assert codes.count(201) == 1 and codes.count(409) == len(codes) - 1
    assert m.employees_col.count_documents({"emp_code": "EMP0002"}) == 1


def test_list_employees_pagination_filter_sort(client):
    for i, dept in enumerate(["Eng", "Ops", "Eng", "Eng", "Ops"], start=1):
        add_employee(client, code=f"EMP{i:04d}", dept=dept)
    p = client.get("/employees", params={"department": "Eng", "page_size": 2}).json()
    assert p["total"] == 3 and [i["emp_code"] for i in p["items"]] == ["EMP0001", "EMP0003"]
    p2 = client.get("/employees", params={"department": "Eng", "page_size": 2, "page": 2}).json()
    assert [i["emp_code"] for i in p2["items"]] == ["EMP0004"]
    assert client.get("/employees", params={"department": "eng"}).json()["total"] == 0  # case-sensitive
    for bad in ({"page": 0}, {"page_size": 0}, {"page_size": 101}):
        assert client.get("/employees", params=bad).status_code == 422


# --------------------------------------------------------------------------- punch-in
def test_punch_in_basic_and_late(client):
    add_employee(client)
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 10, 5)})
    assert r.status_code == 201
    b = r.json()
    assert (b["date"], b["status"], b["late_minutes"], b["punch_out"], b["work_hours"]) == \
           ("2026-07-06", "PRESENT", 35, None, None)
    assert (b["overtime_minutes"], b["half_day"], b["history"]) == (0, False, [])
    assert b["punch_in"] == ms(2026, 7, 6, 10, 5) and "_id" not in b and "id" not in b


@pytest.mark.parametrize("h,mi,s,expected", [(9, 40, 0, 0), (9, 40, 1, 10), (10, 15, 59, 45), (9, 0, 0, 0)])
def test_punch_in_late_boundaries(client, h, mi, s, expected):
    add_employee(client)
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, h, mi, s)})
    assert r.json()["late_minutes"] == expected


def test_punch_in_truncates_to_seconds(client):
    add_employee(client)
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 40, 0) + 900})
    assert r.json()["late_minutes"] == 0 and r.json()["punch_in"] == ms(2026, 7, 6, 9, 40, 0)


def test_punch_in_overnight_date(client):
    add_employee(client, code="EMP0005", start="22:00", end="06:00")
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 7, 0, 30)})
    assert (r.json()["date"], r.json()["late_minutes"]) == ("2026-07-06", 150)


def test_punch_in_errors(client):
    add_employee(client)
    ok = {"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)}
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP9999"}).status_code == 404
    assert client.post("/attendance/punch-in", json=ok).status_code == 201
    assert client.post("/attendance/punch-in", json={**ok, "punched_at": ms(2026, 7, 6, 11)}).status_code == 409
    for bad in ({"punched_at": 1783312500}, {"punched_at": 1783312500000.5}, {"punched_at": "1783312500000"},
                {"punched_at": True}, {"punched_at": 99999999999}, {"punched_at": 4102444800001},
                {"status": "ABSENT"}, {"status": "LEAVE"}, {"status": "nope"}):
        r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", **bad})
        assert r.status_code == 422, (bad, r.text)
    assert client.post("/attendance/punch-in", json={}).status_code == 422


def test_punch_in_race_exactly_one_201(client):
    add_employee(client)
    body = {"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)}
    codes = parallel(lambda i: TestClient(m.app).post("/attendance/punch-in", json=body).status_code, n=12)
    assert codes.count(201) == 1 and codes.count(409) == 11
    assert m.logs_col.count_documents({"emp_code": "EMP0001"}) == 1


# --------------------------------------------------------------------------- punch-out
def test_punch_out_rules(client):
    add_employee(client)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 10, 5)})
    out = lambda t: client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": t})
    assert out(ms(2026, 7, 6, 10, 5)).status_code == 422          # not after punch-in... (also finds it, equal)
    assert out(ms(2026, 7, 7, 10, 5, 1)).status_code == 422       # more than 24 h
    r = out(ms(2026, 7, 6, 19, 10))
    assert r.status_code == 200
    b = r.json()
    assert (b["work_hours"], b["overtime_minutes"], b["half_day"], b["late_minutes"]) == (9.08, 40, False, 35)
    assert out(ms(2026, 7, 6, 20, 0)).status_code == 409           # already punched out
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP9999"}).status_code == 404
    add_employee(client, code="EMP0002")
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0002", "punched_at": ms(2026, 7, 6, 18)}).status_code == 404


def test_punch_out_overnight_and_half_day(client):
    add_employee(client, code="EMP0005", start="22:00", end="06:00")
    client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 6, 21, 55)})
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 7, 6, 40)})
    b = r.json()
    assert (b["date"], b["work_hours"], b["overtime_minutes"]) == ("2026-07-06", 8.75, 40)
    add_employee(client, code="EMP0004")
    client.post("/attendance/punch-in", json={"emp_code": "EMP0004", "punched_at": ms(2026, 7, 7, 9, 30), "status": "WFH"})
    b = client.post("/attendance/punch-out", json={"emp_code": "EMP0004", "punched_at": ms(2026, 7, 7, 13, 0)}).json()
    assert (b["work_hours"], b["half_day"], b["status"]) == (3.5, True, "WFH")


def test_punch_out_race_exactly_one_200(client):
    add_employee(client)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)})
    body = {"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 18, 30)}
    codes = parallel(lambda i: TestClient(m.app).post("/attendance/punch-out", json=body).status_code, n=12)
    assert codes.count(200) == 1 and codes.count(409) == 11


# --------------------------------------------------------------------------- regularize
def test_regularize_sample_8_and_audit_trail(client):
    add_employee(client)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 10, 5)})
    client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 18, 30)})
    patch = lambda **k: client.patch("/attendance/EMP0001/2026-07-07",
                                     json={"reason": "biometric glitch", "regularized_by": "hr.admin", **k})
    r = patch(punch_in=ms(2026, 7, 7, 9, 28))
    assert r.status_code == 200
    b = r.json()
    assert (b["late_minutes"], b["work_hours"], len(b["history"])) == (0, 9.03, 1)
    h = b["history"][0]
    assert h["by"] == "hr.admin" and h["reason"] == "biometric glitch" and isinstance(h["at"], int)
    assert set(h["changes"]) == {"punch_in", "late_minutes", "work_hours"}
    assert h["changes"]["late_minutes"] == {"from": 35, "to": 0}
    assert h["changes"]["work_hours"] == {"from": 8.42, "to": 9.03}
    assert h["changes"]["punch_in"] == {"from": ms(2026, 7, 7, 10, 5), "to": ms(2026, 7, 7, 9, 28)}
    raw = m.logs_col.find_one({"emp_code": "EMP0001", "date": "2026-07-07"})  # stored as BSON dates
    assert isinstance(raw["history"][0]["changes"]["punch_in"]["to"], datetime)
    assert isinstance(raw["history"][0]["at"], datetime)
    # history is appended, never overwritten
    assert len(patch(status="LEAVE").json()["history"]) == 2
    # derived fields in the body are ignored, not applied
    r = patch(status="WFH", punch_in=ms(2026, 7, 7, 10, 0), work_hours=99, late_minutes=99)
    assert r.status_code == 200 and r.json()["late_minutes"] == 30 and r.json()["work_hours"] is None


def test_regularize_errors(client):
    add_employee(client)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 9, 30)})
    base = {"reason": "because", "regularized_by": "hr"}
    patch = lambda body, code="EMP0001", day="2026-07-07": client.patch(f"/attendance/{code}/{day}", json={**base, **body})
    assert patch({"status": "PRESENT"}).status_code == 422                         # changes nothing
    assert patch({"punch_in": ms(2026, 7, 8, 9, 0)}).status_code == 422            # other attendance date
    assert patch({"punch_out": ms(2026, 7, 7, 9, 0)}).status_code == 422           # before punch_in
    assert patch({"punch_out": ms(2026, 7, 8, 9, 31)}).status_code == 422           # > 24 h
    assert patch({"status": "LEAVE", "punch_in": ms(2026, 7, 7, 9, 0)}).status_code == 422
    assert patch({"status": None}).status_code == 422                               # explicit null
    assert patch({"punch_in": 1783312500}).status_code == 422                       # seconds
    assert client.patch("/attendance/EMP0001/2026-07-07", json={"reason": "abc", "regularized_by": "hr"}).status_code == 422
    assert patch({"status": "LEAVE"}, code="EMP9999").status_code == 404
    assert patch({"status": "LEAVE"}, day="2026-07-09").status_code == 404
    assert patch({"status": "LEAVE"}, day="2026-02-30").status_code == 422
    assert patch({"status": "LEAVE"}).status_code == 200
    assert patch({"status": "PRESENT"}).status_code == 422                          # needs a punch_in


def test_regularize_legacy_record_without_history(client):
    add_employee(client)
    m.logs_col.insert_one({"emp_code": "EMP0001", "date": "2026-07-14", "status": "PRESENT",
                           "punch_in": datetime(2026, 7, 14, 4, 2, tzinfo=UTC),
                           "punch_out": datetime(2026, 7, 14, 13, 1, tzinfo=UTC),
                           "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0})
    rec = client.get("/attendance", params={"emp_code": "EMP0001"}).json()["items"][0]
    assert rec["history"] == [] and rec["half_day"] is False
    r = client.patch("/attendance/EMP0001/2026-07-14", json={"status": "ON_DUTY", "reason": "official trip", "regularized_by": "hr"})
    assert r.status_code == 200 and len(r.json()["history"]) == 1


def test_regularize_concurrent_no_lost_history(client):
    add_employee(client)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 9, 30)})

    def go(i):
        return TestClient(m.app).patch("/attendance/EMP0001/2026-07-07", json={
            "punch_in": ms(2026, 7, 7, 9, 31 + i), "reason": f"edit number {i}", "regularized_by": "hr"})

    codes = [r.status_code for r in parallel(go, n=8)]
    assert set(codes) <= {200, 409} and codes.count(200) >= 1
    history = m.logs_col.find_one({"emp_code": "EMP0001"})["history"]
    assert len(history) == codes.count(200)  # every success is in the audit trail, nothing was lost


# --------------------------------------------------------------------------- attendance list
def test_list_attendance(client):
    add_employee(client)
    add_employee(client, code="EMP0002")
    for code, day, status in (("EMP0002", "2026-07-06", "PRESENT"), ("EMP0001", "2026-07-06", "PRESENT"),
                              ("EMP0001", "2026-07-07", "LEAVE"), ("EMP0001", "2026-07-08", "PRESENT")):
        m.logs_col.insert_one({"emp_code": code, "date": day, "status": status,
                               "punch_in": None if status == "LEAVE" else datetime(2026, 7, 6, 4, tzinfo=UTC)})
    p = client.get("/attendance").json()
    assert [(i["date"], i["emp_code"]) for i in p["items"]] == [
        ("2026-07-08", "EMP0001"), ("2026-07-07", "EMP0001"), ("2026-07-06", "EMP0001"), ("2026-07-06", "EMP0002")]
    assert p["total"] == 4 and all("_id" not in i and "id" not in i for i in p["items"])
    p = client.get("/attendance", params={"emp_code": "EMP0001", "date_from": "2026-07-07", "date_to": "2026-07-08"}).json()
    assert p["total"] == 2
    assert client.get("/attendance", params={"status": "LEAVE"}).json()["total"] == 1
    assert client.get("/attendance", params={"page": 2, "page_size": 3}).json()["items"][0]["emp_code"] == "EMP0002"
    assert client.get("/attendance", params={"date_from": "2026-07-09", "date_to": "2026-07-01"}).status_code == 422
    assert client.get("/attendance", params={"date_from": "2026-7-1"}).status_code == 422
    assert client.get("/attendance", params={"status": "NOPE"}).status_code == 422


# --------------------------------------------------------------------------- analytics
# Hand-worked dataset, July 2026 (Jul 1 is a Wednesday; the month has 23 weekdays).
#   Eng: EMP0001 (joined Jan), EMP0002 (joined 2026-07-15), EMP0003 (no logs), EMP0004 (joins 2026-08-01)
#   Ops: EMP0005, EMP0006, EMP0007 (all joined Jan).   EMP0999 has logs but no employee record.
def seed_analytics():
    emps = [("EMP0001", "Eng", "2026-01-05"), ("EMP0002", "Eng", "2026-07-15"), ("EMP0003", "Eng", "2026-01-05"),
            ("EMP0004", "Eng", "2026-08-01"), ("EMP0005", "Ops", "2026-01-05"), ("EMP0006", "Ops", "2026-01-05"),
            ("EMP0007", "Ops", "2026-01-05")]
    m.employees_col.insert_many([{"emp_code": c, "name": f"Name {c}", "email": "a@b.co", "department": d,
                                  "shift_start": "09:30", "shift_end": "18:30", "joined_on": j,
                                  "created_at": datetime(2026, 1, 5, 4, 30, tzinfo=UTC)} for c, d, j in emps])
    pin = datetime(2026, 7, 6, 4, tzinfo=UTC)

    def log(code, day, status, wh=None, late=0, ot=0, half=False, closed=True):
        doc = {"emp_code": code, "date": day, "status": status,
               "punch_in": pin if status in m.PRESENCE_STATUSES else None,
               "punch_out": pin + timedelta(hours=8) if closed and wh is not None else None,
               "work_hours": wh, "late_minutes": late, "overtime_minutes": ot, "half_day": half, "history": []}
        if code == "EMP0001" and day == "2026-07-09":
            doc.pop("history"); doc.pop("half_day")  # a legacy record: both fields missing
        m.logs_col.insert_one(doc)

    log("EMP0001", "2026-07-06", "PRESENT", 9.12)
    log("EMP0001", "2026-07-07", "WFH", 3.5, late=20, half=True)
    log("EMP0001", "2026-07-08", "LEAVE")
    log("EMP0001", "2026-07-09", "PRESENT", 9.08, late=35, ot=40)
    log("EMP0001", "2026-07-11", "PRESENT", 5.0, late=15)              # Saturday
    log("EMP0001", "2026-07-13", "ON_DUTY", 8.0)
    log("EMP0001", "2026-07-14", "ABSENT")
    log("EMP0001", "2026-07-15", "PRESENT", None, late=12)             # open record, no work_hours
    log("EMP0002", "2026-07-15", "PRESENT", 8.0)
    log("EMP0002", "2026-07-16", "PRESENT", 8.5, late=25)
    log("EMP0002", "2026-07-18", "PRESENT", 4.0, half=True)            # Saturday
    log("EMP0005", "2026-07-06", "PRESENT", 9.0, late=30)
    log("EMP0005", "2026-07-07", "PRESENT", 9.0, late=30)
    log("EMP0006", "2026-07-08", "PRESENT", 8.0, late=60)
    log("EMP0007", "2026-07-06", "PRESENT", 9.0, late=10)
    log("EMP0999", "2026-07-06", "PRESENT", 9.0, late=500)             # no such employee


def test_employee_monthly(client):
    seed_analytics()
    g = lambda code, month="2026-07": client.get(f"/analytics/employees/{code}/monthly", params={"month": month})
    assert g("EMP0001").json() == {
        "emp_code": "EMP0001", "month": "2026-07", "working_days": 23, "present_days": 4.5, "leave_days": 1,
        "late_count": 4, "total_late_minutes": 82, "total_overtime_minutes": 40, "attendance_pct": 19.57}
    b = g("EMP0002").json()                      # joined mid-month: working days counted from Jul 15
    assert (b["working_days"], b["present_days"], b["attendance_pct"], b["late_count"]) == (13, 2.0, 15.38, 1)
    b = g("EMP0003").json()                      # no logs at all
    assert (b["working_days"], b["present_days"], b["attendance_pct"], b["late_count"]) == (23, 0.0, 0.0, 0)
    assert g("EMP0004").json()["attendance_pct"] is None   # joins in August: 0 working days in July
    assert g("EMP9999").status_code == 404
    assert g("EMP0001", "2026-13").status_code == 422
    assert client.get("/analytics/employees/EMP0001/monthly").status_code == 422


def test_department_summary(client):
    seed_analytics()
    items = client.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"]
    assert [i["department"] for i in items] == ["Eng", "Ops"]
    # Eng headcount 3: EMP0003 has no logs but counts; EMP0004 joins in August and does not.
    # avg hours is over RECORDS: (9.12+3.5+9.08+5.0+8.0+8.0+8.5+4.0)/8 = 6.90, not an average of averages.
    assert items[0] == {"department": "Eng", "headcount": 3, "present_days": 6.5, "avg_work_hours": 6.9,
                        "late_count": 5, "total_late_minutes": 107, "leave_count": 1, "on_duty_count": 1}
    assert items[1] == {"department": "Ops", "headcount": 3, "present_days": 4.0, "avg_work_hours": 8.75,
                        "late_count": 4, "total_late_minutes": 130, "leave_count": 0, "on_duty_count": 0}
    only = client.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Ops"}).json()["items"]
    assert [i["department"] for i in only] == ["Ops"]
    # before anyone joined in Eng except EMP0001 (Jan): month 2026-02 -> Eng headcount 2, no logs, no hours
    feb = client.get("/analytics/departments/summary", params={"month": "2026-02", "department": "Eng"}).json()["items"]
    assert feb == [{"department": "Eng", "headcount": 2, "present_days": 0.0, "avg_work_hours": None,
                    "late_count": 0, "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0}]
    assert client.get("/analytics/departments/summary", params={"month": "2025-12"}).json()["items"] == []
    assert client.get("/analytics/departments/summary").status_code == 422


def test_late_leaderboard_ties_and_limit(client):
    seed_analytics()
    g = lambda **p: client.get("/analytics/leaderboard/late", params={"month": "2026-07", **p})
    rows = lambda r: [(i["rank"], i["emp_code"], i["total_late_minutes"]) for i in r.json()["items"]]
    assert rows(g()) == [(1, "EMP0001", 82), (2, "EMP0005", 60), (2, "EMP0006", 60), (4, "EMP0002", 25), (5, "EMP0007", 10)]
    # limit applies AFTER ranking: limit=2 still returns both rank-2 rows (3 rows > limit)
    assert rows(g(limit=2)) == [(1, "EMP0001", 82), (2, "EMP0005", 60), (2, "EMP0006", 60)]
    assert rows(g(limit=1)) == [(1, "EMP0001", 82)]
    # ranking happens inside the department; the tie sits exactly at the cutoff
    assert rows(g(department="Ops", limit=1)) == [(1, "EMP0005", 60), (1, "EMP0006", 60)]
    assert rows(g(department="Ops")) == [(1, "EMP0005", 60), (1, "EMP0006", 60), (3, "EMP0007", 10)]
    first = g(limit=1).json()["items"][0]
    assert first["name"] == "Name EMP0001" and first["department"] == "Eng" and first["late_count"] == 4
    assert all(i["emp_code"] != "EMP0999" for i in g(limit=50).json()["items"])  # unknown employee ignored
    assert "EMP0003" not in [i["emp_code"] for i in g(limit=50).json()["items"]]   # total must be > 0
    assert g(limit=0).status_code == 422 and g(limit=51).status_code == 422
    assert client.get("/analytics/leaderboard/late").status_code == 422


def test_department_trend(client):
    seed_analytics()
    g = lambda frm, to, dept="Eng": client.get(f"/analytics/departments/{dept}/trend", params={"from": frm, "to": to})
    r = g("2026-07-04", "2026-07-10")
    assert r.status_code == 200
    got = [(i["date"], i["is_working_day"], i["headcount"], i["present_count"], i["late_count"],
            i["attendance_rate"], i["moving_avg_7d"]) for i in r.json()["items"]]
    assert got == [
        ("2026-07-04", False, 2, 0.0, 0, None, None),      # Saturday
        ("2026-07-05", False, 2, 0.0, 0, None, None),      # Sunday
        ("2026-07-06", True, 2, 1.0, 0, 0.5, 0.5),
        ("2026-07-07", True, 2, 0.5, 1, 0.25, 0.375),      # a WFH half day counts 0.5
        ("2026-07-08", True, 2, 0.0, 0, 0.0, 0.25),        # LEAVE is not present
        ("2026-07-09", True, 2, 1.0, 1, 0.5, 0.3125),
        ("2026-07-10", True, 2, 0.0, 0, 0.0, 0.25),        # no record at all: rate 0.0, not null
    ]
    got = [(i["date"], i["headcount"], i["present_count"], i["late_count"], i["attendance_rate"], i["moving_avg_7d"])
           for i in g("2026-07-13", "2026-07-17").json()["items"]]
    assert got == [("2026-07-13", 2, 1.0, 0, 0.5, 0.5), ("2026-07-14", 2, 0.0, 0, 0.0, 0.25),
                   ("2026-07-15", 3, 2.0, 1, 0.6667, 0.3889),   # EMP0002 joins: headcount 2 -> 3
                   ("2026-07-16", 3, 1.0, 1, 0.3333, 0.375), ("2026-07-17", 3, 0.0, 0, 0.0, 0.3)]
    assert len(g("2026-07-01", "2026-07-31").json()["items"]) == 31
    assert len(g("2026-06-01", "2026-06-01").json()["items"]) == 1       # single day, no logs
    assert len(g("2026-01-01", "2026-04-02").json()["items"]) == 92      # exactly 92 days is allowed
    assert g("2026-01-01", "2026-04-03").status_code == 422              # 93 days
    assert g("2026-07-10", "2026-07-04").status_code == 422
    assert g("2026-07-01", "2026-07-31", dept="Nope").status_code == 404
    assert client.get("/analytics/departments/Eng/trend", params={"from": "2026-07-01"}).status_code == 422


# --------------------------------------------------------------------------- explain
EXPLAIN_CASES = [
    ("attendance_list", {}),
    ("attendance_list", {"emp_code": "EMP0001", "date_from": "2026-07-01", "date_to": "2026-07-31"}),
    ("attendance_list", {"status": "LEAVE"}),
    ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
    ("department_summary", {"month": "2026-07"}),
    ("department_summary", {"month": "2026-07", "department": "Eng"}),
    ("late_leaderboard", {"month": "2026-07"}),
    ("department_trend", {"department": "Eng", "from": "2026-07-01", "to": "2026-07-31"}),
]


@pytest.mark.parametrize("endpoint,params", EXPLAIN_CASES)
def test_explain_uses_an_index_and_never_scans(client, endpoint, params):
    seed_analytics()
    r = client.get(f"/admin/explain/{endpoint}", params=params)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["endpoint"] == endpoint and body["collection"] in ("employees", "attendance_logs")
    text = json.dumps(body["explain"])
    assert "IXSCAN" in text and "COLLSCAN" not in text


def test_explain_validation(client):
    assert client.get("/admin/explain/employee_monthly", params={"month": "2026-07"}).status_code == 422
    assert client.get("/admin/explain/department_summary").status_code == 422
    assert client.get("/admin/explain/department_trend", params={"department": "Eng"}).status_code == 422
    assert client.get("/admin/explain/not_an_endpoint").status_code == 422
