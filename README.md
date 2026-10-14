# Employee Attendance & Analytics API

FastAPI + MongoDB. All application code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"            # real env vars win over .env
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```
MongoDB 6.0+ is required (uses `$densify`, `$setWindowFields`, `$rank`). Indexes are created automatically at startup.
Local MongoDB: `docker run -d -p 27017:27017 mongo:7`. Interactive docs: http://localhost:8000/docs

## Notes
* All endpoints in `openapi.yaml` are implemented, including the three aggregation endpoints, the trend and `/admin/explain/{endpoint}`.
* `REVIEW.md` lists the defects found in the starter; `DECISIONS.md` answers the five questions.
* Judgement calls where the contract is silent: an explicit `null` in PATCH is a 422 (omit a field to leave it unchanged); a trend range of exactly 92 days is allowed, 93 is a 422; the trend `moving_avg_7d` averages the already-rounded (4 dp) `attendance_rate` values.
