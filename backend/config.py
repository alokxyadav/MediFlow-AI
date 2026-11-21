"""
Centralized application configuration.

All environment-dependent values (secrets, URLs, feature flags) live here
and nowhere else. Nothing in the codebase should read os.environ directly
outside this file - import `settings` instead.

Values are loaded from, in order of precedence:
1. Real environment variables (e.g. set by Docker / Railway / Render)
2. A local `.env` file (see `.env.example`) - convenient for local dev
3. The defaults below (safe for local/demo use only)

In production (ENV=production), `validate_production_safety()` is called
at startup and refuses to boot if obviously-unsafe defaults are still in
place (placeholder JWT secret, placeholder admin password, wildcard
CORS). Fail fast beats a silent, subtly-broken deployment.
"""
import sys
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict

# Sentinel values, not random ones. A previous version of this file
# auto-generated a random JWT secret per process if none was set - that
# is actively dangerous with more than one worker (gunicorn/uvicorn
# multi-worker, or any horizontally-scaled deployment): each process
# would sign tokens with a *different* secret, so a token issued by one
# worker would randomly fail validation on another. Using a fixed,
# obviously-fake sentinel instead means: (a) all dev workers agree with
# each other, and (b) production startup validation below can reliably
# detect "this was never actually configured" and refuse to boot.
_INSECURE_DEFAULT_JWT_SECRET = "INSECURE-DEV-ONLY-CHANGE-ME-IN-PRODUCTION"
_INSECURE_DEFAULT_ADMIN_PASSWORD = "ChangeMe123!"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- General -----------------------------------------------------
    ENV: str = "development"  # development | staging | production
    DEBUG: bool = True
    APP_NAME: str = "MediFlow AI Backend"
    APP_VERSION: str = "2.1.0"

    # --- Database ------------------------------------------------------
    DATABASE_URL: str = "sqlite:///./mediflow.db"
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    # How long (seconds) to retry connecting to the DB at startup before
    # giving up - useful when the DB container/service is still coming
    # up (e.g. docker-compose without a healthcheck, or a managed DB
    # that's momentarily unreachable during a deploy).
    DB_STARTUP_RETRY_SECONDS: int = 30

    # --- Security / Auth -----------------------------------------------
    JWT_SECRET_KEY: str = _INSECURE_DEFAULT_JWT_SECRET
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 60 * 12  # 12 hours

    ADMIN_EMAIL: str = "admin@mediflow.ai"
    ADMIN_PASSWORD: str = _INSECURE_DEFAULT_ADMIN_PASSWORD  # only used to auto-seed the first admin

    # Minimum password strength enforced on staff/admin password
    # creation and self-service change (see auth.py:validate_password_strength).
    MIN_PASSWORD_LENGTH: int = 8

    # --- CORS ------------------------------------------------------------
    # Comma-separated list of allowed origins, e.g.
    # "https://your-frontend.com,http://localhost:5173"
    CORS_ORIGINS: str = (
        "https://sujalmishra161-code.github.io,"
        "http://localhost:5173,http://127.0.0.1:5173,"
        "http://localhost:3000,http://127.0.0.1:3000"
    )

    # Comma-separated list of hostnames this API is allowed to be served
    # as (Host header validation - blocks host-header-poisoning attacks).
    # "*" (default) disables the check - fine for local dev, tighten in
    # production, e.g. "api.yourhospital.com".
    ALLOWED_HOSTS: str = "*"

    # --- Third-party APIs -----------------------------------------------
    GEMINI_API_KEY: str = ""

    # --- Rate limiting ----------------------------------------------------
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_DEFAULT: str = "60/minute"
    RATE_LIMIT_CLASSIFY: str = "20/minute"
    RATE_LIMIT_BOOKING: str = "10/minute"

    # --- Observability -----------------------------------------------------
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = False
    METRICS_ENABLED: bool = True
    SENTRY_DSN: str = ""  # optional; error tracking is skipped if unset

    # --- Request limits ---------------------------------------------------
    MAX_REQUEST_BODY_BYTES: int = 1_000_000  # 1 MB - plenty for JSON API bodies

    @property
    def cors_origins_list(self) -> List[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def allowed_hosts_list(self) -> List[str]:
        return [h.strip() for h in self.ALLOWED_HOSTS.split(",") if h.strip()]

    @property
    def is_production(self) -> bool:
        return self.ENV.lower() == "production"


settings = Settings()


def validate_production_safety() -> None:
    """Refuse to boot in production with unsafe placeholder config.

    Called once at application startup (see main.py). Intentionally
    raises SystemExit rather than continuing to run in a state that
    looks fine but silently breaks auth or exposes data - a crash-on-
    boot is loud and immediate; a mis-signed-token bug discovered by a
    user three days later is not.
    """
    if not settings.is_production:
        return

    problems = []

    if settings.JWT_SECRET_KEY == _INSECURE_DEFAULT_JWT_SECRET:
        problems.append(
            "JWT_SECRET_KEY is still the insecure placeholder. Generate a "
            "real one: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )

    if settings.ADMIN_PASSWORD == _INSECURE_DEFAULT_ADMIN_PASSWORD:
        problems.append(
            "ADMIN_PASSWORD is still the placeholder 'ChangeMe123!'. Set a "
            "real password before deploying."
        )

    if "*" in settings.cors_origins_list:
        problems.append(
            "CORS_ORIGINS includes '*', which is unsafe combined with "
            "allow_credentials=True. List explicit origins instead."
        )

    if settings.DATABASE_URL.startswith("sqlite"):
        problems.append(
            "DATABASE_URL is SQLite in production. SQLite does not handle "
            "concurrent writers safely under real traffic - use Postgres "
            "(see docker-compose.yml / docs/DEPLOYMENT.md). If this is "
            "intentional for a low-traffic demo, set ENV=staging instead "
            "to skip this check."
        )

    if problems:
        message = (
            "\n\nREFUSING TO START IN PRODUCTION - unsafe configuration:\n"
            + "\n".join(f"  - {p}" for p in problems)
            + "\n\nSee backend/.env.example for the full list of settings.\n"
        )
        print(message, file=sys.stderr)
        raise SystemExit(1)
