"""Section 6 — Vendor master."""

from __future__ import annotations

from sqlalchemy import Column, String, Text
from sqlalchemy.orm import relationship

from app.config.database import Base
from app.models.mixins import SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Vendor(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "vendors"

    name = Column(String(255), nullable=False, unique=True, index=True)
    contact_person = Column(String(255), nullable=True)
    contact_email = Column(String(320), nullable=True)
    contact_phone = Column(String(50), nullable=True)
    notes = Column(Text, nullable=True)

    requirements = relationship("Requirement", back_populates="vendor")
    profile_statuses = relationship("ProfileVendorStatus", back_populates="vendor")

    def __repr__(self) -> str:
        return f"<Vendor {self.name}>"
