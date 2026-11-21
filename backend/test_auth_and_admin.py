"""
Tests for the auth layer and admin-protected endpoints added in the
production-hardening pass. Run with: pytest -v

Uses whichever DATABASE_URL is already configured for the process (same
as test_mediflow.py) so both test files share one consistently-seeded
database within a CI run. For a from-scratch local run:

    rm -f mediflow.db && python seed.py && pytest -v
"""
import os
from datetime import datetime, timedelta, timezone

# These setdefault calls only take effect if the variable isn't already
# set in the environment - which means a developer's shell having its
# own ADMIN_PASSWORD (e.g. left over from a Docker/prod session) would
# silently override these, while a hardcoded credential literal used in
# the test bodies below would go stale and start failing for reasons
# that have nothing to do with the app being broken. Reading back
# whatever's actually in effect below, instead of repeating separate
# literal strings, keeps the suite correct regardless of the ambient
# environment it happens to run in - verified by running it three ways:
# with no ADMIN_PASSWORD set, with the CI value, and with a deliberately
# different one.
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key")
os.environ.setdefault("ADMIN_PASSWORD", "TestPass123!")
os.environ.setdefault("ADMIN_EMAIL", "admin@mediflow.ai")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

from fastapi.testclient import TestClient  # noqa: E402 - must follow the env setup above

from main import app  # noqa: E402

ADMIN_EMAIL = os.environ["ADMIN_EMAIL"]
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]

# A date safely in the future, so tests that target other validation rules
# (age bounds, blank fields) are not accidentally satisfied by the
# past-date rule instead.
FUTURE_DATE = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
PAST_DATE = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%d")

client = TestClient(app)
# Ensure the startup event (which seeds the default admin) has actually
# run before any test executes - some TestClient/Starlette versions only
# trigger lifespan handlers when used as a context manager.
with TestClient(app) as _startup_probe:
    _startup_probe.get("/api/health")


def test_health_check():
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json()["status"] in ("healthy", "degraded")


def test_login_requires_valid_credentials():
    res = client.post(
        "/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": "wrong-password"},
    )
    assert res.status_code == 401


