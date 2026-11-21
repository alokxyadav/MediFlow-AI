from datetime import date as date_type
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

VALID_PRIORITIES = {"LOW", "MEDIUM", "HIGH", "EMERGENCY"}


def _reject_past_date(d: date_type, field: str) -> None:
    """Reject bookings for days that have already passed.

    The cutoff is one day before today's UTC date rather than today itself:
    a user's local calendar date can lag the server's UTC date by up to a day
    (for example late in the evening in the Americas), and rejecting their
    legitimate "today" would be a bug. Anything older than that is
    unambiguously in the past in every timezone.
    """
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=1)
    if d < cutoff:
        raise ValueError(f"{field} cannot be in the past")



class PatientRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    age: int = Field(..., ge=0, le=130)
    symptoms: str = Field(..., min_length=2, max_length=1000)
    location: str = Field(..., min_length=1, max_length=200)
    preferred_date: str = Field(..., description="YYYY-MM-DD")
    preferred_time: str = Field(..., description="HH:MM")

    @field_validator("name", "symptoms", "location")
    @classmethod
    def strip_and_require_nonblank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @field_validator("preferred_date")
    @classmethod
    def valid_date(cls, v: str) -> str:
        try:
            parsed = date_type.fromisoformat(v)
        except ValueError as exc:
            raise ValueError("preferred_date must be in YYYY-MM-DD format") from exc
        _reject_past_date(parsed, "preferred_date")
        return v

    @field_validator("preferred_time")
    @classmethod
    def valid_time(cls, v: str) -> str:
        parts = v.split(":")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            raise ValueError("preferred_time must be in HH:MM format")
        hh, mm = int(parts[0]), int(parts[1])
        if not (0 <= hh < 24 and 0 <= mm < 60):
            raise ValueError("preferred_time must be a valid 24h HH:MM time")
        return v


class AIClassifyResponse(BaseModel):
    specialty: str
    urgency: str
    confidence: float


class DoctorRecommendation(BaseModel):
    doctor_id: str
    name: str
    specialty: str
    qualification: str
    rating: float
    available_now: bool
    hospital: Dict[str, Any]
    distance_km: float
    score: float
    breakdown: Dict[str, float]
    consultation_duration: int
    queue_count: int
    workload: float


class DoctorRecommendResponse(BaseModel):
    radius_km: float
    escalated: bool
    message: str
    doctors: List[DoctorRecommendation]
    escalation_options: Optional[Dict[str, Any]] = None


class AppointmentCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    age: int = Field(..., ge=0, le=130)
    symptoms: str = Field(..., min_length=2, max_length=1000)
    location: str = Field(..., min_length=1, max_length=200)
    doctor_id: str
    hospital_id: str
    appointment_time: str  # YYYY-MM-DD HH:MM
    priority: str

    @field_validator("appointment_time")
    @classmethod
    def valid_appointment_time(cls, v: str) -> str:
        v = v.strip()
        try:
            parsed = datetime.strptime(v, "%Y-%m-%d %H:%M")
        except ValueError as exc:
            raise ValueError("appointment_time must be in YYYY-MM-DD HH:MM format") from exc
        _reject_past_date(parsed.date(), "appointment_time")
        return v

    @field_validator("priority")
    @classmethod
    def valid_priority(cls, v: str) -> str:
        v = v.upper().strip()
        if v not in VALID_PRIORITIES:
            raise ValueError(f"priority must be one of {sorted(VALID_PRIORITIES)}")
        return v


class AppointmentResponse(BaseModel):
    appointment_id: str
    patient_name: str
    doctor_name: str
    hospital_name: str
    appointment_time: str
    priority: str
    status: str
    notes: Optional[str] = None


# --------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------

class AdminLoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in_minutes: int


class AdminProfile(BaseModel):
    email: str
    full_name: Optional[str] = None
    role: str


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=8, max_length=128)


class CreateStaffRequest(BaseModel):
    email: EmailStr
    full_name: Optional[str] = Field(None, max_length=120)
    role: str = Field("staff", description="'staff' or 'admin'")
    temporary_password: Optional[str] = Field(
        None,
        min_length=8,
        max_length=128,
        description="If omitted, a random one is generated and returned once.",
    )

    @field_validator("role")
    @classmethod
    def valid_role(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in {"admin", "staff"}:
            raise ValueError("role must be 'admin' or 'staff'")
        return v


class CreateStaffResponse(BaseModel):
    email: str
    role: str
    temporary_password: str


class AuditLogEntry(BaseModel):
    id: int
    admin_email: str
    action: str
    detail: Optional[str] = None
    created_at: str


# --------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------

class ErrorResponse(BaseModel):
    error: str
    detail: Optional[Any] = None
    request_id: Optional[str] = None
