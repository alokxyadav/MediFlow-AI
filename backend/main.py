import csv
import io
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

import schemas
from ai import classify_symptoms
from auth import (
    authenticate_admin,
    create_access_token,
    ensure_default_admin,
    get_current_admin,
    get_optional_admin,
    hash_password,
    mask_patient_name,
    oauth2_scheme,
    purge_expired_revocations,
    require_role,
    revoke_token,
    validate_password_strength,
    verify_password,
)
from config import settings, validate_production_safety
from database import Base, SessionLocal, engine, get_db
from logging_config import configure_logging
from matching import (
    find_and_rank_doctors,
    resolve_location_to_coords,
)
from metrics import METRICS_CONTENT_TYPE, metrics_middleware, render_metrics
from models import AdminUser, Appointment, AuditLog, Doctor, Hospital, Patient
from scheduler import (
    calculate_appointment_slot,
    cancel_appointment_and_pull_forward,
    preview_appointment_slot,
)

# ============================================================
# LOGGING
# ============================================================

configure_logging()
logger = logging.getLogger("mediflow")


# ============================================================
# STARTUP / SHUTDOWN (lifespan)
# ============================================================

def _wait_for_database(timeout_seconds: int) -> None:
    """Retry the DB connection with backoff instead of crashing on the
    first attempt. Matters most in docker-compose / fresh deploys where
    the API container can start slightly before the database is ready
    to accept connections, even behind a healthcheck."""
    deadline = time.monotonic() + timeout_seconds
    attempt = 0
    delay = 0.5

    while True:
        attempt += 1
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            if attempt > 1:
                logger.info("Database reachable after %d attempt(s)", attempt)
            return
        except OperationalError as exc:
            if time.monotonic() >= deadline:
                logger.error(
                    "Database still unreachable after %ds (%d attempts): %s",
                    timeout_seconds, attempt, exc,
                )
                raise
            logger.warning(
                "Database not ready (attempt %d): %s - retrying in %.1fs",
                attempt, exc, delay,
            )
            time.sleep(delay)
            delay = min(delay * 1.7, 5.0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- startup ---
    validate_production_safety()
    _wait_for_database(settings.DB_STARTUP_RETRY_SECONDS)
    # create_all can race under concurrent worker startup (two gunicorn
    # workers both see a table missing and both issue CREATE TABLE at
    # once) - most backends handle this fine, but SQLite in particular
    # can raise "table already exists" in that window. Treat it the same
    # way as the admin-seeding race below: harmless, another worker beat
    # us to it.
    #
    # This is a zero-config convenience for local dev and the test suite
    # only - it can create missing tables but can never alter an
    # existing one. Production deployments run `alembic upgrade head`
    # before this even starts (see the Dockerfile CMD and
    # backend/alembic/README.md) - by the time this line runs there,
    # every table already exists and create_all() is a no-op.
    try:
        Base.metadata.create_all(bind=engine)
    except OperationalError as exc:
        if "already exists" not in str(exc).lower():
            raise
        logger.info("Schema already created by another worker process")

    db = SessionLocal()
    try:
        ensure_default_admin(db)
        purged = purge_expired_revocations(db)
        if purged:
            logger.info("Purged %d expired token-revocation record(s)", purged)
    finally:
        db.close()

    logger.info(
        "%s v%s starting in '%s' mode (rate_limit=%s, metrics=%s)",
        settings.APP_NAME, settings.APP_VERSION, settings.ENV,
        settings.RATE_LIMIT_ENABLED, settings.METRICS_ENABLED,
    )

    if settings.SENTRY_DSN:
        try:
            import sentry_sdk
            sentry_sdk.init(dsn=settings.SENTRY_DSN, environment=settings.ENV,
                             traces_sample_rate=0.1)
            logger.info("Sentry error tracking initialized")
        except ImportError:
            logger.warning(
                "SENTRY_DSN is set but sentry-sdk is not installed; "
                "add it to requirements.txt to enable error tracking"
            )

    yield
    # --- shutdown ---
    logger.info("%s shutting down", settings.APP_NAME)


# ============================================================
# APP CONFIGURATION
# ============================================================

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="AI Dynamic Appointment & Hospital Resource Optimization Engine",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)


# ============================================================
# RATE LIMITING
# ============================================================

limiter = Limiter(
    key_func=get_remote_address,
    enabled=settings.RATE_LIMIT_ENABLED,
    default_limits=[settings.RATE_LIMIT_DEFAULT],
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ============================================================
# HOST HEADER PROTECTION
# ============================================================
# Blocks Host-header-poisoning / cache-poisoning style attacks. Defaults
# to "*" (disabled) for local dev; set ALLOWED_HOSTS in production.

if settings.allowed_hosts_list != ["*"]:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts_list)


# ============================================================
# RESPONSE COMPRESSION
# ============================================================

app.add_middleware(GZipMiddleware, minimum_size=1000)


# ============================================================
# CORS
# ============================================================
# IMPORTANT:
# Do NOT use allow_origins=["*"] together with allow_credentials=True.
# Allowed origins are driven by the CORS_ORIGINS env var - see .env.example.

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# REQUEST BODY SIZE LIMIT
# ============================================================
# Rejects oversized request bodies before they're read into memory -
# cheap protection against accidental or malicious huge payloads on a
# JSON API that never legitimately needs more than ~1MB per request.

@app.middleware("http")
async def body_size_limit_middleware(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > settings.MAX_REQUEST_BODY_BYTES:
        return JSONResponse(
            status_code=413,
            content={"error": f"Request body exceeds {settings.MAX_REQUEST_BODY_BYTES} bytes"},
        )
    return await call_next(request)


# ============================================================
# METRICS (Prometheus-compatible)
# ============================================================

if settings.METRICS_ENABLED:
    app.middleware("http")(metrics_middleware)

    @app.get("/metrics", include_in_schema=False)
    def metrics_endpoint():
        return Response(render_metrics(), media_type=METRICS_CONTENT_TYPE)


# ============================================================
# REQUEST LOGGING + SECURITY HEADERS MIDDLEWARE
# ============================================================

@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    request_id = str(uuid.uuid4())[:8]
    start = time.perf_counter()

    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "Unhandled error [%s] %s %s", request_id, request.method, request.url.path
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": "Internal server error",
                "request_id": request_id,
            },
        )

    duration_ms = round((time.perf_counter() - start) * 1000, 1)
    logger.info(
        "[%s] %s %s -> %s (%sms)",
        request_id,
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )

    # Audit trail: if a protected route ran (get_current_admin stashed the
    # acting admin's email on request.state) and the request mutated
    # something (non-GET) and succeeded, record it. This is centralized
    # here rather than in each route so no protected endpoint can forget
    # to log itself.
    acting_admin_email = getattr(request.state, "acting_admin_email", None)
    if (
        acting_admin_email
        and request.method != "GET"
        and 200 <= response.status_code < 400
    ):
        db = SessionLocal()
        try:
            db.add(
                AuditLog(
                    admin_email=acting_admin_email,
                    action=f"{request.method} {request.url.path}",
                    detail=str(request.url.query)[:2000] if request.url.query else None,
                    ip_address=request.client.host if request.client else None,
                )
            )
            db.commit()
        except Exception:
            logger.exception("Failed to write audit log for [%s]", request_id)
            db.rollback()
        finally:
            db.close()

    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if settings.is_production:
        response.headers["Strict-Transport-Security"] = (
            "max-age=63072000; includeSubDomains"
        )
    return response


