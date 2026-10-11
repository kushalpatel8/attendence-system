# DECISIONS.md

1. **Indexes.**
On `employees`: unique `emp_code` ensures identifier uniqueness; `(department, joined_on)` powers department lookups and headcount filtering. On `attendance_logs`: unique `(emp_code, date)` guarantees one punch-in per day; `(date, emp_code)` and `(status, date, emp_code)` support listing and sorting without in-memory sorts; `(emp_code, punch_in)` accelerates punch-out; and `(date, late_minutes)` powers leaderboard index scans. We considered a partial index on `punch_out: null` but rejected it because `(emp_code, punch_in)` already provides an efficient index scan for the most recent punch-in.

2. **Punch-in race.**
Both requests execute concurrently and attempt `insert_one`. MongoDB's unique index on `(emp_code, date)` serializes write lock acquisition. Exactly one write succeeds and returns `201 Created`. The other encounters a `DuplicateKeyError`, caught by our application to return `409 Conflict`.

3. **Ties.**
Using `$rank` in `$setWindowFields`, ties share a rank (1, 2, 2, 4). Filtering on `rank <= limit` includes all tied candidates at the cutoff threshold (possibly returning more rows than `limit`), ordered by `total_late_minutes DESC` then `emp_code ASC`.

4. **Headcount.**
The aggregation originates on `employees`, matching `joined_on <= period_end`. It `$lookups` logs and unwinds with `preserveNullAndEmptyArrays: true`. Employees with zero logs are retained and counted in headcount via `$addToSet: "$emp_code"`.

5. **One thing you would change for 100x data.**
We would pre-aggregate daily and monthly statistics via event-driven background workers (or MongoDB change streams) into dedicated summary collections, eliminating on-demand cross-collection log aggregations.
