# Deployment Guide

Three supported paths, from simplest to most production-grade:

1. **Docker Compose** — one command, full stack, on any VM/server you control.
2. **Managed platforms** — Railway/Render for the backend, GitHub Pages/Netlify/Vercel for the frontend (matches the project's original setup).
3. **Manual** — running each half yourself for full control.

---

## 1. Docker Compose (recommended for self-hosting)

Prerequisites: Docker + Docker Compose.

```bash
git clone <your-fork-url>
cd MediFlow-AI

cp backend/.env.example backend/.env
# Edit backend/.env:
#   - JWT_SECRET_KEY: generate with
#       python -c "import secrets; print(secrets.token_urlsafe(48))"
#   - ADMIN_PASSWORD: set a real password
#   - GEMINI_API_KEY: optional, for AI-powered triage

docker compose up --build
```

This starts:
- **Postgres** on `localhost:5432` (persisted in a named volume)
- **Backend API** on `http://localhost:8000` (docs at `/docs`)
- **Frontend** on `http://localhost:5173` (nginx-served static build)

The backend seeds the reference hospital/doctor dataset automatically on
first boot and creates the default admin account from your `.env`.

To update after pulling new code:

```bash
docker compose up --build -d
```

To reset the database:

```bash
docker compose down -v   # -v also removes the Postgres volume
```

### Scaling the backend

`docker-compose.yml` sets `WEB_CONCURRENCY: 2` (gunicorn workers). Bump
this for a busier deployment — a good starting point is
`(2 x CPU cores) + 1`.

---

## 2. Managed platforms (Railway / Render + GitHub Pages)

This mirrors the project's original deployment shape, hardened.

### One-click Render Blueprint

`render.yaml` in the repo root defines the full stack (Postgres +
backend + frontend) as a Render Blueprint:

1. From the Render dashboard: **New → Blueprint**, select your fork of
   this repo. Render reads `render.yaml` and proposes all three
   services.
2. Render prompts for `ADMIN_PASSWORD`, `CORS_ORIGINS`, and
   `VITE_API_BASE_URL` before the first deploy — leave `CORS_ORIGINS`
   and `VITE_API_BASE_URL` blank for now, you can't know either
   service's URL until it exists.
3. Deploy. Postgres and the backend come up first.
4. Once the backend is live, note its URL (Render dashboard →
   `mediflow-backend` → the URL at the top), then:
   - Set `CORS_ORIGINS` on the **backend** to the frontend's URL
     (also visible in the dashboard once it deploys) and save —
     Render redeploys automatically on env var changes.
   - Set `VITE_API_BASE_URL` on the **frontend** to
     `<backend-url>/api` and trigger a manual redeploy (Vite bakes
     this in at build time, so changing it requires a rebuild, not
     just a restart).
5. This two-step manual linking exists because Render can't know a
   service's own public URL before it's created — everything else
   (the database connection string, the JWT secret, the schema
   migration) is wired up automatically.

Honest caveat: this blueprint was written against Render's documented
schema and validated as syntactically correct YAML, but not
live-deployed to a Render account from the environment it was built
in. Render's blueprint schema has changed over the years; if the
parser rejects a field, check https://render.com/docs/blueprint-spec
against your account and adjust — the three-service shape (db +
backend + frontend) is the part that matters.

### Backend — Railway or Render (manual, without the Blueprint)

1. Create a new Web Service from your GitHub repo, root directory `/`
   (it will use the root `Dockerfile`).
2. Add a **Postgres** plugin/add-on; copy its connection string.
3. Set environment variables:
   | Key | Value |
   |---|---|
   | `DATABASE_URL` | the Postgres connection string from step 2 |
   | `JWT_SECRET_KEY` | a long random string (see command above) |
   | `ADMIN_EMAIL` / `ADMIN_PASSWORD` | your real admin credentials |
   | `ENV` | `production` |
   | `DEBUG` | `false` |
   | `CORS_ORIGINS` | your frontend's URL, e.g. `https://you.github.io` |
   | `GEMINI_API_KEY` | optional |
4. Deploy. The platform will build the Dockerfile and run the container's
   `CMD` (seed + gunicorn) automatically.
5. Note the public backend URL (e.g.
   `https://mediflow-ai-production.up.railway.app`).

### Frontend — GitHub Pages (via the included workflow)

`.github/workflows/deploy.yml` builds and publishes `frontend/` to the
`gh-pages` branch on every push to `main`.

To point it at your backend instead of the demo URL:

1. Repo **Settings → Secrets and variables → Actions → Variables**.
2. Add a repository variable `PROD_API_BASE_URL` = your backend URL +
   `/api`, e.g. `https://your-backend.example.com/api`.
3. Push to `main` — the next build picks it up automatically.
4. Enable GitHub Pages: **Settings → Pages → Source: `gh-pages` branch**.

### Frontend — Netlify / Vercel (alternative)

1. Import the repo, set the base directory to `frontend/`.
2. Build command: `npm run build`. Output directory: `dist`.
3. Add environment variable `VITE_API_BASE_URL` = your backend URL +
   `/api`.
4. Deploy.

---

## 3. Manual (no Docker)

For local development (not production), see the more detailed
**[docs/SETUP.md](SETUP.md)**, which covers this same path with
troubleshooting. Condensed version:

**Backend:**

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # edit as needed
alembic upgrade head   # create/update the database schema
python seed.py
uvicorn main:app --host 0.0.0.0 --port 8000 --reload   # dev
# or, for something closer to production:
gunicorn main:app --workers 2 --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000
```

**Frontend:**

```bash
cd frontend
cp .env.example .env   # set VITE_API_BASE_URL to your backend
npm install
npm run dev     # dev server on :5173
# or for a production build:
npm run build && npm run preview
```

---

---

---

## Backups & restore

`scripts/backup.sh` and `scripts/restore.sh` handle Postgres backups.
SQLite has no equivalent — another reason it's dev/demo-only.

```bash
# Backup (docker-compose's db service, or any Postgres via DATABASE_URL)
./scripts/backup.sh
DATABASE_URL=postgresql://user:pass@host:5432/db ./scripts/backup.sh

# Restore (destructive - prompts for confirmation)
./scripts/restore.sh backups/mediflow_20260101_000000.sql.gz
```

`backup.sh` keeps the last 14 backups in `./backups/` and prunes older
ones. For production, run it on a schedule (cron, a CI scheduled
workflow, or your platform's managed-Postgres snapshot feature if
using Railway/Render/RDS) and store the output somewhere durable off
the host — S3, a backup bucket, or your platform's own retention.

---

## Database migrations

Schema changes go through Alembic, not manual SQL or hand-editing the
database. See `backend/alembic/README.md` for the full guide. Quick
reference:

```bash
cd backend
alembic upgrade head                              # apply all pending migrations
alembic revision --autogenerate -m "add X column"  # after changing models.py
alembic current                                    # check what's applied
```

- **Docker Compose / the production Dockerfile**: migrations run
  automatically on container startup, before the server starts
  accepting traffic (see the Dockerfile `CMD`).
- **Manual/Railway/Render deployments**: run `alembic upgrade head`
  yourself as part of your deploy step, before starting the server.
- **First-ever deploy** against a brand-new empty database: migrations
  create the full schema from scratch — there's no separate "initial
  setup" step.

---

## Post-deploy checklist

The app validates most of this itself: with `ENV=production`, it
**refuses to start** if `JWT_SECRET_KEY`/`ADMIN_PASSWORD` are still
placeholders, `CORS_ORIGINS` contains `*`, or `DATABASE_URL` is SQLite.
Check anyway:

- [ ] `JWT_SECRET_KEY` is a real random value, not the placeholder
- [ ] `ADMIN_PASSWORD` has been changed from the default
- [ ] `DEBUG=false` and `ENV=production` in the backend environment
- [ ] `CORS_ORIGINS` lists only the frontend domains you actually use
- [ ] `ALLOWED_HOSTS` is set to your real API hostname (defaults to `*`,
      which disables Host-header validation — fine for local dev only)
- [ ] `DATABASE_URL` points at Postgres, not the SQLite file (SQLite is
      fine for a demo, not for concurrent production traffic)
- [ ] `GET /api/health/ready` returns `"status":"ready"` from the public URL
- [ ] `POST /api/auth/login` works with your real admin credentials
- [ ] `GET /metrics` is reachable only from your monitoring network (it's
      unauthenticated by design, like a standard Prometheus target — put
      it behind a reverse-proxy rule or VPC boundary, not the public internet)
- [ ] Frontend loads and successfully calls the backend (check the
      Network tab for CORS errors if not)