# ============================================================
# GLOBAL EXCEPTION HANDLERS
# ============================================================

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # exc.errors() can contain non-JSON-serializable objects (e.g. the raw
    # ValueError raised inside a validator, under "ctx"). jsonable_encoder
    # with a fallback stringifier keeps this endpoint from ever 500'ing.
    safe_errors = jsonable_encoder(exc.errors(), custom_encoder={Exception: str})
    return JSONResponse(
        status_code=422,
        content={"error": "Validation failed", "detail": safe_errors},
    )


@app.exception_handler(SQLAlchemyError)
async def db_exception_handler(request: Request, exc: SQLAlchemyError):
    logger.exception("Database error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=503,
        content={"error": "Database temporarily unavailable"},
    )


# ============================================================
# BOOKING CONCURRENCY SAFETY
# ============================================================
# Two patients hitting "book with Dr. X at 10:00" at the same instant
# must not both succeed. Two defenses, layered:
#   1. lock_doctor_for_booking() takes a row lock on the doctor (Postgres/
#      MySQL) so concurrent booking requests for the *same* doctor are
#      serialized - the second request's slot-optimization logic runs
#      only after the first has committed, so it correctly sees the slot
#      as taken and picks a different one.
#   2. A partial unique DB index (models.py) as a backstop for anything
#      the lock doesn't cover (SQLite has no real row locking - see
#      database.py's docstring on why Postgres is required for real
#      concurrent traffic).
# generate_appointment_id() also switches to a UUID-based ID: the
# original timestamp-modulo scheme could produce the same ID for two
# requests arriving within the same millisecond.

def lock_doctor_for_booking(db: Session, doctor_id: str):
    """Fetch the Doctor row, locking it (SELECT ... FOR UPDATE) when the
    backing database supports row-level locks. No-op lock on SQLite,
    where FOR UPDATE isn't meaningful - the unique index is the
    remaining safety net there."""
    query = db.query(Doctor).filter(Doctor.doctor_id == doctor_id)
    if db.bind.dialect.name != "sqlite":
        query = query.with_for_update()
    return query.first()


def generate_appointment_id(prefix: str = "A") -> str:
    """Collision-resistant appointment ID. Short enough to stay readable
    in the UI/CSV export, long enough (36^8 ≈ 2.8 trillion combinations)
    that a same-millisecond collision under real traffic is effectively
    impossible - unlike the previous timestamp-modulo scheme. `prefix`
    preserves the existing convention ("A" for normal bookings, "E" for
    emergency intakes) so IDs stay visually distinguishable.
    """
    return f"{prefix}{uuid.uuid4().hex[:8].upper()}"


# ============================================================
# ROOT / HEALTH CHECK
# ============================================================

@app.get("/")
def root():
    return {
        "service": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "docs": "/docs",
        "health": "/api/health",
    }


