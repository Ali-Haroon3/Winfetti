from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def _make_engine():
    return create_engine(get_settings().database_url, pool_pre_ping=True)


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """Request-scoped session. Routes commit explicitly; anything uncommitted
    is rolled back on the way out."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.rollback()
        db.close()
