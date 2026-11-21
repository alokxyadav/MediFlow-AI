# MediFlow AI — API Reference

Base URL (local): `http://localhost:8000`
Interactive docs (Swagger UI): `http://localhost:8000/docs`
Interactive docs (ReDoc): `http://localhost:8000/redoc`

All request/response bodies are JSON unless noted otherwise (CSV export is
the one exception). All endpoints are prefixed `/api` except the health
and root endpoints.

---

## Authentication

Most endpoints are public (patient-facing, matches the original app's
design). A small set of **admin** endpoints require a JWT bearer token.

### Log in

```
POST /api/auth/login
Content-Type: application/json

{
  "email": "admin@mediflow.ai",
  "password": "ChangeMe123!"
}
```

Response:

```json
{
  "access_token": "eyJhbGciOi...",
  "token_type": "bearer",
  "expires_in_minutes": 720
}
```

Use the token on protected routes:

```
Authorization: Bearer eyJhbGciOi...
```

Rate limited to 10 requests/minute per IP to slow down credential
stuffing.

### Who am I

```
GET /api/auth/me
Authorization: Bearer <token>
```

### Refresh a token

```
POST /api/auth/refresh
Authorization: Bearer <still-valid-token>
```

Returns a fresh token with a renewed expiry. Sliding-session style — no
separate refresh-token family, just re-issue while the current token is
still valid.

### Log out

```
POST /api/auth/logout
Authorization: Bearer <token>
```

Immediately invalidates the token server-side (stored in the
`revoked_tokens` table, checked on every authenticated request) —
logging out is not just the frontend forgetting the token. A copied or
intercepted token is rejected as soon as its owner logs out, not only
when it would have expired naturally.

### Change your own password

```
POST /api/auth/change-password
Authorization: Bearer <token>
Content-Type: application/json

{ "current_password": "old", "new_password": "at-least-8-chars" }
```

Rate limited to 5/minute.

### Create a staff or admin account (admin only)

```
POST /api/admin/staff
Authorization: Bearer <admin-token>
Content-Type: application/json

{ "email": "reception@yourhospital.com", "role": "staff" }
```

If `temporary_password` is omitted, one is generated and returned once —
capture it immediately, it isn't recoverable afterward.

```json
{ "email": "reception@yourhospital.com", "role": "staff", "temporary_password": "aZ8f..." }
```

### Roles

| Role | Can do |
|---|---|
| `staff` | book/cancel appointments, simulate emergency/doctor-unavailable, toggle doctor availability |
| `admin` | everything `staff` can, plus reallocate queues, export CSV data, create staff accounts, view the audit log |

> The default admin account is auto-created on first startup from the
> `ADMIN_EMAIL` / `ADMIN_PASSWORD` environment variables. **Change the
> password before deploying** — see `backend/.env.example`.

---

## Health & meta

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/` | none | Service banner + links |
| GET | `/api/health` | none | Combined status + DB check (backward compatible) |
| GET | `/api/health/live` | none | Liveness probe — never touches the DB |
| GET | `/api/health/ready` | none | Readiness probe — checks DB, 503 if unreachable |
| GET | `/api/version` | none | App version / environment |
| GET | `/metrics` | none | Prometheus-format request count + latency (disable with `METRICS_ENABLED=false`) |

```bash
curl http://localhost:8000/api/health
# {"status":"healthy","service":"MediFlow AI Backend","version":"2.1.0",
#  "environment":"production","database":"ok","time":"2026-08-23T12:00:00+00:00"}
```

Use `/api/health/live` and `/api/health/ready` for Kubernetes-style probes
— liveness should never depend on the database (a slow DB shouldn't get
a healthy pod killed), readiness should.

---

## AI symptom classification

```
POST /api/ai/classify
Content-Type: application/json

{ "symptoms": "chest pain and breathing difficulty" }
```

```json
{ "specialty": "Cardiology", "urgency": "HIGH", "confidence": 0.91 }
```

Uses Google Gemini if `GEMINI_API_KEY` is set; otherwise falls back
automatically to a local rule-based classifier — the endpoint always
works, key or no key. Rate limited (default 20/minute per IP).

---

## Patient intake — end-to-end recommendation

```
POST /api/patient/request
Content-Type: application/json

