# Employee Attendance & Analytics API

FastAPI + MongoDB (6.0+). All application code is in `app/main.py`.

## Setup and run

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"            # or an Atlas URI
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```

`MONGO_URI` / `MONGO_DB` are read from the environment. A local `.env` is also loaded
(see `.env.example`), but real environment variables take priority. Local MongoDB:
`docker run -p 27017:27017 mongo:7`. Indexes are created at startup (idempotent); `GET /health`
returns 200 only when MongoDB answers a ping, otherwise 503.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | 200 / 503 |
| POST | `/employees` | 201; 409 duplicate `emp_code` (unique index); 422 invalid |
| GET | `/employees` | `department`, `page`, `page_size`; sorted by `emp_code` |
| POST | `/attendance/punch-in` | 201; 404 unknown employee; 409 already punched in; 422 |
| POST | `/attendance/punch-out` | 200; 404; 409 already punched out; 422 (`<=` punch-in or > 24 h) |
| GET | `/attendance` | `emp_code`, `date_from`, `date_to`, `status`, paging; date desc, `emp_code` asc |
| PATCH | `/attendance/{emp_code}/{date}` | Regularize; recomputes derived fields; appends to `history` |
| GET | `/analytics/employees/{emp_code}/monthly?month=YYYY-MM` | Aggregation |
| GET | `/analytics/departments/summary?month=YYYY-MM[&department=]` | Aggregation across both collections |
| GET | `/analytics/leaderboard/late?month=YYYY-MM[&limit=][&department=]` | `$setWindowFields` / `$rank` |
| GET | `/analytics/departments/{department}/trend?from=&to=` | One row per day, 7-day moving average |
| GET | `/admin/explain/{endpoint}` | `executionStats` explain of the endpoint's main query |

Request and response shapes follow `openapi.yaml`. Instants are epoch milliseconds; dates are IST.

## Tests

```bash
pip install -r requirements-dev.txt
pytest tests/test_logic.py -v      # pure rules (R1-R8), no database needed
pytest tests/test_api.py -v        # integration: needs a running MongoDB
```

`tests/test_api.py` uses its own database (`attendance_test`, override with `TEST_MONGO_DB`),
wipes it between tests and refuses to run against a database whose name lacks "test".
It builds its own awkward dataset (ties, weekend records, an employee with no logs, a mid-month
joiner, a not-yet-joined employee, an overnight shift, a legacy record, a log with no employee)
and also checks races with parallel requests and that explain plans contain `IXSCAN` and not `COLLSCAN`.

## Design notes and assumptions

- **Moving average** (`moving_avg_7d`) is the mean of the *reported* (already 4-decimal-rounded)
  `attendance_rate` values in the window. This is my reading of the contract.
- **Gap filling** in the trend uses `$range` + `$dateAdd` inside the pipeline (one row per day even
  when there are no logs at all) rather than `$densify`.
- A PATCH "that changes nothing" is judged on `status`, `punch_in` and `punch_out`.
- A concurrent PATCH or punch-out that loses a race gets 409; history entries are never overwritten.
- Index hints are used on the main queries so explain output is deterministic.

## Status and known limitations

All endpoints in the contract are implemented. Be aware of what has and has not been verified:

- The pure rule helpers (`tests/test_logic.py`) were executed and pass.
- The aggregation pipelines, MongoDB races, explain output and request validation (pydantic/FastAPI)
  are covered by `tests/test_api.py`; **that suite must be run against a real MongoDB** before
  relying on it.
- Not implemented: authentication (not in scope), a holiday calendar (R7), changing an `emp_code`.
