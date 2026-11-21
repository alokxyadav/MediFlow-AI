# Roadmap — Next Phases

This pass (Phase 0 → call it "Production Hardening") made the existing
prototype safe to deploy: auth, validation, rate limiting, logging,
Docker, docs, tests. It deliberately did **not** touch the core
matching/scheduling algorithms or redesign the UI — those already work
well. What follows is a realistic, ordered plan for what to build next,
grouped into phases. Each phase is scoped to be shippable on its own;
you don't need to do them in order, but later phases generally assume
earlier ones exist.

---

## Phase 1 — Close the auth loop ✅ done

- [x] Protect the remaining operational endpoints (`/api/events/*`,
      `/api/optimization/reallocate`) — done via `require_role()`
- [x] Role check (`staff` vs `admin`) — staff can book/cancel/simulate,
      only admins can reallocate, export, or create staff accounts
- [x] Frontend login modal + in-memory token storage (not localStorage)
      + `Authorization` header attached automatically to protected calls,
      with automatic re-prompt on 401/403
- [x] Sliding-session refresh (`POST /api/auth/refresh`)
- [x] Self-service password change (`POST /api/auth/change-password`)
- [x] Audit log (`GET /api/admin/audit-log`) — every authenticated
      mutating request is logged automatically via middleware
- [ ] Full password-reset-by-email flow (current change-password requires
      knowing the old password — there's no "forgot password" path yet,
      since that needs email infrastructure, see Phase 3)
- [x] Token revocation — done. `POST /api/auth/logout` immediately
      invalidates the current token server-side (`revoked_tokens` table,
      checked on every authenticated request), rather than only clearing
      it client-side. Verified live through the real UI: token rejected
      immediately after clicking "Sign out", a fresh login unaffected.
      Refresh (`/api/auth/refresh`) is still simple re-signing, not a
      separate rotating refresh-token family — fine for a shift-based
      staff workflow, not yet a fully hardened session model.

## Phase 2 — Real patient identity & history

Right now a "patient" is just a name typed into a form each visit — no
persistent identity, no history, no way to see "my past appointments."

- [ ] Patient accounts (email/phone + OTP, not a full password system)
- [ ] Link appointments to a persistent `patient_id` across visits
- [ ] Patient-facing "my appointments" view (upcoming, past, cancel)
- [ ] Medical history notes attached to a patient record (careful: this
      is real PHI — see Phase 6 on compliance before storing anything
      sensitive)

## Phase 3 — Notifications

The system currently has no way to tell anyone anything happened.

- [ ] SMS/email confirmation on booking (Twilio / SendGrid — start with
      email, it's simpler and cheaper)
- [ ] Reminder N hours before an appointment
- [ ] Real-time "your appointment was shifted because of an emergency"
      notification instead of the patient finding out by checking the app
- [ ] Doctor-facing notification when their schedule changes
- [ ] In-app notification center (bell icon, unread count) backed by the
      new admin auth infra

## Phase 4 — Live updates instead of polling

The dashboard currently re-fetches on demand. For a hospital ops screen
that's meant to be glanced at all day, push is better than pull.

- [ ] WebSocket or SSE channel for appointment/queue changes
- [ ] Live-updating hospital dashboard (no manual refresh needed)
- [ ] Live queue position for a patient waiting in the app ("3 people
      ahead of you, ~22 min")

## Phase 5 — Data layer maturity

- [x] Alembic migrations — done. `alembic upgrade head` creates/evolves
      the schema; wired into the Dockerfile, CI, and `make migrate` /
      `make migration`. See `backend/alembic/README.md`.
- [x] Database backups + restore runbook — `scripts/backup.sh` /
      `scripts/restore.sh`, documented in `docs/DEPLOYMENT.md`.
- [ ] Soft-delete / archival for old appointments instead of unbounded
      table growth
- [ ] Full-text search on doctor name/specialty for larger doctor
      catalogs (the current dataset is small enough that `ilike` is fine;
      it won't scale past a few thousand rows)

## Phase 6 — Compliance & data protection

Before this ever touches real patient data:

- [ ] Data retention policy + automated purge of old PII
- [ ] Encryption at rest for the database (managed Postgres providers
      typically offer this — verify, don't assume)
- [ ] Access logging specifically for PHI reads (separate from general
      request logging)
- [ ] Legal review: HIPAA (if used in the US) or India's DPDP Act (given
      the Kanpur-region dataset) — this is a legal/compliance
      conversation, not just an engineering task, and needs to happen
      before Phase 2's patient history feature goes anywhere near real
      patients

## Phase 7 — Observability & reliability

- [ ] Error tracking (Sentry or similar) wired into the existing
      exception handlers in `main.py` — the hooks are already there,
      just needs an SDK + DSN
- [ ] Metrics (request latency, booking success rate, queue-shift
      frequency) exported to Prometheus/Grafana or a hosted equivalent
- [ ] Uptime monitoring hitting `/api/health` from outside the cluster
- [ ] Load testing the matching/scheduling endpoints specifically —
      they're the most computationally interesting part of the system
      and the ones most likely to slow down under concurrent load
- [ ] Graceful degradation: if Gemini is down, confirm the local
      fallback classifier kicks in under real traffic, not just in tests

## Phase 8 — Frontend scale-up

`App.jsx` is currently a single ~2,400-line file. That's fine at the
current feature count; it won't stay fine.

- [ ] Split into route-based components (patient flow / dashboard /
      admin) once a router is introduced
- [ ] Extract the fetch calls into a shared API client (auth headers,
      retries, error normalization in one place instead of copy-pasted
      per call)
- [ ] Component-level tests (Vitest + React Testing Library) — there are
      currently zero frontend tests, only backend
- [ ] Code-splitting (the production build already warns about a >500KB
      chunk — `React.lazy` on the dashboard/admin views would help)
- [ ] Accessibility pass (keyboard navigation, ARIA labels on the
      symptom-intake form, color contrast on priority badges)

## Phase 9 — Multi-hospital / multi-region growth

The dataset and matching logic currently assume one metro area
(Kanpur). Expanding beyond that:

- [ ] Configurable service regions instead of hardcoded coordinates
- [ ] Per-hospital admin scoping (a hospital's staff shouldn't see or
      manage another hospital's doctors)
- [ ] Bulk doctor/hospital onboarding (CSV upload in the admin UI,
      instead of editing the seed CSVs by hand)

---

## Suggested order if you're doing this alone

1. Phase 1 (finish the auth story — highest security value, lowest effort)
2. Phase 7 partial (error tracking — cheap, catches problems fast)
3. Phase 3 (notifications — highest visible user value)
4. Phase 2 (patient identity — needed before Phase 6 matters)
5. Phase 6 (compliance — mandatory *before* any real patient data)
6. Everything else, driven by actual usage feedback

Don't build Phase 4 (real-time) or Phase 9 (multi-region) speculatively
— both are meaningfully easier to add once Phase 1–3 exist and you have
a clearer picture of real usage patterns.
