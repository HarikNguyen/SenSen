"""SQLite persistence layer: User & APIKey models (MVP scope, no document storage)."""

import os
import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker

DATABASE_URL = os.getenv("SENSEN_DATABASE_URL", "sqlite:///./saas.db")

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    api_keys = relationship("APIKey", back_populates="user", cascade="all, delete-orphan")


class APIKey(Base):
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String, unique=True, index=True, default=lambda: uuid.uuid4().hex)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    request_count = Column(Integer, default=0)
    deep_scan_count = Column(Integer, default=0)
    ocr_api_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="api_keys")


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _ensure_ocr_api_count_column()


def _ensure_ocr_api_count_column() -> None:
    """create_all() never adds a column to an existing table -- a saas.db
    predating this column would 500 instead of starting it at 0.
    """
    inspector = inspect(engine)
    if "api_keys" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("api_keys")}
    if "ocr_api_count" not in existing:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE api_keys ADD COLUMN ocr_api_count INTEGER DEFAULT 0"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
