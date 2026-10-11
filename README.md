# Employee Attendance & Analytics API

FastAPI and MongoDB backend for employee attendance tracking, manual regularization audits, and analytics pipelines.

## Features & Implementation Status
All requirements and business rules (R1–R10) from the specification are fully implemented:
- **System**: `/health` probe verifying live MongoDB connectivity (503 on failure).
- **Employee Management**: `/employees` CRUD with unique business code index (`emp_code`), validation (`shift_start != shift_end`), and paginated listing.
- **Attendance Lifecycle**:
  - `/attendance/punch-in`: Race-safe punch-in via unique `(emp_code, date)` compound index, handling overnight shifts (R1) and grace period/late calculations (R2).
  - `/attendance/punch-out`: Atomic single-winner punch-out closing the active record across midnight, computing half-up work hours (R4) and overtime (R3, R5).
  - `/attendance/{emp_code}/{date}`: Audit-logged manual regularization appending to `history` with optimistic concurrency conflict detection.
  - `/attendance`: High-performance index-backed filtering and pagination.
- **Analytics Aggregations**:
  - `/analytics/employees/{emp_code}/monthly`: Employee monthly summary with working days (Mon–Fri from `joined_on`, R7) and half-day weighted presence.
  - `/analytics/departments/summary`: Cross-collection summary counting zero-log employees (R9) and record-weighted average work hours.
  - `/analytics/leaderboard/late`: Window-ranked late-comer leaderboard with competition ties (`$rank: 1, 2, 2, 4`).
  - `/analytics/departments/{department}/trend`: Gap-filled calendar timeline via `$densify` with 7-day moving averages (`$setWindowFields`).
- **Database & Query Performance**:
  - `/admin/explain/{endpoint}`: Raw explain outputs backed by index scans (`IXSCAN`) without collection scans (`COLLSCAN`).
  - Idempotent index creation on startup via FastAPI `lifespan`.

## Prerequisites
- Python 3.11+
- MongoDB 6.0+ (Local Docker or MongoDB Atlas)

## Setup & Running Locally

1. **Clone the repository and create a virtual environment**:
   ```bash
   python -m venv .venv
   # Windows:
   .venv\Scripts\activate
   # Linux/macOS:
   source .venv/bin/activate
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure Environment Variables**:
   Create a `.env` file or export the variables in your environment:
   ```env
   MONGO_URI="mongodb://localhost:27017"
   MONGO_DB="attendance_db"
   ```

4. **Seed Sample Data (Optional)**:
   ```bash
   python sample_seed.py
   ```

5. **Start the API Server**:
   ```bash
   uvicorn app.main:app --port 8000
   ```
   Interactive OpenAPI documentation is available at `http://localhost:8000/docs`.

## Repository Structure
- `app/main.py`: Complete application code (models, endpoints, helpers, indexes).
- `requirements.txt`: Python package dependencies.
- `REVIEW.md`: Detailed defect log and review of starter code.
- `DECISIONS.md`: Key architectural decisions (indexes, concurrency, ranking, scaling).
- `sample_seed.py` / `sample_data/`: Seed script and Extended JSON sample documents.
