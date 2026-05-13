import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
IS_PRODUCTION = os.getenv("ENVIRONMENT", "").lower() == "production" or bool(os.getenv("VERCEL"))

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is required. Configure a Postgres/Supabase connection string "
        "for Vercel before starting the app."
    )

# Render/local development previously fell back to SQLite. Vercel production must
# use an explicit external database because the serverless filesystem is ephemeral.
if DATABASE_URL.startswith("sqlite") and IS_PRODUCTION:
    raise RuntimeError("SQLite DATABASE_URL is not supported in production. Use Postgres/Supabase.")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine_kwargs = {}
if DATABASE_URL.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}
    if DATABASE_URL in {"sqlite://", "sqlite:///:memory:"}:
        engine_kwargs["poolclass"] = StaticPool
else:
    engine_kwargs.update(
        {
            "pool_pre_ping": True,
            "pool_recycle": 300,
        }
    )

engine = create_engine(DATABASE_URL, **engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
