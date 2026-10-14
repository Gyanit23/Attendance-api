# DECISIONS.md

1. **Indexes.** `attendance_logs`: unique `(emp_code, date)` is the real key; it stops duplicate punch-ins and serves the per-employee queries. `(date desc, emp_code)` matches the list sort exactly, so no in-memory sort. `(status, date desc, emp_code)` serves the status filter. `(emp_code, punch_in desc)` finds the latest punch-in for punch-out. A small partial index on `late_minutes > 0` feeds the leaderboard. `employees`: unique `emp_code`, `(department, emp_code)`, `(joined_on, department)`. Rejected: a lone `emp_code` index (the unique one already covers it) and any index on `work_hours` (never filtered on).

2. **Punch-in race.** Both requests compute the same date, then both call `insert_one`. The unique `(emp_code, date)` index lets exactly one succeed; the other gets `DuplicateKeyError`, which I turn into 409. There is no "check first", so nothing can slip between a check and an insert.

3. **Ties.** The leaderboard uses `$rank` in `$setWindowFields`, so ties share a rank and the next is skipped (1, 2, 2, 4). The `limit` filter is applied after ranking (`rank <= limit`). If two people tie at the cutoff, both are returned and the list can be longer than `limit`.

4. **Headcount.** The summary pipeline starts from `employees` (joined on or before month end), then `$lookup`s logs into each employee. A person with no logs still has a row, with zero counts, so the department headcount counts them.

5. **At 100x data.** I would pre-aggregate monthly per-employee totals (updated on punch/PATCH) and read those, instead of re-scanning raw logs for every analytics call.
