# Local Setup Guide

A complete, from-zero walkthrough for running MediFlow AI on your own
machine. Three paths — pick one:

- **[Path A: Docker Compose](#path-a-docker-compose-easiest)** — one
  command, no local Python/Node setup needed. Best if you just want it
  running.
- **[Path B: Manual (Python + Node)](#path-b-manual-setup)** — best for
  actively developing/editing the code, with hot-reload on both sides.
- **[Path C: Makefile shortcuts](#path-c-makefile-shortcuts)** — same as
  Path B, fewer commands to remember.

---

## Prerequisites

| Path | You need |
|---|---|
| A (Docker) | Docker Desktop (or Docker Engine + Compose) — that's it |
| B / C (Manual) | Python 3.11+, Node.js 18+, npm |

Check what you have:

```bash
docker --version          # Path A
python3 --version         # Path B/C — need 3.11+
node --version             # Path B/C — need 18+
npm --version
```

Don't have Docker or Python/Node? Get them here:
- Docker: https://docs.docker.com/get-docker/
- Python: https://www.python.org/downloads/
- Node.js: https://nodejs.org/ (LTS version)

---

## Path A: Docker Compose (easiest)

Runs Postgres + the backend API + the frontend, all containerized, with
one command.

```bash
git clone <your-fork-url>
cd MediFlow-AI

# Copy the env template and set real values
cp backend/.env.example backend/.env
```

Open `backend/.env` in any text editor and change at least these two
lines (the app will refuse to start with the placeholders in
production mode, and it's good practice even for local Docker use):

```
JWT_SECRET_KEY=<paste output of the command below>
ADMIN_PASSWORD=<pick a real password>
```

Generate a real secret key:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Now start everything:

```bash
docker compose up --build
```

First run takes a couple of minutes (downloading base images, building,
installing dependencies). Once you see the backend log settle and the
frontend container report it's ready:

- **App:** http://localhost:5173
- **API docs (Swagger):** http://localhost:8000/docs
- **API health check:** http://localhost:8000/api/health

Log in as the admin account you set in `.env` (email defaults to
`admin@mediflow.ai`) via the "Staff sign in" button in the app header,
or directly:

```bash
curl -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@mediflow.ai","password":"<your ADMIN_PASSWORD>"}'
```

**Stop everything:**

```bash
docker compose down
```

**Stop and wipe the database** (start completely fresh next time):

```bash
docker compose down -v
```

**Rebuild after pulling new code:**

```bash
docker compose up --build
```

---

## Path B: Manual setup

Two terminals: one for the backend, one for the frontend.

### 1. Clone the repo

```bash
git clone <your-fork-url>
cd MediFlow-AI
```

### 2. Backend (Terminal 1)

```bash
cd backend

# Create an isolated Python environment
python3 -m venv .venv

# Activate it
source .venv/bin/activate        # macOS/Linux
# .venv\Scripts\activate         # Windows (Command Prompt)
# .venv\Scripts\Activate.ps1     # Windows (PowerShell)

# Install dependencies
pip install -r requirements.txt

# Set up your local environment file
cp .env.example .env
```

The defaults in `.env` work out of the box for local development —
SQLite database, a placeholder JWT secret, default admin credentials.
No editing required to just get it running locally (though you should
still change `ADMIN_PASSWORD` if you'll be leaving this running or
sharing your machine).

```bash
# Create the database schema (Alembic migrations - see backend/alembic/README.md)
alembic upgrade head

# Load the reference dataset (20 hospitals, 100 doctors, sample appointments)
python seed.py

# Start the API with hot-reload
uvicorn main:app --reload
```

You should see something like:

```
INFO:     Uvicorn running on http://127.0.0.1:8000
INFO:     Application startup complete.
```

Verify it's working: open http://localhost:8000/docs in a browser — you
should see the interactive Swagger UI listing every endpoint.

### 3. Frontend (Terminal 2, new tab/window)

```bash
cd frontend

# Point the frontend at your local backend
cp .env.example .env
```

The default in `.env` (`VITE_API_BASE_URL=http://localhost:8000/api`)
already matches the backend you just started — no editing needed.

```bash
npm install
npm run dev
```

You should see:

```
  VITE ready
  ➜  Local:   http://localhost:5173/
```

Open http://localhost:5173 — the app should load and successfully call
your local backend (check the browser's Network tab if it doesn't; see
[Troubleshooting](#troubleshooting) below).

### 4. Log in as admin (optional, needed for the ops/simulation buttons)

Click **"Staff sign in"** in the top-right of the header.
Default credentials (from `backend/.env.example`):

```
Email:    admin@mediflow.ai
Password: ChangeMe123!
```

You're now able to use the emergency simulation, doctor-availability
toggle, cancellation, and queue-reallocation features, which require an
authenticated staff/admin session.

---

## Path C: Makefile shortcuts

If you're on macOS/Linux (or WSL on Windows), the included `Makefile`
wraps the manual steps above:

```bash
make install     # creates backend/.venv and installs dependencies
make seed        # loads the reference dataset
make test        # runs the backend test suite
make dev         # prints the two commands to run backend + frontend
make up          # docker compose up --build (Path A, if you prefer)
make down        # docker compose down
make clean       # removes caches, build artifacts, local db
```

`make dev` doesn't run both servers itself (Make isn't great at
managing two long-running foreground processes) — it just reminds you
of the two commands to run in separate terminals, same as Path B steps
2 and 3 above.

---

## Running the test suite

```bash
cd backend
source .venv/bin/activate   # if not already active
rm -f mediflow.db && python seed.py   # tests need the reference data seeded
pytest -v
```

Expect `18 passed`. This covers symptom classification, doctor
matching, appointment scheduling and queue-shifting, cancellation,
admin auth and roles, input validation, rate limiting, and — the most
important one — a real multi-threaded concurrency test that fires 8
simultaneous booking requests at the same doctor/slot and verifies no
two land on the same time.

### Frontend e2e tests (Playwright)

Requires the backend running (seeded) in one terminal, then:

```bash
cd frontend
npm install
npx playwright install chromium   # one-time browser download
npm run build                      # test against the real production build
npm run preview -- --port 4173 &   # serve it
npm run test:e2e                   # runs frontend/tests/e2e/app.spec.js
```

Covers: no horizontal overflow on any tab, all three tabs render and
switch correctly, no uncaught JS errors, the patient form is
fillable/submittable, the hospital dashboard shows a live backend
connection, the staff sign-in control is reachable on both mobile and
desktop, the login modal opens correctly, and the footer's navigation
links actually work.

`npm run test:e2e:ui` opens Playwright's interactive UI mode if you
want to watch the tests run and debug failures visually.

---

## Verifying your local setup end-to-end

A quick smoke test once both servers are running:

```bash
# 1. Backend health
curl http://localhost:8000/api/health
# expect: {"status":"healthy", ...}

# 2. AI classification (works with or without a Gemini API key - falls
#    back to a local classifier automatically)
curl -X POST http://localhost:8000/api/ai/classify \
  -H "Content-Type: application/json" \
  -d '{"symptoms":"chest pain and shortness of breath"}'
# expect: {"specialty":"Cardiology","urgency":"HIGH",...}

# 3. Doctor list
curl http://localhost:8000/api/doctors?specialty=Cardiology
# expect: a JSON array of doctors

# 4. Admin login
curl -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@mediflow.ai","password":"ChangeMe123!"}'
# expect: {"access_token": "...", "token_type": "bearer", ...}
```

Then open http://localhost:5173 in a browser and:
1. Fill out the patient symptom form and submit — you should get a
   ranked list of recommended doctors.
2. Switch to the **Hospital** tab — you should see a live dashboard
   with doctor/queue/capacity stats.
3. Switch to the **Analytics** tab — you should see network-wide
   charts.
4. Sign in as staff (see above) and try **"Simulate Emergency"** on the
   Hospital tab — you should see the queue reshuffle in real time.

If all four work, your local setup is fully functional.

---

## API keys & optional integrations

MediFlow works fully out of the box with **zero API keys** — every
external integration has a safe local fallback. Add keys only if you
want the upgraded behavior.

### Google Gemini (AI symptom classification)

**Without a key:** `/api/ai/classify` uses a local, deterministic
rule-based classifier (keyword matching against a specialty/urgency
table in `backend/ai.py`). This is what the automated tests exercise,
and it's genuinely fine for demo/dev use.

**With a key:** the same endpoint uses Gemini 1.5 Flash for more
nuanced free-text symptom understanding.

To add one:

1. Go to https://aistudio.google.com/app/apikey and sign in with a
   Google account.
2. Click **"Create API key"** — copy the generated key (starts with
   `AIza...`).
3. Add it to `backend/.env`:
   ```
   GEMINI_API_KEY=AIza...your-key-here
   ```
4. Restart the backend (`uvicorn main:app --reload` picks up the new
   env var on restart, not automatically).
5. Verify it's being used — check the backend logs when you call
   `/api/ai/classify`; with no key, `ai.py` logs a fallback notice.

**Docker Compose:** set `GEMINI_API_KEY` in `backend/.env` before
`docker compose up --build` — it's already wired through in
`docker-compose.yml`.

**Cost:** Gemini 1.5 Flash has a free tier generous enough for
development and small deployments. Check current limits at
https://ai.google.dev/pricing before relying on it for production
volume.

**Never commit a real key.** `backend/.env` is gitignored specifically
for this — only `backend/.env.example` (with blank/placeholder values)
should ever be committed.

### Adding a new third-party API integration

If you're extending MediFlow with another external API (SMS/email
notifications, a different AI provider, a mapping service, etc.), follow
the same pattern already established for Gemini:

1. **Add the setting** in `backend/config.py`:
   ```python
   MY_SERVICE_API_KEY: str = ""
   ```
2. **Add it to `backend/.env.example`** (blank, with a comment
   explaining what it's for and where to get one) and to
   `docker-compose.yml`'s backend `environment:` block.
3. **Read it from `settings`, never `os.environ` directly** — every
   other module imports `from config import settings` and uses
   `settings.MY_SERVICE_API_KEY`. This keeps all configuration
   discoverable in one place.
4. **Make it optional with a graceful fallback** if at all possible —
   the pattern in `ai.py` (try the real integration, catch the
   exception, fall back to local logic, log which path was taken) is
   the template. This keeps the app fully testable/runnable without
   every contributor needing every third-party account.
5. **Never log or return the key itself** in any response or error
   message — only log whether it's configured (boolean), not its value.

---

## Troubleshooting

**Frontend loads but shows no data / network errors in the console**
Check `frontend/.env` has the right `VITE_API_BASE_URL` and that the
backend is actually running (`curl http://localhost:8000/api/health`).
If you changed `frontend/.env` while `npm run dev` was already running,
restart it — Vite only reads `.env` at startup.

**`ModuleNotFoundError` when running the backend**
Your virtual environment probably isn't activated. Run
`source backend/.venv/bin/activate` (or the Windows equivalent) and
retry. You should see `(.venv)` in your terminal prompt when it's active.

**`bcrypt`/`passlib` errors on `pip install`**
This project pins `bcrypt==4.0.1` specifically because newer bcrypt
versions break `passlib`'s version detection. If you see a bcrypt
error, run `pip install -r requirements.txt` again fresh in a clean
virtual environment — don't `pip install bcrypt` separately first.

**Port already in use (`8000` or `5173`)**
Something else is already listening on that port. Either stop it, or
run on a different port:
```bash
uvicorn main:app --reload --port 8001
# and update VITE_API_BASE_URL in frontend/.env to match
```
```bash
npm run dev -- --port 5174
```

**CORS errors in the browser console**
The backend's `CORS_ORIGINS` (in `backend/.env`) must include whatever
origin the frontend is actually running on. The default already
includes `http://localhost:5173` — if you changed the frontend's port,
add the new one to `CORS_ORIGINS` (comma-separated) and restart the
backend.

**Admin login returns 401 with the documented default credentials**
The default admin account is only auto-created on the *first* backend
startup against an *empty* database. If you've already run the backend
before with a different `ADMIN_EMAIL`/`ADMIN_PASSWORD` in `.env`, the
account already exists with the old credentials. Either use those, or
delete `backend/mediflow.db` and restart to re-seed from scratch.

**Tests fail with doctor/specialty-not-found errors**
The test suite needs the reference dataset seeded first:
`rm -f mediflow.db && python seed.py`, then `pytest -v`.

**Still stuck?**
Check the backend terminal's logs — every request gets an
`X-Request-ID` and structured log line, which usually points straight
at the problem. For anything deeper, see
[docs/DEPLOYMENT.md](DEPLOYMENT.md) and [docs/API.md](API.md).
