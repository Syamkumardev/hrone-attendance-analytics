# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (identity, sort); `(department, emp_code)` for filtered listing; `(department, joined_on)` and `(joined_on)` for headcount matches. `attendance_logs`: unique `(emp_code, date)` (race guard, per-employee lookups); `(date desc, emp_code)` for the list sort and month scans; `(status, date desc, emp_code)` for status filters. Rejected: a covering `(date, late_minutes, emp_code)` index for the leaderboard; extra write cost for little gain.
2. **Punch-in race.** Both requests find the employee and compute the same date, then insert. The unique `(emp_code, date)` index lets one insert win; the other raises `DuplicateKeyError`, which I return as 409. No find-before-insert.
3. **Ties.** `$rank` gives tied employees the same rank, and `limit` filters `rank <= limit` afterwards, so a tie at the cutoff returns every tied row, possibly more than `limit`, ordered by emp_code.
4. **Headcount.** The pipeline starts from `employees`, filtered by `joined_on`, and `$lookup`s logs per employee; `$unwind` with `preserveNullAndEmptyArrays` keeps people with no logs.
5. **100x data.** Maintain a daily per-employee rollup collection updated on punch/PATCH, so analytics read summaries, not raw logs.
