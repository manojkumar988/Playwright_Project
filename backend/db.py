from __future__ import annotations

import os
from collections.abc import Iterator

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

load_dotenv()


DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://postgres@localhost:5432/QA",
)

engine = create_engine(
    DATABASE_URL,
    future=True,
    echo=False,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from . import orm_models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    # create_all does not add columns to an existing database; these idempotent
    # migrations preserve existing accounts while enabling email verification.
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT TRUE"))
        connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS verification_token_hash VARCHAR(128)"))
        connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS verification_expires_at TIMESTAMP"))
        connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_reset_token_hash VARCHAR(128)"))
        connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_reset_expires_at TIMESTAMP"))
