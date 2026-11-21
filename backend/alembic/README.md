# Database migrations (Alembic)

This directory manages schema changes. The initial baseline
(`versions/5d6166ac7bb9_baseline_schema.py`) captures the full current
schema (all 6 tables, all indexes, including the partial unique index
that prevents double-booking) — it was generated from and verified
against the live models in `../models.py`.

`env.py` reads the database URL from the app's own `config.settings`
(not a separate hardcoded value in `alembic.ini`), so migrations always
target whatever `DATABASE_URL` the app itself is configured to use.

## Common commands

Run from the `backend/` directory, with your virtualenv active:

```bash
# Apply all pending migrations (this is what production/Docker runs)
alembic upgrade head

# After changing a model in models.py, generate a new migration
alembic revision --autogenerate -m "describe the change"

# ALWAYS review the generated file in versions/ before committing -
# autogenerate is good but not infallible (e.g. it won't detect a
# column rename, it'll see that as a drop + add and lose data on
# upgrade - rename the operations manually in that case)

# Check current migration state
alembic current

# Roll back one migration
alembic downgrade -1

# Roll back everything (mainly useful for testing the migrations
# themselves, not for production use)
alembic downgrade base
```

## Relationship to `Base.metadata.create_all()`

`main.py`'s startup still calls `create_all()` as a zero-config
convenience for local development and the automated test suite (see
the comment there) — it only *creates missing tables*, it can never
*alter* an existing one. That's exactly the gap Alembic closes: once a
table exists, evolving its schema (adding a column, changing a type,
adding a constraint) requires a real migration.

**In other words:** `create_all()` is fine for a brand-new empty
database (dev, CI, first-ever deploy). The moment a table has existed
and needs to *change*, use `alembic revision --autogenerate` +
`alembic upgrade head` instead of hand-editing `models.py` and hoping.

## Production usage

The Dockerfile's startup command runs `alembic upgrade head` before
starting the server (replacing the previous seed-then-serve sequence
for schema setup specifically — seeding reference data is unaffected).
This means:

- First deploy: migrations create the schema from scratch (same result
  as `create_all()` would have, but now every future change goes
  through the same reviewable, revertible mechanism).
- Every subsequent deploy: only the migrations that haven't been
  applied yet run. Already-applied ones are tracked in the
  `alembic_version` table and skipped automatically.
