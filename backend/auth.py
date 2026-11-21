"""
Authentication layer.

Provides:
- password hashing (bcrypt via passlib)
- JWT access-token creation/verification (python-jose)
- `get_current_admin` FastAPI dependency to protect sensitive routes
- `ensure_default_admin` to auto-create the first admin account from env
  vars on startup, so the API is usable out of the box without a manual
  bootstrap step.

Usage in a route:

    @app.post("/api/admin/doctors/{doctor_id}/toggle")
    def toggle(doctor_id: str, admin: AdminUser = Depends(get_current_admin)):
        ...
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from models import AdminUser, AuditLog, RevokedToken

logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# tokenUrl is documentation-only (drives the "Authorize" button in
# /docs); the real endpoint is /api/auth/login.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/auth/login", auto_error=False)


def validate_password_strength(password: str) -> Optional[str]:
    """Returns an error message if the password is too weak, else None.
    Kept intentionally simple (length + letter + digit) - NIST 800-63B
    guidance favors length and breach-checking over complex character-
    class rules, but "at least one letter and one digit" catches the
    most common weak passwords (all-digits, single dictionary word)
    without being obnoxious."""
    if len(password) < settings.MIN_PASSWORD_LENGTH:
        return f"Password must be at least {settings.MIN_PASSWORD_LENGTH} characters"
    if not any(c.isalpha() for c in password):
        return "Password must contain at least one letter"
    if not any(c.isdigit() for c in password):
        return "Password must contain at least one digit"
    return None


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def create_access_token(subject: str, expires_minutes: Optional[int] = None) -> str:
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=expires_minutes or settings.JWT_EXPIRE_MINUTES
    )
    payload = {
        "sub": subject,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "jti": uuid.uuid4().hex,  # unique per token - lets a single token be revoked individually
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> Optional[str]:
    payload = _decode_token_payload(token)
    return payload.get("sub") if payload else None


def _decode_token_payload(token: str) -> Optional[dict]:
    try:
        return jwt.decode(
            token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
        )
    except JWTError:
        return None


def revoke_token(db: Session, token: str) -> bool:
    """Invalidate a token immediately, before its natural expiry. Used by
    the logout endpoint. Returns False if the token was already invalid/
    malformed (nothing to revoke)."""
    payload = _decode_token_payload(token)
    if not payload or "jti" not in payload:
        return False

    exp_timestamp = payload.get("exp")
    expires_at = (
        datetime.fromtimestamp(exp_timestamp, tz=timezone.utc)
        if exp_timestamp
        else datetime.now(timezone.utc) + timedelta(minutes=settings.JWT_EXPIRE_MINUTES)
    )

    existing = db.query(RevokedToken).filter(RevokedToken.jti == payload["jti"]).first()
    if not existing:
        db.add(RevokedToken(jti=payload["jti"], expires_at=expires_at))
        db.commit()
    return True


def is_token_revoked(db: Session, jti: str) -> bool:
    return db.query(RevokedToken).filter(RevokedToken.jti == jti).first() is not None


def purge_expired_revocations(db: Session) -> int:
    """Delete revocation records whose underlying token has expired
    naturally anyway - keeps the table from growing unbounded. Safe to
    call periodically (e.g. once at startup); never removes anything
    still relevant, since an expired token needs no revocation record
    to be rejected - decode_access_token already rejects it on its own.
    """
    deleted = (
        db.query(RevokedToken)
        .filter(RevokedToken.expires_at < datetime.now(timezone.utc))
        .delete(synchronize_session=False)
    )
    db.commit()
    return deleted


def authenticate_admin(db: Session, email: str, password: str) -> Optional[AdminUser]:
    user = db.query(AdminUser).filter(AdminUser.email == email.lower()).first()
    if not user or not user.is_active:
        return None
    if not verify_password(password, user.hashed_password):
        return None
    return user


def get_current_admin(
    request: Request,
    token: Optional[str] = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> AdminUser:
    """FastAPI dependency: requires a valid Bearer token, returns the admin.

    Raises 401 if the token is missing/invalid/expired/revoked, or the
    account was deactivated after the token was issued.
    """
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if not token:
        raise credentials_error

    payload = _decode_token_payload(token)
    if not payload or "sub" not in payload:
        raise credentials_error
    email = payload["sub"]

    # A token can be structurally valid and unexpired but still have
    # been explicitly logged out (see /api/auth/logout) - that's exactly
    # what this closes the gap on: without it, "logging out" only ever
    # cleared the token client-side, and a copied/stolen token stayed
    # usable until it naturally expired up to JWT_EXPIRE_MINUTES later.
    jti = payload.get("jti")
    if jti and is_token_revoked(db, jti):
        raise credentials_error

    user = db.query(AdminUser).filter(AdminUser.email == email).first()
    if not user or not user.is_active:
        raise credentials_error

    # Stashed so the request-logging middleware can attach an audit-log
    # entry after the request completes, without every protected route
    # needing to call the audit logger itself.
    request.state.acting_admin_email = user.email

    return user


def get_optional_admin(
    request: Request,
    token: Optional[str] = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> Optional[AdminUser]:
    """Like get_current_admin, but anonymous or invalid callers get None
    instead of a 401. For endpoints that stay public but return less to
    people who are not signed in (see mask_patient_name)."""
    if not token:
        return None
    try:
        return get_current_admin(request, token, db)
    except HTTPException:
        return None


def mask_patient_name(name: Optional[str]) -> str:
    """Reduce a patient's name to first name plus last initial ("Priya
    Singh" -> "Priya S."), so the public queue stays readable without
    publishing identities. Staff see full names."""
    parts = (name or "").split()
    if not parts:
        return ""
    if len(parts) == 1:
        return f"{parts[0][0]}."
    return f"{parts[0]} {parts[-1][0]}."


def require_role(*allowed_roles: str):
    """Dependency factory: require the caller's admin account to have one
    of the given roles, on top of being authenticated at all.

    Usage:
        admin: AdminUser = Depends(require_role("admin"))       # admin-only
        staff: AdminUser = Depends(require_role("admin", "staff"))  # either
    """

    def dependency(admin: AdminUser = Depends(get_current_admin)) -> AdminUser:
        if admin.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This action requires one of the following roles: {', '.join(allowed_roles)}",
            )
        return admin

    return dependency


def log_action(
    db: Session,
    admin: AdminUser,
    action: str,
    detail: str = "",
    ip_address: str = "",
) -> None:
    """Append an audit-log entry for a sensitive admin action. Never
    raises - a logging failure should never block the underlying
    operation, it just gets logged to the app logger instead."""
    try:
        db.add(
            AuditLog(
                admin_email=admin.email,
                action=action,
                detail=detail[:2000] if detail else None,
                ip_address=ip_address or None,
            )
        )
        db.commit()
    except Exception:
        logger.exception("Failed to write audit log entry for action=%s", action)
        db.rollback()


def ensure_default_admin(db: Session) -> None:
    """Create the first admin account from ADMIN_EMAIL / ADMIN_PASSWORD if
    the admin_users table is empty. Safe to call on every startup -
    including when multiple gunicorn/uvicorn workers boot concurrently
    and race to do this at the same time: whichever worker loses the
    race hits a UNIQUE constraint violation on insert, which is caught
    and treated as "someone else already created it," not an error.
    """
    if db.query(AdminUser).count() > 0:
        return

    email = settings.ADMIN_EMAIL.lower().strip()
    admin = AdminUser(
        email=email,
        hashed_password=hash_password(settings.ADMIN_PASSWORD),
        full_name="Default Admin",
        role="admin",
        is_active=True,
    )
    db.add(admin)
    try:
        db.commit()
    except IntegrityError:
        # Another worker won the race and already inserted this account
        # (or one with this email) between our count() check and this
        # commit - that's success, not failure. Roll back our half-done
        # transaction and move on.
        db.rollback()
        logger.info(
            "Default admin already created by another worker process - skipping"
        )
        return

    logger.info("Seeded default admin account: %s", email)
    if settings.ADMIN_PASSWORD == "ChangeMe123!":
        logger.warning(
            "Default admin password is still the placeholder value. "
            "Set ADMIN_PASSWORD in your environment before deploying to production."
        )
