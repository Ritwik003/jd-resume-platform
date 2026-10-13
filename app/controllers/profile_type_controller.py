"""/profile-types — section 10 (a person may have several profile types)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.auth.current_user import get_current_user
from app.config.database import get_db
from app.models.person import ProfileType
from app.models.user import User
from app.repositories.base_repository import ensure_unique
from app.schemas.masters import ProfileTypeCreate, ProfileTypeResponse

router = APIRouter()


@router.get("", response_model=list[ProfileTypeResponse])
def list_profile_types(db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> list[ProfileType]:
    return db.query(ProfileType).filter(ProfileType.is_active.is_(True)).order_by(ProfileType.name).all()


@router.post("", response_model=ProfileTypeResponse, status_code=status.HTTP_201_CREATED)
def create_profile_type(
    payload: ProfileTypeCreate, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> ProfileType:
    ensure_unique(db, ProfileType, "name", payload.name)
    profile_type = ProfileType(**payload.model_dump())
    db.add(profile_type)
    db.commit()
    db.refresh(profile_type)
    return profile_type
