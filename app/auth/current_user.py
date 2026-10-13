"""Authentication dependencies."""

from __future__ import annotations

from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.auth.hashing import decode_access_token
from app.config import API_V1_PREFIX
from app.config.database import get_db
from app.models.user import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{API_V1_PREFIX}/auth/login", auto_error=False)

_CREDENTIALS_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    if not token:
        raise _CREDENTIALS_ERROR
    payload = decode_access_token(token)
    if not payload or not payload.get("sub"):
        raise _CREDENTIALS_ERROR
    try:
        user_id = UUID(str(payload["sub"]))
    except ValueError as exc:
        raise _CREDENTIALS_ERROR from exc
    user = db.query(User).filter(User.id == user_id).first()
    if user is None or not user.is_active:
        raise _CREDENTIALS_ERROR
    return user
