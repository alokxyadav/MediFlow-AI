"""
Regression tests for the booking-concurrency fixes: row locking, the
partial unique DB index, collision-resistant appointment IDs, and the
rewritten (interval-tracking) queue-shifting algorithm.

Run with: pytest test_booking_concurrency.py -v
"""
import os
import threading

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key")
os.environ.setdefault("ADMIN_PASSWORD", "TestPass123!")
os.environ.setdefault("ADMIN_EMAIL", "admin@mediflow.ai")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from database import SessionLocal
from main import app
from models import Appointment, Doctor

client = TestClient(app)
with TestClient(app) as _probe:
    _probe.get("/api/health")


def _cardiology_doctor_id() -> str:
    db = SessionLocal()
    try:
        doc = db.query(Doctor).filter(Doctor.specialty == "Cardiology").first()
        assert doc is not None, "seed data must include a Cardiology doctor"
        return doc.doctor_id
    finally:
        db.close()


def _clear_doctor_schedule(doctor_id: str, date_prefix: str):
    db = SessionLocal()
    try:
        db.query(Appointment).filter(
            Appointment.doctor_id == doctor_id,
            Appointment.appointment_time.like(f"{date_prefix}%"),
        ).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _active_times_for(doctor_id: str, date_prefix: str):
    db = SessionLocal()
    try:
        rows = (
            db.query(Appointment.appointment_time)
            .filter(
                Appointment.doctor_id == doctor_id,
                Appointment.appointment_time.like(f"{date_prefix}%"),
                Appointment.status != "CANCELLED",
            )
            .all()
        )
        return [r[0] for r in rows]
    finally:
        db.close()


def test_partial_unique_index_rejects_direct_duplicate():
    """Sanity-check the DB constraint itself works, independent of the
    application code that's supposed to prevent hitting it."""
    doctor_id = _cardiology_doctor_id()
    date_prefix = "2031-01-01"
    _clear_doctor_schedule(doctor_id, date_prefix)

    db = SessionLocal()
    try:
        hospital_id = db.query(Doctor).filter(Doctor.doctor_id == doctor_id).first().hospital_id

        db.add(Appointment(
            appointment_id="ZTEST0001",
            patient_name="Direct Insert 1",
            doctor_id=doctor_id,
            hospital_id=hospital_id,
            appointment_time=f"{date_prefix} 09:00",
            priority="LOW",
            estimated_duration=20,
            status="BOOKED",
        ))
        db.commit()

        db.add(Appointment(
            appointment_id="ZTEST0002",
            patient_name="Direct Insert 2",
            doctor_id=doctor_id,
            hospital_id=hospital_id,
            appointment_time=f"{date_prefix} 09:00",  # same doctor, same time, same day
            priority="LOW",
            estimated_duration=20,
            status="BOOKED",
        ))
        raised = False
        try:
            db.commit()
        except IntegrityError:
            raised = True
            db.rollback()
        assert raised, "duplicate active (doctor_id, appointment_time) should violate the unique index"
    finally:
        _clear_doctor_schedule(doctor_id, date_prefix)
        db.close()


def test_concurrent_bookings_same_doctor_same_slot_never_collide():
    """The actual regression test: fire N booking requests at the same
    doctor and the same preferred slot, truly concurrently (real
    threads), and assert every successfully-booked appointment for that
    doctor on that day ends up at a distinct time - the exact bug this
    round's fixes were written to close."""
    doctor_id = _cardiology_doctor_id()
    date_prefix = "2031-02-02"
    _clear_doctor_schedule(doctor_id, date_prefix)

    hospital_id = SessionLocal().query(Doctor).filter(Doctor.doctor_id == doctor_id).first().hospital_id

    def book(i: int):
        return client.post(
            "/api/appointments",
            json={
                "name": f"Concurrent Patient {i}",
                "age": 30 + i,
                "symptoms": "chest pain",
                "location": "Kalyanpur",
                "doctor_id": doctor_id,
                "hospital_id": hospital_id,
                "appointment_time": f"{date_prefix} 09:00",
                "priority": "LOW",
            },
        )

    n_requests = 8
    results = [None] * n_requests
    threads = []

    def worker(i):
        results[i] = book(i)

    for i in range(n_requests):
        t = threading.Thread(target=worker, args=(i,))
        threads.append(t)
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    try:
        # Every request should get a clean response - either booked (200)
        # or a clean "just got taken, retry" (409). Never a raw 500/503
        # from an unhandled integrity violation reaching the client.
        statuses = [r.status_code for r in results]
        assert all(s in (200, 409) for s in statuses), f"unexpected status codes: {statuses}"

        booked_times = _active_times_for(doctor_id, date_prefix)
        assert len(booked_times) == len(set(booked_times)), (
            f"double-booking detected - duplicate times among: {booked_times}"
        )
        # At least the first request should have succeeded outright.
        assert statuses.count(200) >= 1
    finally:
        _clear_doctor_schedule(doctor_id, date_prefix)
