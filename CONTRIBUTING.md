# Contributing to MediFlow AI

Thank you for helping improve MediFlow AI. This guide covers the workflow for proposing and landing changes.

## Getting set up

Follow [docs/SETUP.md](docs/SETUP.md) to run the backend and frontend locally. In short:

```bash
# Backend
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head && python seed.py
uvicorn main:app --reload

# Frontend
cd frontend && npm install && npm run dev
```

## Before you open a pull request

Run the same checks CI runs:

```bash
# Backend
cd backend
ruff check .
rm -f mediflow.db && alembic upgrade head && python seed.py
pytest -v

# Frontend
cd frontend
npm run lint
npm run build
```

New behavior needs a test. Bug fixes should include a test that fails without the fix. The booking path has a concurrency regression test (`backend/test_booking_concurrency.py`); changes to scheduling or booking must keep it passing.

## Database changes

Schema changes go through Alembic, never by editing tables by hand.

```bash
cd backend
alembic revision --autogenerate -m "describe the change"
```

Review the generated file before committing (autogenerate cannot detect column renames and will emit a drop and add). Confirm it applies and reverses cleanly with `alembic upgrade head`, `alembic downgrade -1`, `alembic upgrade head`. See [backend/alembic/README.md](backend/alembic/README.md).

## Code style

- Backend: formatted and linted with `ruff` (configuration in `backend/pyproject.toml`). Use type hints on new functions.
- Frontend: linted with `oxlint`. Follow the existing Tailwind conventions and keep layouts verified at mobile widths.
- Read configuration through `config.settings`, never `os.environ` directly.
- Never commit secrets. Only `.env.example` files belong in the repository.

## Pull requests

- Keep each pull request focused on one change.
- Describe what changed and why, and how you verified it.
- Update the relevant documentation and add an entry to [CHANGELOG.md](CHANGELOG.md) for user-visible changes.
- A maintainer will review; please respond to feedback in the same thread.

## Reporting bugs and requesting features

Use the issue templates. For security vulnerabilities, do not open a public issue; follow [SECURITY.md](SECURITY.md).

## Code of Conduct

By participating you agree to abide by the [Code of Conduct](CODE_OF_CONDUCT.md).