def test_login_succeeds_with_seeded_admin():
    res = client.post(
        "/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    assert res.status_code == 200
    body = res.json()
    assert "access_token" in body
    assert body["token_type"] == "bearer"


def _get_token() -> str:
    res = client.post(
        "/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    return res.json()["access_token"]


def test_admin_routes_reject_missing_token():
    res = client.get("/api/admin/dashboard-summary")
    assert res.status_code == 401


def test_admin_routes_reject_garbage_token():
    res = client.get(
        "/api/admin/dashboard-summary",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert res.status_code == 401


def test_admin_routes_accept_valid_token():
    token = _get_token()
    res = client.get(
        "/api/admin/dashboard-summary",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    body = res.json()
    assert "total_appointments" in body


def test_logout_revokes_the_token_immediately():
    """The actual regression this closes: logging out must invalidate
    the token server-side, not just tell the frontend to forget it -
    otherwise a copied/intercepted token stays usable until it expires
    naturally, regardless of the user believing they've signed out."""
    token = _get_token()

    # Token works before logout.
    res = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200

    logout_res = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert logout_res.status_code == 200

    # The exact same token must now be rejected everywhere, not just on
    # the endpoint that happened to revoke it.
    res = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 401

    res = client.get(
        "/api/admin/dashboard-summary",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 401

    # A fresh login must still work fine - revocation is per-token, not
    # a lockout of the account.
    fresh_token = _get_token()
    res = client.get("/api/auth/me", headers={"Authorization": f"Bearer {fresh_token}"})
    assert res.status_code == 200


def test_patient_request_validates_age():
    res = client.post(
        "/api/patient/request",
        json={
            "name": "Edge Case",
            "age": 999,
            "symptoms": "fever",
            "location": "Kalyanpur",
            "preferred_date": FUTURE_DATE,
            "preferred_time": "10:00",
        },
    )
    assert res.status_code == 422


def test_patient_request_rejects_blank_symptoms():
    res = client.post(
        "/api/patient/request",
        json={
            "name": "Edge Case",
            "age": 30,
            "symptoms": "  ",
            "location": "Kalyanpur",
            "preferred_date": FUTURE_DATE,
            "preferred_time": "10:00",
        },
    )
    assert res.status_code == 422


def test_classify_requires_symptoms():
    res = client.post("/api/ai/classify", json={})
    assert res.status_code == 400


def _patient_payload(**overrides):
    payload = {
        "name": "Validation Case",
        "age": 30,
        "symptoms": "fever and body ache",
        "location": "Kalyanpur",
        "preferred_date": FUTURE_DATE,
        "preferred_time": "10:00",
    }
    payload.update(overrides)
    return payload


def test_patient_request_rejects_past_date():
    res = client.post("/api/patient/request", json=_patient_payload(preferred_date=PAST_DATE))
    assert res.status_code == 422
    assert "past" in res.text


def test_patient_request_accepts_future_date():
    res = client.post("/api/patient/request", json=_patient_payload())
    assert res.status_code == 200


def test_patient_request_accepts_todays_date_in_any_timezone():
    # Yesterday (UTC) is the grace boundary: a user west of UTC late in
    # their day can legitimately be one calendar day behind the server.
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    res = client.post("/api/patient/request", json=_patient_payload(preferred_date=yesterday))
    assert res.status_code == 200


def _appointment_payload(**overrides):
    payload = {
        "name": "Validation Case",
        "age": 30,
        "symptoms": "fever",
        "location": "Kalyanpur",
        "doctor_id": "D001",
        "hospital_id": "H001",
        "appointment_time": f"{FUTURE_DATE} 10:00",
        "priority": "LOW",
    }
    payload.update(overrides)
    return payload


def test_appointment_rejects_past_date():
    res = client.post("/api/appointments", json=_appointment_payload(appointment_time=f"{PAST_DATE} 10:00"))
    assert res.status_code == 422
    assert "past" in res.text


def test_appointment_rejects_malformed_time():
    for bad in ["tomorrow", "2031-01-01", "2031-01-01T10:00", "2031-13-40 10:00", "2031-01-01 25:99"]:
        res = client.post("/api/appointments", json=_appointment_payload(appointment_time=bad))
        assert res.status_code == 422, f"{bad!r} should be rejected"


# --- Patient privacy: public reads must not reveal who a patient is -------

def _book_named_patient(name: str) -> str:
    """Create a booking through the public API against a real seeded doctor
    and return its appointment_id. Does not depend on the matching engine's
    recommendation, which can legitimately be empty for some inputs."""
    from database import SessionLocal
    from models import Doctor

    db = SessionLocal()
    try:
        doctor = db.query(Doctor).order_by(Doctor.doctor_id).first()
        doctor_id, hospital_id = doctor.doctor_id, doctor.hospital_id
    finally:
        db.close()

    booking = client.post(
        "/api/appointments",
        json=_appointment_payload(
            name=name,
            doctor_id=doctor_id,
            hospital_id=hospital_id,
            appointment_time=f"{FUTURE_DATE} 11:00",
        ),
    )
    assert booking.status_code == 200, booking.text
    return booking.json()["appointment"]["appointment_id"]


def test_anonymous_appointment_reads_mask_patient_names():
    appointment_id = _book_named_patient("Priya Singh")

    listing = client.get("/api/appointments?page_size=1000").json()
    mine = next(a for a in listing if a["appointment_id"] == appointment_id)
    assert mine["patient_name"] == "Priya S."
    assert "Singh" not in str(listing)

    single = client.get(f"/api/appointments/{appointment_id}").json()
    assert single["patient_name"] == "Priya S."
    assert single["patient_id"] is None


def test_staff_see_full_patient_names():
    appointment_id = _book_named_patient("Priya Singh")
    headers = {"Authorization": f"Bearer {_get_token()}"}

    listing = client.get("/api/appointments?page_size=1000", headers=headers).json()
    mine = next(a for a in listing if a["appointment_id"] == appointment_id)
    assert mine["patient_name"] == "Priya Singh"

    single = client.get(f"/api/appointments/{appointment_id}", headers=headers).json()
    assert single["patient_name"] == "Priya Singh"


def test_invalid_or_revoked_token_is_treated_as_anonymous():
    appointment_id = _book_named_patient("Rohan Verma")

    garbage = client.get(
        f"/api/appointments/{appointment_id}", headers={"Authorization": "Bearer not-a-token"}
    )
    assert garbage.status_code == 200  # stays public, never a 401
    assert garbage.json()["patient_name"] == "Rohan V."

    token = _get_token()
    client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
    revoked = client.get(
        f"/api/appointments/{appointment_id}", headers={"Authorization": f"Bearer {token}"}
    )
    assert revoked.json()["patient_name"] == "Rohan V."
