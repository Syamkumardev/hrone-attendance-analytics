"""Unit tests for the pure helpers (R1-R8). No database needed:  pytest tests/test_logic.py

Expected values come from the business rules in openapi.yaml and from the sample documents in
DATA_MODEL.md, not from running the code."""
from datetime import datetime, timedelta, timezone

from app import main as m


def ist(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=m.IST)


def utc(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


# ---- R2 late ------------------------------------------------------------------------------
def test_late_grace_boundary_in_seconds():
    day = "2026-07-06"  # shift 09:30
    assert m.compute_late_minutes(ist(2026, 7, 6, 9, 28), day, "09:30") == 0
    assert m.compute_late_minutes(ist(2026, 7, 6, 9, 40, 0), day, "09:30") == 0    # exactly 10:00
    assert m.compute_late_minutes(ist(2026, 7, 6, 9, 40, 1), day, "09:30") == 10   # strictly more
    assert m.compute_late_minutes(ist(2026, 7, 6, 10, 15, 59), day, "09:30") == 45  # floor
    assert m.compute_late_minutes(ist(2026, 7, 6, 8, 0), day, "09:30") == 0         # early


def test_late_sample_document_2():
    # EMP0003, punch-in 2026-07-06T04:35Z = 10:05 IST, shift 09:30 -> 35
    assert m.compute_late_minutes(utc(2026, 7, 6, 4, 35), "2026-07-06", "09:30") == 35


def test_truncation_to_whole_seconds():
    ts = m.utc_from_ms(int(ist(2026, 7, 6, 9, 40, 0).timestamp()) * 1000 + 900)  # 09:40:00.900
    assert ts.microsecond == 0
    assert m.compute_late_minutes(ts, "2026-07-06", "09:30") == 0


# ---- R1 attendance date / overnight -----------------------------------------------------------
def test_attendance_date_day_shift_uses_ist_not_utc():
    # 2026-07-06 00:30 IST is 2026-07-05 19:00 UTC; the IST date is what counts
    assert m.attendance_date(utc(2026, 7, 5, 19, 0), "09:30", "18:30") == "2026-07-06"


def test_attendance_date_overnight():
    f = lambda *a: m.attendance_date(ist(*a), "22:00", "06:00")
    assert f(2026, 7, 6, 21, 55) == "2026-07-06"        # sample 7
    assert f(2026, 7, 7, 0, 30) == "2026-07-06"         # after midnight -> previous day's shift
    assert f(2026, 7, 7, 5, 59, 59) == "2026-07-06"
    assert f(2026, 7, 7, 6, 0, 0) == "2026-07-07"       # not earlier than shift_end
    assert f(2026, 7, 7, 12, 0) == "2026-07-07"


def test_overnight_late_is_measured_from_the_previous_days_start():
    # punch-in 00:30 on the 7th belongs to the 6th; shift started 22:00 on the 6th -> 150 min late
    assert m.compute_late_minutes(ist(2026, 7, 7, 0, 30), "2026-07-06", "22:00") == 150


def test_shift_end_overnight_is_next_day():
    assert m.shift_end_at("2026-07-06", "22:00", "06:00") == ist(2026, 7, 7, 6, 0)
    assert m.shift_end_at("2026-07-06", "09:30", "18:30") == ist(2026, 7, 6, 18, 30)
    assert m.is_overnight("09:00", "09:00")  # shift_end <= shift_start


# ---- R3 overtime ------------------------------------------------------------------------------
def test_overtime_minimum_30():
    end = m.shift_end_at("2026-07-06", "09:30", "18:30")
    assert m.compute_overtime(ist(2026, 7, 6, 18, 40), end) == 0       # 10 min: below minimum
    assert m.compute_overtime(ist(2026, 7, 6, 18, 59, 59), end) == 0   # 29 min 59 s
    assert m.compute_overtime(ist(2026, 7, 6, 19, 0, 0), end) == 30
    assert m.compute_overtime(ist(2026, 7, 6, 19, 10), end) == 40      # sample 2
    assert m.compute_overtime(ist(2026, 7, 6, 17, 0), end) == 0        # left early


def test_overtime_overnight_sample_7():
    end = m.shift_end_at("2026-07-06", "22:00", "06:00")
    assert m.compute_overtime(utc(2026, 7, 7, 1, 10), end) == 40       # 06:40 IST


# ---- R4 / R5 work hours and half day ----------------------------------------------------------
def test_work_hours_samples():
    assert m.compute_work_hours(utc(2026, 7, 6, 3, 58), utc(2026, 7, 6, 13, 5)) == 9.12   # sample 1
    assert m.compute_work_hours(utc(2026, 7, 6, 4, 35), utc(2026, 7, 6, 13, 40)) == 9.08  # sample 2
    assert m.compute_work_hours(utc(2026, 7, 7, 4, 0), utc(2026, 7, 7, 7, 30)) == 3.5      # sample 4
    assert m.compute_work_hours(utc(2026, 7, 6, 16, 25), utc(2026, 7, 7, 1, 10)) == 8.75   # sample 7
    assert m.compute_work_hours(utc(2026, 7, 7, 3, 58), utc(2026, 7, 7, 13, 0)) == 9.03    # sample 8


def test_work_hours_rounds_half_up_not_bankers():
    base = utc(2026, 7, 6, 4, 0)
    # 16218 s = 4.505 h exactly -> 4.51 (Python's round(4.505, 2) gives 4.5)
    assert m.compute_work_hours(base, base + timedelta(seconds=16218)) == 4.51
    assert m.compute_work_hours(base, base + timedelta(seconds=16182)) == 4.5   # 4.495 -> 4.50


def test_half_day_uses_rounded_hours():
    assert m.compute_half_day(3.5) is True
    assert m.compute_half_day(4.49) is True
    assert m.compute_half_day(4.5) is False
    base = utc(2026, 7, 6, 4, 0)
    hours = m.compute_work_hours(base, base + timedelta(seconds=16182))  # 4.495 -> 4.50
    assert m.compute_half_day(hours) is False  # raw 4.495 < 4.50, but the ROUNDED value is 4.50


def test_round_half_up():
    assert m.round_half_up(2.675, 2) == 2.68        # round(2.675, 2) == 2.67 in Python
    assert m.round_half_up(0.03125, 4) == 0.0313
    assert m.round_half_up(0.5, 0) == 1.0
    assert m.round_half_up(1.005, 2) == 1.01


# ---- derived fields as a whole ---------------------------------------------------------------
def test_derive_fields():
    args = ("2026-07-06", "09:30", "18:30")
    d = m.derive_fields("PRESENT", utc(2026, 7, 6, 4, 35), utc(2026, 7, 6, 13, 40), *args)
    assert d == {"work_hours": 9.08, "late_minutes": 35, "overtime_minutes": 40, "half_day": False}
    d = m.derive_fields("WFH", utc(2026, 7, 6, 4, 35), None, *args)
    assert d == {"work_hours": None, "late_minutes": 35, "overtime_minutes": 0, "half_day": False}
    d = m.derive_fields("LEAVE", None, None, *args)
    assert d == {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}


# ---- time conversion --------------------------------------------------------------------------
def test_ms_round_trip_and_truncation():
    assert m.to_ms(m.utc_from_ms(1783312500999)) == 1783312500000
    assert m.to_ms(utc(2026, 7, 6, 4, 35)) == 1783312500000
    naive = datetime(2026, 7, 6, 4, 35)  # a naive datetime from the driver is UTC
    assert m.to_ms(naive) == 1783312500000


# ---- dates and months -------------------------------------------------------------------------
def test_parse_iso_date_is_strict():
    assert m.parse_iso_date("2026-07-06").isoformat() == "2026-07-06"
    for bad in ("20260706", "2026-7-6", "2026-02-30", "2026-13-01", "2026-07-06T00:00", "", "abc"):
        try:
            m.parse_iso_date(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should be rejected")


def test_month_bounds_and_working_days():
    first, last = m.month_bounds("2026-07")
    assert (first.isoformat(), last.isoformat()) == ("2026-07-01", "2026-07-31")
    assert m.month_bounds("2028-02")[1].day == 29
    assert m.count_working_days(first, last) == 23                       # Jul 2026 has 23 Mon-Fri
    joined = m.parse_iso_date("2026-07-15")                               # a Wednesday
    assert m.count_working_days(joined, last) == 13                       # mid-month joiner (R7)
    assert m.count_working_days(m.parse_iso_date("2026-07-04"), m.parse_iso_date("2026-07-05")) == 0
    try:
        m.month_bounds("0000-01")
    except ValueError:
        pass
    else:
        raise AssertionError("year 0 must be rejected")
