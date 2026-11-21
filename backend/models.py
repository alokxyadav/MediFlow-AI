from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from database import Base


class Hospital(Base):
    __tablename__ = "hospitals"

    hospital_id = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False)
    hospital_type = Column(String)
    address = Column(String)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    total_capacity = Column(Integer, default=100)
    current_occupancy = Column(Integer, default=0)
    icu_capacity = Column(Integer, default=0)
    icu_occupancy = Column(Integer, default=0)
    emergency_available = Column(Boolean, default=True)
    teleconsultation_available = Column(Boolean, default=False)

    doctors = relationship("Doctor", back_populates="hospital")
    appointments = relationship("Appointment", back_populates="hospital")

class Doctor(Base):
    __tablename__ = "doctors"

    doctor_id = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False)
    qualification = Column(String)
    specialty = Column(String, nullable=False)
    clinic_or_affiliation = Column(String)
    hospital_id = Column(String, ForeignKey("hospitals.hospital_id"))
    experience_years = Column(Integer, default=0)
    consultation_duration = Column(Integer, default=15)  # minutes
    available_now = Column(Boolean, default=True)
    queue_count = Column(Integer, default=0)
    max_daily_patients = Column(Integer, default=20)
    workload = Column(Float, default=0.0)  # percentage
    emergency_available = Column(Boolean, default=True)
    teleconsultation_available = Column(Boolean, default=False)
    rating = Column(Float, default=4.0)

    hospital = relationship("Hospital", back_populates="doctors")
    appointments = relationship("Appointment", back_populates="doctor")

class Patient(Base):
    __tablename__ = "patients"

    patient_id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    name = Column(String, nullable=False)
    age = Column(Integer, nullable=False)
    symptoms = Column(String, nullable=False)
    location_name = Column(String)
    latitude = Column(Float)
    longitude = Column(Float)
    preferred_date = Column(String)
    preferred_time = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    appointments = relationship("Appointment", back_populates="patient")

class Appointment(Base):
    __tablename__ = "appointments"

    appointment_id = Column(String, primary_key=True, index=True)
    patient_id = Column(Integer, ForeignKey("patients.patient_id"), nullable=True)
    patient_name = Column(String, nullable=False)
    doctor_id = Column(String, ForeignKey("doctors.doctor_id"), nullable=False, index=True)
    hospital_id = Column(String, ForeignKey("hospitals.hospital_id"), nullable=False, index=True)
    appointment_time = Column(String, nullable=False, index=True)  # stored as string "YYYY-MM-DD HH:MM"
    priority = Column(String, default="LOW")  # LOW, MEDIUM, HIGH, EMERGENCY
    estimated_duration = Column(Integer, default=15)  # minutes
    status = Column(String, default="BOOKED", index=True)  # BOOKED, CANCELLED, SHIFTED, REALLOCATED
    notes = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    patient = relationship("Patient", back_populates="appointments")
    doctor = relationship("Doctor", back_populates="appointments")
    hospital = relationship("Hospital", back_populates="appointments")

    __table_args__ = (
        # Defense in depth against double-booking: a partial unique index
        # so at most one *active* (non-cancelled) appointment can exist
        # for a given doctor at a given time. The primary defense is the
        # row lock taken in main.py's create_appointment() (which
        # serializes concurrent bookings for the same doctor and lets the
        # existing queue-shifting logic do its job correctly); this index
        # is the backstop for cases the lock can't cover (e.g. SQLite,
        # which has no real row-level locking - see database.py).
        # Cancelled appointments are excluded so a freed-up slot can be
        # rebooked.
        Index(
            "ix_appointments_doctor_time_active",
            "doctor_id",
            "appointment_time",
            unique=True,
            postgresql_where=(status != "CANCELLED"),
            sqlite_where=(status != "CANCELLED"),
        ),
    )


class AdminUser(Base):
    """Staff/admin account used to protect sensitive operations
    (doctor availability overrides, cancellations, reallocation,
    data export, analytics) behind authentication."""

    __tablename__ = "admin_users"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    email = Column(String, unique=True, nullable=False, index=True)
    hashed_password = Column(String, nullable=False)
    full_name = Column(String, nullable=True)
    role = Column(String, default="admin")  # admin, staff
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AuditLog(Base):
    """Records who did what to sensitive operations (cancellations,
    reallocations, availability overrides, data exports) and when.
    Append-only from the application's perspective - nothing ever
    updates or deletes rows here."""

    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    admin_email = Column(String, nullable=False, index=True)
    action = Column(String, nullable=False, index=True)
    detail = Column(String, nullable=True)
    ip_address = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class RevokedToken(Base):
    """Tokens invalidated before their natural expiry (explicit logout,
    or an admin forcibly ending a session). Checked on every
    authenticated request - see auth.py:get_current_admin.

    Stored in the DB rather than in-memory so revocation is honored
    consistently across every gunicorn worker process and survives a
    restart - an in-memory blocklist would only protect the one worker
    that happened to handle the logout call.
    """

    __tablename__ = "revoked_tokens"

    jti = Column(String, primary_key=True)  # JWT ID claim - unique per issued token
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    revoked_at = Column(DateTime(timezone=True), server_default=func.now())
