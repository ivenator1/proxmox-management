"""SQLAlchemy user model for FastAPI-Users authentication."""
from __future__ import annotations

from fastapi_users.db import SQLAlchemyBaseUserTable
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """SQLAlchemy declarative base."""
    pass


class User(SQLAlchemyBaseUserTable[int], Base):
    """User model for FastAPI-Users with SQLAlchemy.

    Single admin user (email='admin@localhost', password set at install time).
    """
    pass
