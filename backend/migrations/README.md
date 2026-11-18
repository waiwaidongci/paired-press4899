Schema v1 is defined by the SQLAlchemy models and created at startup via Base.metadata.create_all. For this local small application no destructive migration is run. Back up named db-data before modifying schemas.

## Withdrawal / erasure additions

`src/config/migrations.py` runs after `create_all` on every start. It only adds
the new `runs.specimen_keys`, `runs.erased` and `runs.erasure_id` columns when an
older database already has a `runs` table, using `ADD COLUMN IF NOT EXISTS` on
PostgreSQL and a guarded `ALTER TABLE` on SQLite. New tables
(`specimen_erasures`, `erasure_runs`, `erasure_steps`) are created by
`create_all`. No column, row or value is dropped, and the upgrade is safe to
re-run.

`specimen_erasures` is the tombstone register and must never be truncated while
a withdrawal is in force: deleting a tombstone row would allow re-registration.
