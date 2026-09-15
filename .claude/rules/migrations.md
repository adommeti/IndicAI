---
paths: ["platform/db/**", "alembic.ini"]
---
# Database rules

- One alembic revision per prompt at most; name it `NNNN_<uc>_<change>.py`; never edit a revision
  that exists on the base branch.
- Models in `platform/db/models.py` (shared) or `apps/<app>/models.py` (app-owned) with
  SQLAlchemy 2.0 typed mappings; JSON columns are `JSONB` on Postgres.
- Append-only tables (comms_surveillance `analysis_runs`, `flags`, `dispositions`) carry
  `prev_hash`/`row_hash`; the app role receives INSERT/SELECT only, granted in a migration, and a
  test proves UPDATE is refused.
- `alembic upgrade head` and `alembic check` both pass against a fresh database in the
  integration test job.
- Retention jobs delete by policy and log every deletion; they never `TRUNCATE`.