@app.get("/api/health")
def health_check(db: Session = Depends(get_db)):
    """Combined health check - kept for backward compatibility with the
    original app and simple uptime monitors. For Kubernetes-style probes,
    prefer /api/health/live and /api/health/ready below: a liveness probe
    should NOT depend on the database (a slow DB shouldn't get a healthy
    pod killed), but a readiness probe should."""
    db_status = "ok"
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        db_status = "unreachable"

    return {
        "status": "healthy" if db_status == "ok" else "degraded",
        "service": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "environment": settings.ENV,
        "database": db_status,
        "time": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/health/live")
def liveness_check():
    """Is the process up and able to handle requests at all? Never
    touches the database - a DB outage should not cause an orchestrator
    to kill and restart otherwise-healthy application pods."""
    return {"status": "alive"}


@app.get("/api/health/ready")
def readiness_check(db: Session = Depends(get_db)):
    """Is this instance ready to receive traffic? Checks the DB - used
    by load balancers/orchestrators to decide whether to route requests
    here (e.g. during rolling deploys or a DB failover)."""
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Database unreachable") from exc
    return {"status": "ready"}


@app.get("/api/version")
def get_version():
    return {"version": settings.APP_VERSION, "environment": settings.ENV}


# ============================================================
# AUTH
# ============================================================

@app.post("/api/auth/login", response_model=schemas.TokenResponse)
@limiter.limit("10/minute")
def admin_login(
    request: Request,
    credentials: schemas.AdminLoginRequest,
    db: Session = Depends(get_db),
):
    """Authenticate a staff/admin account and receive a JWT bearer token.

    Use it as: `Authorization: Bearer <access_token>` on protected routes
    (e.g. /api/appointments/export, /api/admin/*).
    """
    admin = authenticate_admin(db, credentials.email, credentials.password)
    if not admin:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = create_access_token(subject=admin.email)
    return schemas.TokenResponse(
        access_token=token,
        expires_in_minutes=settings.JWT_EXPIRE_MINUTES,
    )


@app.get("/api/auth/me", response_model=schemas.AdminProfile)
def get_my_profile(admin: AdminUser = Depends(get_current_admin)):
    return schemas.AdminProfile(
        email=admin.email, full_name=admin.full_name, role=admin.role
    )


@app.post("/api/auth/refresh", response_model=schemas.TokenResponse)
@limiter.limit("20/minute")
def refresh_token(request: Request, admin: AdminUser = Depends(get_current_admin)):
    """Exchange a still-valid access token for a fresh one, extending the
    session without requiring the password again. This is a simple
    sliding-session refresh (not a separate refresh-token family) - call
    it periodically from the frontend while the admin is active."""
    token = create_access_token(subject=admin.email)
    return schemas.TokenResponse(
        access_token=token,
        expires_in_minutes=settings.JWT_EXPIRE_MINUTES,
    )


@app.post("/api/auth/logout")
@limiter.limit("20/minute")
def logout(
    request: Request,
    admin: AdminUser = Depends(get_current_admin),  # enforces the token is currently valid
    token: Optional[str] = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
):
    """Immediately invalidate the current token server-side, rather than
    relying on the frontend simply discarding it. Without this, a token
    copied out of the browser (or a device left signed in) stayed usable
    for up to JWT_EXPIRE_MINUTES after the user believed they'd logged
    out - this closes that gap."""
    revoke_token(db, token)
    logger.info("Admin %s logged out (token revoked)", admin.email)
    return {"status": "logged out"}


@app.post("/api/auth/change-password")
@limiter.limit("5/minute")
def change_password(
    request: Request,
    payload: schemas.ChangePasswordRequest,
    admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Self-service password change - requires the current password."""
    if not verify_password(payload.current_password, admin.hashed_password):
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    weakness = validate_password_strength(payload.new_password)
    if weakness:
        raise HTTPException(status_code=422, detail=weakness)

    admin.hashed_password = hash_password(payload.new_password)
    db.commit()
    logger.info("Admin %s changed their password", admin.email)
    return {"status": "password updated"}


@app.post(
    "/api/admin/staff",
    response_model=schemas.CreateStaffResponse,
    status_code=201,
)
@limiter.limit("10/minute")
def create_staff_account(
    request: Request,
    payload: schemas.CreateStaffRequest,
    admin: AdminUser = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Admin-only: create a new staff or admin account. Returns the
    temporary password once - it is not recoverable afterwards, so the
    caller must relay it to the new user out of band."""
    import secrets as _secrets

    existing = db.query(AdminUser).filter(AdminUser.email == payload.email.lower()).first()
    if existing:
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    if payload.temporary_password:
        weakness = validate_password_strength(payload.temporary_password)
        if weakness:
            raise HTTPException(status_code=422, detail=weakness)
        temp_password = payload.temporary_password
    else:
        # token_urlsafe is long enough to satisfy length, but isn't
        # guaranteed to contain both a letter and a digit - regenerate
        # until it does rather than relaxing the policy for this path.
        temp_password = _secrets.token_urlsafe(12)
        while validate_password_strength(temp_password):
            temp_password = _secrets.token_urlsafe(12)

    new_user = AdminUser(
        email=payload.email.lower(),
        hashed_password=hash_password(temp_password),
        full_name=payload.full_name,
        role=payload.role,
        is_active=True,
    )
    db.add(new_user)
    db.commit()

    logger.info("Admin %s created new %s account: %s", admin.email, payload.role, new_user.email)

    return schemas.CreateStaffResponse(
        email=new_user.email,
        role=new_user.role,
        temporary_password=temp_password,
    )


@app.get("/api/admin/audit-log", response_model=List[schemas.AuditLogEntry])
def get_audit_log(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    admin: AdminUser = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Admin-only: view the append-only audit trail of sensitive actions
    (cancellations, reallocations, availability overrides, exports,
    staff account changes)."""
    entries = (
        db.query(AuditLog)
        .order_by(AuditLog.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return [
        schemas.AuditLogEntry(
            id=e.id,
            admin_email=e.admin_email,
            action=e.action,
            detail=e.detail,
            created_at=e.created_at.isoformat() if e.created_at else "",
        )
        for e in entries
    ]


# ============================================================
# AI CLASSIFICATION
# ============================================================

@app.post("/api/ai/classify", response_model=schemas.AIClassifyResponse)
@limiter.limit(settings.RATE_LIMIT_CLASSIFY)
def classify_patient_symptoms(request: Request, payload: Dict[str, str] = Body(...)):
    symptoms = payload.get("symptoms")

    if not symptoms or not symptoms.strip():
        raise HTTPException(
            status_code=400,
            detail="Symptoms text required"
        )

    result = classify_symptoms(symptoms.strip())

    return result


# ============================================================
# PATIENT REQUEST
# ============================================================

@app.post("/api/patient/request")
@limiter.limit(settings.RATE_LIMIT_BOOKING)
def patient_request(
    request: Request,
    req: schemas.PatientRequest,
    db: Session = Depends(get_db)
):
    """
    Registers patient request.

    Pipeline:
    1. AI specialty + urgency classification
    2. Location resolution
    3. Patient creation
    4. Doctor/hospital matching
    5. Appointment slot optimization
    """

    # --------------------------------------------------------
    # 1. AI CLASSIFICATION
    # --------------------------------------------------------

    ai_res = classify_symptoms(req.symptoms)

    # --------------------------------------------------------
    # 2. LOCATION
    # --------------------------------------------------------

    lat, lon = resolve_location_to_coords(req.location)

    # --------------------------------------------------------
    # 3. CREATE PATIENT
    # --------------------------------------------------------

    db_patient = Patient(
        name=req.name,
        age=req.age,
        symptoms=req.symptoms,
        location_name=req.location,
        latitude=lat,
        longitude=lon,
        preferred_date=req.preferred_date,
        preferred_time=req.preferred_time,
    )

    db.add(db_patient)
    db.commit()
    db.refresh(db_patient)

    # --------------------------------------------------------
    # 4. FIND DOCTORS
    # --------------------------------------------------------

    recommendations = find_and_rank_doctors(
        db,
        lat,
        lon,
        ai_res["specialty"]
    )

    # --------------------------------------------------------
    # 5. OPTIMIZE APPOINTMENT SLOT
    # --------------------------------------------------------

    appointment_recommendation = None

    preferred_datetime = (
        f"{req.preferred_date} {req.preferred_time}"
    )

    available_candidates = [
        doctor
        for doctor in recommendations.get("doctors", [])
        if doctor["available_now"]
    ]

    candidate_slots = []

    for doctor in available_candidates:

        try:
            slot = preview_appointment_slot(
                db=db,
                doctor_id=doctor["doctor_id"],
                preferred_time_str=preferred_datetime,
                duration_mins=doctor["consultation_duration"],
            )

        except (ValueError, TypeError, KeyError):
            continue

        # Waiting-time penalty.
        # Every 20 minutes of waiting reduces effective score by 1.
        wait_penalty = min(
            slot["wait_minutes"] / 20,
            20
        )

        effective_score = round(
            doctor["score"] - wait_penalty,
            1
        )

        candidate_slots.append({
            **doctor,
            **slot,
            "effective_score": effective_score,
        })

    if candidate_slots:

        candidate_slots.sort(
            key=lambda candidate: (
                -candidate["effective_score"],
                candidate["wait_minutes"],
                candidate["distance_km"],
            )
        )

        best = candidate_slots[0]

        if best["wait_minutes"] == 0:
            delay_description = "at the preferred time"
        else:
            delay_description = (
                f"after an estimated "
                f"{best['wait_minutes']}-minute delay"
            )

        appointment_recommendation = {
            "doctor_id": best["doctor_id"],
            "doctor_name": best["name"],
            "hospital_id": best["hospital"]["hospital_id"],
            "hospital_name": best["hospital"]["name"],
            "appointment_time": best["scheduled_time"],
            "estimated_wait_minutes": best["wait_minutes"],
            "effective_score": best["effective_score"],
            "reason": (
                f"Best available slot {delay_description}, based on "
                "specialty match, availability, distance, queue, "
                "workload, hospital capacity, and existing appointments."
            ),
        }

    # --------------------------------------------------------
    # RESPONSE
    # --------------------------------------------------------

    return {
        "patient_id": db_patient.patient_id,
        "classification": ai_res,
        "location": {
            "latitude": lat,
            "longitude": lon,
        },
        "recommendations": recommendations,
        "appointment_recommendation": appointment_recommendation,
    }


# ============================================================
# GET DOCTORS
# ============================================================

@app.get(
    "/api/doctors",
    response_model=List[Dict[str, Any]]
)
def get_doctors(
    specialty: Optional[str] = None,
    available_only: bool = False,
    page: int = Query(1, ge=1, description="1-indexed page number"),
    page_size: int = Query(500, ge=1, le=500, description="Items per page"),
    db: Session = Depends(get_db)
):

    query = db.query(Doctor)

    if specialty:
        query = query.filter(
            Doctor.specialty.ilike(specialty)
        )

    if available_only:
        query = query.filter(Doctor.available_now.is_(True))

    docs = (
        query
        .order_by(Doctor.doctor_id)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    result = []

    for doctor in docs:

        result.append({
            "doctor_id": doctor.doctor_id,
            "name": doctor.name,
            "specialty": doctor.specialty,
            "qualification": doctor.qualification,
            "experience_years": doctor.experience_years,
            "available_now": doctor.available_now,
            "queue_count": doctor.queue_count,
            "workload": doctor.workload,
            "rating": doctor.rating,
            "hospital_name": (
                doctor.hospital.name
                if doctor.hospital
                else "Private Clinic"
            ),
        })

    return result


# ============================================================
# RECOMMENDED DOCTORS
# ============================================================

@app.get(
    "/api/doctors/recommend",
    response_model=schemas.DoctorRecommendResponse
)
def get_recommended_doctors(
    location: str = Query(
        ...,
        description="Patient location name"
    ),
    specialty: str = Query(
        ...,
        description="Target medical specialty"
    ),
    db: Session = Depends(get_db)
):

    lat, lon = resolve_location_to_coords(location)

    return find_and_rank_doctors(
        db,
        lat,
        lon,
        specialty
    )


# ============================================================
# NEARBY HOSPITALS
# ============================================================

@app.get("/api/hospitals/nearby")
def get_nearby_hospitals(
    location: str,
    db: Session = Depends(get_db)
):

    lat, lon = resolve_location_to_coords(location)

    hospitals = db.query(Hospital).all()

    from distance import haversine_distance

    result = []

    for hospital in hospitals:

        distance = haversine_distance(
            hospital.latitude,
            hospital.longitude,
            lat,
            lon
        )

        occupancy_rate = (
            (hospital.current_occupancy /
             hospital.total_capacity) * 100
            if hospital.total_capacity > 0
            else 0
        )

        icu_occupancy_rate = (
            (hospital.icu_occupancy /
             hospital.icu_capacity) * 100
            if hospital.icu_capacity > 0
            else 0
        )

        result.append({
            "hospital_id": hospital.hospital_id,
            "name": hospital.name,
            "address": hospital.address,
            "distance_km": round(distance, 2),
            "occupancy_rate": round(
                occupancy_rate,
                1
            ),
            "icu_occupancy_rate": round(
                icu_occupancy_rate,
                1
            ),
            "emergency_available": hospital.emergency_available,
        })

    result.sort(
        key=lambda x: x["distance_km"]
    )

    return result


# ============================================================
# CREATE APPOINTMENT
# ============================================================

@app.post("/api/appointments")
@limiter.limit(settings.RATE_LIMIT_BOOKING)
def create_appointment(
    request: Request,
    req: schemas.AppointmentCreate,
    db: Session = Depends(get_db)
):

    # --------------------------------------------------------
    # 1. LOCATION
    # --------------------------------------------------------

    lat, lon = resolve_location_to_coords(
        req.location
    )

    # --------------------------------------------------------
    # 2. FIND / CREATE PATIENT
    # --------------------------------------------------------

    patient = (
        db.query(Patient)
        .filter(
            Patient.name == req.name,
            Patient.symptoms == req.symptoms
        )
        .first()
    )

    if not patient:

        appointment_date = req.appointment_time.split()[0]
        appointment_time = req.appointment_time.split()[1]

        patient = Patient(
            name=req.name,
            age=req.age,
            symptoms=req.symptoms,
            location_name=req.location,
            latitude=lat,
            longitude=lon,
            preferred_date=appointment_date,
            preferred_time=appointment_time,
        )

        db.add(patient)
        db.commit()
        db.refresh(patient)

    # --------------------------------------------------------
    # 3. CHECK DOCTOR
    # --------------------------------------------------------

    # --------------------------------------------------------

    # Locks this doctor's row (Postgres/MySQL) so a second concurrent
    # booking request for the same doctor waits until this one commits
    # or rolls back, instead of both computing a slot against the same
    # stale view of the schedule.
    doctor = lock_doctor_for_booking(db, req.doctor_id)

    if not doctor:
        raise HTTPException(
            status_code=404,
            detail=f"Doctor '{req.doctor_id}' not found"
        )

    # --------------------------------------------------------
    # 4. OPTIMIZE SLOT
    # --------------------------------------------------------

    schedule_res = calculate_appointment_slot(
        db=db,
        doctor_id=req.doctor_id,
        priority=req.priority,
        preferred_time_str=req.appointment_time,
        duration_mins=doctor.consultation_duration,
    )

    # --------------------------------------------------------
    # 5. CREATE APPOINTMENT ID
    # --------------------------------------------------------

    app_id = generate_appointment_id()

    # --------------------------------------------------------
    # 6. SAVE APPOINTMENT
    # --------------------------------------------------------

    db_app = Appointment(
        appointment_id=app_id,
        patient_id=patient.patient_id,
        patient_name=patient.name,
        doctor_id=req.doctor_id,
        hospital_id=req.hospital_id,
        appointment_time=schedule_res["scheduled_time"],
        priority=req.priority,
        estimated_duration=doctor.consultation_duration,
        status=schedule_res["status"],
        notes=schedule_res["notes"],
    )

    db.add(db_app)
    try:
        db.commit()
    except IntegrityError as exc:
        # Backstop for the rare case the row lock didn't prevent a clash
        # (e.g. running on SQLite, or an ID collision - astronomically
        # unlikely with UUID-based IDs, but handled cleanly regardless).
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "That slot was just booked by another request. "
                "Please try again - the doctor's schedule has been refreshed."
            ),
        ) from exc
    db.refresh(db_app)

    return {
        "appointment": {
            "appointment_id": db_app.appointment_id,
            "patient_id": db_app.patient_id,
            "patient_name": db_app.patient_name,
            "doctor_id": db_app.doctor_id,
            "doctor_name": doctor.name,
            "hospital_id": db_app.hospital_id,
            "hospital_name": (
                doctor.hospital.name
                if doctor.hospital
                else "Private Clinic"
            ),
            "appointment_time": db_app.appointment_time,
            "priority": db_app.priority,
            "status": db_app.status,
            "notes": db_app.notes,
        },
        "shifted_appointments":
            schedule_res["shifted_appointments"],
    }


# ============================================================
# GET SINGLE APPOINTMENT
# ============================================================

@app.get("/api/appointments/{id}")
def get_appointment(
    id: str,
    db: Session = Depends(get_db),
    viewer: Optional[AdminUser] = Depends(get_optional_admin),
):

    appointment = (
        db.query(Appointment)
        .filter(
            Appointment.appointment_id == id
        )
        .first()
    )

    if not appointment:
        raise HTTPException(
            status_code=404,
            detail="Appointment not found"
        )

    # Public endpoint: only signed-in staff see who the patient is.
    return {
        "appointment_id": appointment.appointment_id,
        "patient_id": appointment.patient_id if viewer else None,
        "patient_name": (
            appointment.patient_name
            if viewer
            else mask_patient_name(appointment.patient_name)
        ),
        "doctor_id": appointment.doctor_id,
        "hospital_id": appointment.hospital_id,
        "appointment_time": appointment.appointment_time,
        "priority": appointment.priority,
        "estimated_duration": appointment.estimated_duration,
        "status": appointment.status,
        "notes": appointment.notes,
    }


# ============================================================
# GET ALL APPOINTMENTS
# ============================================================

@app.get("/api/appointments")
def get_all_appointments(
    status: Optional[str] = None,
    doctor_id: Optional[str] = None,
    hospital_id: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(1000, ge=1, le=1000),
    db: Session = Depends(get_db),
    viewer: Optional[AdminUser] = Depends(get_optional_admin),
):

    query = db.query(Appointment)

    if status:
        query = query.filter(Appointment.status == status.upper())
    if doctor_id:
        query = query.filter(Appointment.doctor_id == doctor_id)
    if hospital_id:
        query = query.filter(Appointment.hospital_id == hospital_id)

    appointments = (
        query
        .order_by(Appointment.appointment_time)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    result = []

    for appointment in appointments:

        doctor = (
            db.query(Doctor)
            .filter(
                Doctor.doctor_id ==
                appointment.doctor_id
            )
            .first()
        )

        hospital = (
            db.query(Hospital)
            .filter(
                Hospital.hospital_id ==
                appointment.hospital_id
            )
            .first()
        )

        result.append({
            "appointment_id":
                appointment.appointment_id,

            "patient_name":
                appointment.patient_name
                if viewer
                else mask_patient_name(appointment.patient_name),

            "doctor_name":
                doctor.name
                if doctor
                else appointment.doctor_id,

            "hospital_name":
                hospital.name
                if hospital
                else appointment.hospital_id,

            "appointment_time":
                appointment.appointment_time,

            "priority":
                appointment.priority,

            "status":
                appointment.status,

            "notes":
                appointment.notes,
        })

    return result


# ============================================================
# EMERGENCY SIMULATION
# ============================================================

@app.post("/api/events/emergency")
def simulate_emergency(
    request: Request,
    req: Dict[str, Any] = Body(default={}),
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(require_role("admin", "staff")),
):

    """
    Simulates emergency patient arrival.

    Steps:
    1. Classify symptoms
    2. Force EMERGENCY priority
    3. Find best available doctor
    4. Schedule immediately
    5. Shift affected appointments
    """

    name = req.get(
        "name",
        "Emergency Patient"
    )

    age = req.get(
        "age",
        45
    )

    symptoms = req.get(
        "symptoms",
        "Chest pain and breathing difficulty"
    )

    location = req.get(
        "location",
        "Civil Lines"
    )

    preferred_time = req.get(
        "preferred_time",
        datetime.now().strftime(
            "%Y-%m-%d %H:%M"
        )
    )

    # --------------------------------------------------------
    # 1. CLASSIFY
    # --------------------------------------------------------

    ai_res = classify_symptoms(symptoms)

    # Emergency overrides normal urgency.
    ai_res["urgency"] = "EMERGENCY"

    # --------------------------------------------------------
    # 2. LOCATION
    # --------------------------------------------------------

    lat, lon = resolve_location_to_coords(
        location
    )

    # --------------------------------------------------------
    # 3. FIND DOCTORS
    # --------------------------------------------------------

    recommendations = find_and_rank_doctors(
        db,
        lat,
        lon,
        ai_res["specialty"]
    )

    if not recommendations.get("doctors"):

        recommendations = find_and_rank_doctors(
            db,
            lat,
            lon,
            "General Medicine"
        )

    if not recommendations.get("doctors"):
        raise HTTPException(
            status_code=503,
            detail="No available doctors found for emergency."
        )

    best_doc = recommendations["doctors"][0]

    # --------------------------------------------------------
    # 4. SCHEDULE EMERGENCY
    # --------------------------------------------------------

    schedule_res = calculate_appointment_slot(
        db=db,
        doctor_id=best_doc["doctor_id"],
        priority="EMERGENCY",
        preferred_time_str=preferred_time,
        duration_mins=best_doc["consultation_duration"],
    )

    # --------------------------------------------------------
    # 5. CREATE PATIENT
    # --------------------------------------------------------

    p = Patient(
        name=name,
        age=age,
        symptoms=symptoms,
        location_name=location,
        latitude=lat,
        longitude=lon,
        preferred_date=preferred_time.split()[0],
        preferred_time=preferred_time.split()[1],
    )

    db.add(p)
    db.commit()
    db.refresh(p)

    # --------------------------------------------------------
    # 6. CREATE EMERGENCY APPOINTMENT
    # --------------------------------------------------------

    app_id = generate_appointment_id(prefix="E")

    emergency_appointment = Appointment(
        appointment_id=app_id,
        patient_id=p.patient_id,
        patient_name=p.name,
        doctor_id=best_doc["doctor_id"],
        hospital_id=best_doc["hospital"]["hospital_id"],
        appointment_time=schedule_res["scheduled_time"],
        priority="EMERGENCY",
        estimated_duration=best_doc["consultation_duration"],
        status="BOOKED",
        notes="🚨 EMERGENCY CASE SCHEDULED IMMEDIATELY.",
    )

    db.add(emergency_appointment)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="A conflicting appointment was just created for this doctor. Please retry.",
        ) from exc

    # --------------------------------------------------------
    # 7. KEEP DOCTOR ACTIVE
    # --------------------------------------------------------

    doctor = (
        db.query(Doctor)
        .filter(
            Doctor.doctor_id ==
            best_doc["doctor_id"]
        )
        .first()
    )

    if doctor:
        doctor.available_now = True
        doctor.queue_count = (
            (doctor.queue_count or 0) + 1
        )

    db.commit()

    # --------------------------------------------------------
    # RESPONSE
    # --------------------------------------------------------

    return {
        "message":
            "Emergency successfully scheduled.",

        "appointment_id":
            app_id,

        "patient": {
            "name":
                name,

            "specialty":
                ai_res["specialty"],

            "urgency":
                "EMERGENCY",
        },

        "assigned_doctor":
            best_doc["name"],

        "assigned_hospital":
            best_doc["hospital"]["name"],

        "scheduled_time":
            schedule_res["scheduled_time"],

        "shifted_appointments":
            schedule_res["shifted_appointments"],
    }


# ============================================================
# DOCTOR UNAVAILABLE
# ============================================================

@app.post("/api/events/doctor-unavailable")
def simulate_doctor_unavailable(
    req: Dict[str, str] = Body(default={}),
    doctor_id: Optional[str] = Query(
        None,
        description="Doctor ID"
    ),
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(require_role("admin", "staff")),
):

    # Accept doctor_id either from:
    # JSON body OR query parameter.

    requested_doctor_id = (
        req.get("doctor_id")
        if req
        else None
    )

    requested_doctor_id = (
        requested_doctor_id
        or doctor_id
    )

    if not requested_doctor_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "doctor_id required. "
                "Example: "
                '{"doctor_id":"D001"}'
            )
        )

    # --------------------------------------------------------
    # FIND DOCTOR
    # --------------------------------------------------------

    doctor = (
        db.query(Doctor)
        .filter(
            Doctor.doctor_id ==
            requested_doctor_id
        )
        .first()
    )

    if not doctor:
        raise HTTPException(
            status_code=404,
            detail="Doctor not found"
        )

    # --------------------------------------------------------
    # MARK UNAVAILABLE
    # --------------------------------------------------------

    doctor.available_now = False
    db.commit()

    # --------------------------------------------------------
    # FIND AFFECTED APPOINTMENTS
    # --------------------------------------------------------

    affected_apps = (
        db.query(Appointment)
        .filter(
            Appointment.doctor_id ==
            requested_doctor_id,
            Appointment.status != "CANCELLED"
        )
        .all()
    )

    reallocations = []

    # --------------------------------------------------------
    # REALLOCATE EACH APPOINTMENT
    # --------------------------------------------------------

    for appointment in affected_apps:

        old_time = appointment.appointment_time

        # Default Kanpur coordinates.
        p_lat = 26.456
        p_lon = 80.331

        if appointment.patient:

            if appointment.patient.latitude:
                p_lat = appointment.patient.latitude

            if appointment.patient.longitude:
                p_lon = appointment.patient.longitude

        alternative_recommendations = (
            find_and_rank_doctors(
                db,
                p_lat,
                p_lon,
                doctor.specialty
            )
        )

        valid_doctors = [
            d
            for d in alternative_recommendations.get(
                "doctors",
                []
            )
            if (
                d["doctor_id"] !=
                requested_doctor_id
                and d["available_now"]
            )
        ]

        # ----------------------------------------------------
        # ALTERNATIVE FOUND
        # ----------------------------------------------------

        if valid_doctors:

            best_alt = valid_doctors[0]

            schedule_result = calculate_appointment_slot(
                db=db,
                doctor_id=best_alt["doctor_id"],
                priority=appointment.priority,
                preferred_time_str=appointment.appointment_time,
                duration_mins=best_alt[
                    "consultation_duration"
                ],
            )

            appointment.doctor_id = (
                best_alt["doctor_id"]
            )

            appointment.hospital_id = (
                best_alt["hospital"]["hospital_id"]
            )

            appointment.appointment_time = (
                schedule_result["scheduled_time"]
            )

            appointment.status = "REALLOCATED"

            appointment.notes = (
                f"Reallocated from {doctor.name} "
                f"(unavailable) to "
                f"{best_alt['name']} at "
                f"{best_alt['hospital']['name']}."
            )

            reallocations.append({
                "appointment_id":
                    appointment.appointment_id,

                "patient_name":
                    appointment.patient_name,

                "original_doctor":
                    doctor.name,

                "reallocated_doctor":
                    best_alt["name"],

                "reallocated_hospital":
                    best_alt["hospital"]["name"],

                "old_time":
                    old_time,

                "new_time":
                    schedule_result[
                        "scheduled_time"
                    ],

                "notes":
                    appointment.notes,
            })

        # ----------------------------------------------------
        # NO ALTERNATIVE
        # ----------------------------------------------------

        else:

            appointment.status = "REALLOCATED"

            appointment.notes = (
                f"Unassigned: No alternative "
                f"specialist in {doctor.specialty} "
                f"available. Specialist escalation "
                f"triggered."
            )

            reallocations.append({
                "appointment_id":
                    appointment.appointment_id,

                "patient_name":
                    appointment.patient_name,

                "original_doctor":
                    doctor.name,

                "reallocated_doctor":
                    "Specialist Escalation Needed",

                "reallocated_hospital":
                    "N/A",

                "old_time":
                    old_time,

                "new_time":
                    "N/A",

                "notes":
                    appointment.notes,
            })

    # --------------------------------------------------------
    # RESET ORIGINAL DOCTOR
    # --------------------------------------------------------

    doctor.queue_count = 0
    doctor.workload = 0.0

    db.commit()

    return {
        "message":
            f"Doctor {doctor.name} marked unavailable.",

        "affected_appointments_count":
            len(affected_apps),

        "reallocations":
            reallocations,
    }


# ============================================================
# CANCEL APPOINTMENT
# ============================================================

@app.post("/api/events/cancel")
def simulate_cancel_appointment(
    req: Dict[str, str] = Body(default={}),
    appointment_id: Optional[str] = Query(
        None,
        description="Appointment ID"
    ),
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(require_role("admin", "staff")),
):

    requested_appointment_id = (
        req.get("appointment_id")
        if req
        else None
    )

    requested_appointment_id = (
        requested_appointment_id
        or appointment_id
    )

    if not requested_appointment_id:

        raise HTTPException(
            status_code=400,
            detail=(
                "appointment_id required. "
                "Example: "
                '{"appointment_id":"A001"}'
            )
        )

    # Verify appointment exists before calling scheduler.

    appointment = (
        db.query(Appointment)
        .filter(
            Appointment.appointment_id ==
            requested_appointment_id
        )
        .first()
    )

    if not appointment:

        raise HTTPException(
            status_code=404,
            detail="Appointment not found"
        )

    if appointment.status == "CANCELLED":

        return {
            "message":
                f"Appointment "
                f"{requested_appointment_id} "
                f"is already cancelled.",

            "shifted_appointments": []
        }

    result = cancel_appointment_and_pull_forward(
        db,
        requested_appointment_id
    )

    return result


# ============================================================
# QUEUE REBALANCING
# ============================================================

@app.post("/api/optimization/reallocate")
def trigger_queue_rebalancing(
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(require_role("admin")),
):

    overloaded_doctors = (
        db.query(Doctor)
        .filter(
            Doctor.queue_count > 5,
            Doctor.available_now.is_(True)
        )
        .all()
    )

    reallocations = []

    for doctor in overloaded_doctors:

        low_priority_appointments = (
            db.query(Appointment)
            .filter(
                Appointment.doctor_id ==
                doctor.doctor_id,

                Appointment.priority ==
                "LOW",

                Appointment.status !=
                "CANCELLED"
            )
            .all()
        )

        for appointment in low_priority_appointments:

            p_lat = 26.456
            p_lon = 80.331

            if appointment.patient:

                if appointment.patient.latitude:
                    p_lat = (
                        appointment.patient.latitude
                    )

                if appointment.patient.longitude:
                    p_lon = (
                        appointment.patient.longitude
                    )

            recommendations = (
                find_and_rank_doctors(
                    db,
                    p_lat,
                    p_lon,
                    doctor.specialty
                )
            )

            better_alternatives = [
                d
                for d in recommendations.get(
                    "doctors",
                    []
                )
                if (
                    d["doctor_id"] !=
                    doctor.doctor_id
                    and d["available_now"]
                    and d["queue_count"] <
                    doctor.queue_count - 1
                )
            ]

            if not better_alternatives:
                continue

            best_alt = better_alternatives[0]

            old_time = appointment.appointment_time

            schedule_result = calculate_appointment_slot(
                db=db,
                doctor_id=best_alt["doctor_id"],
                priority="LOW",
                preferred_time_str=old_time,
                duration_mins=best_alt[
                    "consultation_duration"
                ],
            )

            appointment.doctor_id = (
                best_alt["doctor_id"]
            )

            appointment.hospital_id = (
                best_alt["hospital"]["hospital_id"]
            )

            appointment.appointment_time = (
                schedule_result["scheduled_time"]
            )

            appointment.status = "REALLOCATED"

            appointment.notes = (
                f"Rebalanced from overloaded "
                f"queue of {doctor.name} to "
                f"{best_alt['name']}."
            )

            reallocations.append({
                "appointment_id":
                    appointment.appointment_id,

                "patient_name":
                    appointment.patient_name,

                "original_doctor":
                    doctor.name,

                "reallocated_doctor":
                    best_alt["name"],

                "reallocated_hospital":
                    best_alt["hospital"]["name"],

                "old_time":
                    old_time,

                "new_time":
                    schedule_result[
                        "scheduled_time"
                    ],

                "reason":
                    appointment.notes,
            })

            doctor.queue_count = max(
                0,
                doctor.queue_count - 1
            )

            db.commit()

    return {
        "message":
            "Queue rebalancing completed. "
            f"Reallocated "
            f"{len(reallocations)} appointments.",

        "reallocations":
            reallocations,
    }


# ============================================================
# HOSPITAL DASHBOARD
# ============================================================

@app.get("/api/hospitals/{id}/dashboard")
def get_hospital_dashboard_stats(
    id: str,
    db: Session = Depends(get_db)
):

    hospital = (
        db.query(Hospital)
        .filter(
            Hospital.hospital_id == id
        )
        .first()
    )

    if not hospital:

        raise HTTPException(
            status_code=404,
            detail="Hospital not found"
        )

    doctors = (
        db.query(Doctor)
        .filter(
            Doctor.hospital_id == id
        )
        .all()
    )

    available_doctors = sum(
        1
        for doctor in doctors
        if doctor.available_now
    )

    total_doctors = len(doctors)

    # --------------------------------------------------------
    # ACTIVE PATIENTS
    # --------------------------------------------------------

    waiting_count = (
        db.query(Appointment)
        .filter(
            Appointment.hospital_id == id,
            Appointment.status != "CANCELLED"
        )
        .count()
    )

    # --------------------------------------------------------
    # WAIT TIME
    # --------------------------------------------------------

    if waiting_count > 0:
        average_wait = min(
            90,
            10 + (waiting_count * 2)
        )
    else:
        average_wait = 21

    # --------------------------------------------------------
    # CAPACITY
    # --------------------------------------------------------

    capacity_percentage = (
        hospital.current_occupancy /
        hospital.total_capacity * 100
        if hospital.total_capacity > 0
        else 0
    )

    emergency_capacity_percentage = (
        hospital.icu_occupancy /
        hospital.icu_capacity * 100
        if hospital.icu_capacity > 0
        else 0
    )

    return {
        "hospital_id":
            hospital.hospital_id,

        "name":
            hospital.name,

        "doctors_available":
            f"{available_doctors} / {total_doctors}",

        "doctors_available_count":
            available_doctors,

        "total_doctors_count":
            total_doctors,

        "patients_waiting":
            waiting_count,

        "average_waiting_time_minutes":
            average_wait,

        "capacity_percentage":
            round(
                capacity_percentage,
                1
            ),

        "emergency_capacity_percentage":
            round(
                emergency_capacity_percentage,
                1
            ),
    }


# ============================================================
# ANALYTICS
# ============================================================

@app.get("/api/analytics")
def get_analytics_data(
    db: Session = Depends(get_db)
):

    # --------------------------------------------------------
    # SUMMARY
    # --------------------------------------------------------

    total_hospitals = (
        db.query(Hospital).count()
    )

    total_doctors = (
        db.query(Doctor).count()
    )

    active_appointments = (
        db.query(Appointment)
        .filter(
            Appointment.status !=
            "CANCELLED"
        )
        .count()
    )

    # --------------------------------------------------------
    # WAITING TIME
    # --------------------------------------------------------

    doctor_queues = (
        db.query(Doctor.queue_count)
        .all()
    )

    queue_sum = sum(
        q[0]
        for q in doctor_queues
        if q[0] is not None
    )

    if total_doctors > 0:

        average_waiting_time = max(
            10,
            min(
                120,
                int(
                    queue_sum *
                    15 /
                    total_doctors
                )
            )
        )

    else:
        average_waiting_time = 21

    # --------------------------------------------------------
    # HOSPITAL UTILIZATION
    # --------------------------------------------------------

    hospitals = (
        db.query(Hospital).all()
    )

    utilization_values = [
        (
            h.current_occupancy /
            h.total_capacity
        )
        for h in hospitals
        if h.total_capacity > 0
    ]

    if utilization_values:

        average_utilization = round(
            (
                sum(utilization_values) /
                len(utilization_values)
            ) * 100,
            1
        )

    else:
        average_utilization = 0.0

    # --------------------------------------------------------
    # EMERGENCY CASES
    # --------------------------------------------------------

    emergency_count = (
        db.query(Appointment)
        .filter(
            Appointment.priority ==
            "EMERGENCY"
        )
        .count()
    )

    # --------------------------------------------------------
    # HOSPITAL CAPACITY CHART
    # --------------------------------------------------------

    hospital_capacity_chart = []

    for hospital in hospitals[:8]:

        occupancy_rate = (
            hospital.current_occupancy /
            hospital.total_capacity * 100
            if hospital.total_capacity > 0
            else 0
        )

        hospital_capacity_chart.append({
            "name":
                hospital.name
                .split("(")[0]
                .strip(),

            "occupancy":
                hospital.current_occupancy,

            "capacity":
                hospital.total_capacity,

            "occupancy_rate":
                round(
                    occupancy_rate,
                    1
                ),
        })

    # --------------------------------------------------------
    # DOCTOR WORKLOAD
    # --------------------------------------------------------

    doctors = (
        db.query(Doctor)
        .order_by(
            Doctor.workload.desc()
        )
        .limit(8)
        .all()
    )

    doctor_workload_chart = []

    for doctor in doctors:

        doctor_workload_chart.append({
            "name":
                doctor.name,

            "specialty":
                doctor.specialty,

            "workload":
                doctor.workload,
        })

    # --------------------------------------------------------
    # SPECIALTY DEMAND
    # --------------------------------------------------------

    specialty_counts = {}

    appointments = (
        db.query(Appointment).all()
    )

    for appointment in appointments:

        doctor = (
            db.query(Doctor)
            .filter(
                Doctor.doctor_id ==
                appointment.doctor_id
            )
            .first()
        )

        if doctor:

            specialty_counts[
                doctor.specialty
            ] = (
                specialty_counts.get(
                    doctor.specialty,
                    0
                ) + 1
            )

    specialty_demand_chart = [
        {
            "name": specialty,
            "value": count
        }
        for specialty, count
        in specialty_counts.items()
    ]

    # --------------------------------------------------------
    # FALLBACK CHART DATA
    # --------------------------------------------------------

    if not specialty_demand_chart:

        specialty_demand_chart = [
            {
                "name": "Cardiology",
                "value": 5
            },
            {
                "name": "Neurology",
                "value": 3
            },
            {
                "name": "Pulmonology",
                "value": 4
            },
            {
                "name": "General Medicine",
                "value": 8
            },
        ]

    # --------------------------------------------------------
    # PRIORITY DISTRIBUTION
    # --------------------------------------------------------

    priorities = [
        "LOW",
        "MEDIUM",
        "HIGH",
        "EMERGENCY"
    ]

    priority_distribution = []

    for priority in priorities:

        count = (
            db.query(Appointment)
            .filter(
                Appointment.priority ==
                priority
            )
            .count()
        )

        priority_distribution.append({
            "name":
                priority,

            "value":
                count,
        })

    # --------------------------------------------------------
    # WAITING TIME BY SPECIALTY
    # --------------------------------------------------------

    specialties = list(
        set(
            doctor.specialty
            for doctor
            in db.query(Doctor).all()
        )
    )

    waiting_time_by_specialty = []

    for specialty in specialties[:6]:

        specialty_doctors = (
            db.query(Doctor)
            .filter(
                Doctor.specialty ==
                specialty
            )
            .all()
        )

        if specialty_doctors:

            average_queue = (
                sum(
                    doctor.queue_count
                    for doctor
                    in specialty_doctors
                )
                /
                len(specialty_doctors)
            )

            average_wait = round(
                average_queue * 20,
                0
            )

            waiting_time_by_specialty.append({
                "specialty":
                    specialty,

                "waiting_time":
                    max(
                        5,
                        average_wait
                    ),
            })

    # --------------------------------------------------------
    # RESPONSE
    # --------------------------------------------------------

    return {
        "summary": {

            "total_hospitals":
                total_hospitals,

            "total_doctors":
                total_doctors,

            "active_appointments":
                active_appointments,

            "average_waiting_time":
                f"{average_waiting_time} min",

            "hospital_utilization":
                f"{average_utilization}%",

            "emergency_cases":
                emergency_count,
        },

        "charts": {

            "hospital_capacity":
                hospital_capacity_chart,

            "doctor_workload":
                doctor_workload_chart,

            "specialty_demand":
                specialty_demand_chart,

            "priority_distribution":
                priority_distribution,

            "waiting_time_by_specialty":
                waiting_time_by_specialty,
        },
    }


# ============================================================
# ADMIN — PROTECTED OPERATIONS
# ============================================================
# Everything below requires a valid admin Bearer token (see
# POST /api/auth/login). These are the operations a hospital's back
# office should be able to do that a public patient-facing client
# should not be able to trigger anonymously.

@app.get("/api/admin/appointments/export")
def export_appointments_csv(
    status: Optional[str] = None,
    admin: AdminUser = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Export appointments as a CSV file (e.g. for hospital record-keeping
    or import into billing/EHR systems)."""

    query = db.query(Appointment)
    if status:
        query = query.filter(Appointment.status == status.upper())

    appointments = query.order_by(Appointment.appointment_time).all()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "appointment_id", "patient_name", "doctor_id", "hospital_id",
            "appointment_time", "priority", "status", "estimated_duration",
            "notes",
        ]
    )
    for a in appointments:
        writer.writerow(
            [
                a.appointment_id, a.patient_name, a.doctor_id, a.hospital_id,
                a.appointment_time, a.priority, a.status, a.estimated_duration,
                a.notes or "",
            ]
        )

    buffer.seek(0)
    filename = f"appointments_export_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.patch("/api/admin/doctors/{doctor_id}/availability")
def set_doctor_availability(
    doctor_id: str,
    available_now: bool = Body(..., embed=True),
    admin: AdminUser = Depends(require_role("admin", "staff")),
    db: Session = Depends(get_db),
):
    """Manually override a doctor's availability flag (e.g. the doctor
    called in sick, or has returned from leave)."""

    doctor = db.query(Doctor).filter(Doctor.doctor_id == doctor_id).first()
    if not doctor:
        raise HTTPException(status_code=404, detail=f"Doctor '{doctor_id}' not found")

    doctor.available_now = available_now
    db.commit()
    db.refresh(doctor)

    logger.info(
        "Admin %s set doctor %s availability -> %s",
        admin.email, doctor_id, available_now,
    )

    return {
        "doctor_id": doctor.doctor_id,
        "name": doctor.name,
        "available_now": doctor.available_now,
    }


@app.get("/api/admin/dashboard-summary")
def admin_dashboard_summary(
    admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Lightweight, auth-gated summary intended for a staff-only view -
    counts only, no PII, cheap to poll frequently."""

    return {
        "total_patients": db.query(Patient).count(),
        "total_appointments": db.query(Appointment).count(),
        "booked": db.query(Appointment).filter(Appointment.status == "BOOKED").count(),
        "cancelled": db.query(Appointment).filter(Appointment.status == "CANCELLED").count(),
        "emergency_priority": db.query(Appointment).filter(Appointment.priority == "EMERGENCY").count(),
        "doctors_available": db.query(Doctor).filter(Doctor.available_now.is_(True)).count(),
        "doctors_total": db.query(Doctor).count(),
    }
