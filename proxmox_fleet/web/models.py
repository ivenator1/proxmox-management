"""SQLAlchemy user model for FastAPI-Users authentication."""
from __future__ import annotations

from fastapi_users.db import SQLAlchemyBaseUserTable
from sqlalchemy import Integer
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """SQLAlchemy declarative base."""


class User(SQLAlchemyBaseUserTable[int], Base):
    """Dashboard user (single ``admin@fleet.lan`` account, password set at
    install time). ``SQLAlchemyBaseUserTable`` provides email/hashed_password/
    is_active/is_superuser/is_verified; the integer PK is declared here (only
    the UUID variant ships one)."""

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
