import os

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool

from config import settings

DATABASE_URL = settings.DATABASE_URL

# Some hosting providers (Railway, Render, Heroku) still hand out
# "postgres://" URLs, but SQLAlchemy 1.4+/2.x requires "postgresql://".
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

connect_args = {}
engine_kwargs = {"pool_pre_ping": True}

if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

    if ":memory:" in DATABASE_URL:
        # An in-memory DB only exists for the lifetime of a single
        # connection. Force every checkout to reuse the same connection
        # (used by the test suite) so tables/data survive across requests.
        engine_kwargs["poolclass"] = StaticPool
    elif DATABASE_URL == "sqlite:///./mediflow.db":
        # SQLite files are relative to the backend/ directory by default.
        db_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "mediflow.db"
        )
        DATABASE_URL = f"sqlite:///{db_path}"
else:
    # Real pool sizing only applies to server-based databases (Postgres/MySQL).
    engine_kwargs["pool_size"] = settings.DB_POOL_SIZE
    engine_kwargs["max_overflow"] = settings.DB_MAX_OVERFLOW

engine = create_engine(DATABASE_URL, connect_args=connect_args, **engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