{
  "name": "Asha Verma",
  "age": 34,
  "symptoms": "fever and body ache",
  "location": "Kalyanpur",
  "preferred_date": "2026-08-25",
  "preferred_time": "10:00"
}
```

Runs the full pipeline: AI triage → location resolution → doctor
matching/ranking → best-slot recommendation. Returns the classification,
resolved coordinates, ranked doctor list, and a single best
`appointment_recommendation`. Rate limited (default 10/minute per IP).

Field validation: `age` 0-130, `symptoms`/`name`/`location` non-blank,
`preferred_date` must be `YYYY-MM-DD` and not in the past, `preferred_time`
must be `HH:MM` (24h). `POST /api/appointments` additionally requires
`appointment_time` in `YYYY-MM-DD HH:MM` format on a date that is not in the
past. The past-date cutoff is one day before the server's current UTC date, so
users in timezones behind UTC are never rejected for their own "today".
Invalid input returns `422` with a `detail` array describing each
problem.

---

## Doctors

```
GET /api/doctors?specialty=Cardiology&available_only=true&page=1&page_size=50
```

All query params are optional; omitting them returns the same shape the
original app used (backward compatible).

```
GET /api/doctors/recommend?location=Kalyanpur&specialty=Cardiology
```

Ranked, distance/workload/rating-aware doctor recommendations with an
escalation path if nothing is found nearby.

---

## Hospitals

```
GET /api/hospitals/nearby?location=Kalyanpur&radius_km=10
```

---

## Appointments

```
POST /api/appointments          # book (priority-aware slot optimization)
GET  /api/appointments/{id}     # fetch one
GET  /api/appointments?status=BOOKED&doctor_id=D001&page=1&page_size=100
```

`GET /api/appointments` supports optional `status`, `doctor_id`,
`hospital_id` filters and pagination — all optional and backward
compatible with the original unfiltered call.

**Patient privacy:** both read endpoints are public, but patient identity is
not. Anonymous callers receive names reduced to first name plus last initial
(`Priya Singh` becomes `Priya S.`) and `patient_id` is `null`. Requests that
carry a valid staff or admin bearer token receive full names. An invalid,
expired or revoked token is treated as anonymous rather than rejected, so the
public queue view never errors because of a stale session.

---



---

## Operational events (hospital-ops simulation)

**Require a Bearer token** (`staff` or `admin` role, see Roles above):

```
POST /api/events/emergency            # simulate an emergency intake
POST /api/events/doctor-unavailable   # simulate a doctor going offline
POST /api/events/cancel               # cancel + pull the queue forward
```

**Admin only:**

```
POST /api/optimization/reallocate     # rebalance load across doctors/hospitals
```

See `/docs` for exact request bodies — these mirror the original app's
contracts unchanged, aside from now requiring auth.

## Analytics

```
GET /api/hospitals/{id}/dashboard
GET /api/analytics
```

Aggregate stats (utilization, waiting time, workload, priority mix) for
charts — unchanged from the original app.

---

## Admin (requires Bearer token)

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/api/admin/appointments/export` | admin | Download appointments as CSV |
| PATCH | `/api/admin/doctors/{doctor_id}/availability` | staff/admin | Manually override a doctor's availability |
| GET | `/api/admin/dashboard-summary` | staff/admin | Cheap, PII-free counts for a staff view |
| POST | `/api/admin/staff` | admin | Create a new staff/admin account |
| GET | `/api/admin/audit-log` | admin | View the append-only audit trail |

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@mediflow.ai","password":"ChangeMe123!"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

curl -H "Authorization: Bearer $TOKEN" \
  -o appointments.csv \
  "http://localhost:8000/api/admin/appointments/export"

curl -X PATCH "http://localhost:8000/api/admin/doctors/D001/availability" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"available_now": false}'
```

Extending auth to more routes: import `get_current_admin` from
`backend/auth.py` and add `admin: AdminUser = Depends(get_current_admin)`
as a parameter to any route in `backend/main.py` you want to lock down.

---

## Errors

All error responses share a consistent envelope:

```json
{ "error": "Validation failed", "detail": [ { "loc": ["body", "age"], "msg": "...", "type": "..." } ] }
```

| Status | Meaning |
|---|---|
| 400 | Bad request (e.g. missing symptoms text) |
| 401 | Missing/invalid/expired admin token |
| 404 | Resource not found |
| 422 | Request body failed validation |
| 429 | Rate limit exceeded |
| 503 | Database temporarily unavailable |
| 500 | Unhandled server error (logged with a request ID for debugging) |

Every response includes an `X-Request-ID` header — include it when
reporting a bug so the matching server log line can be found instantly.
