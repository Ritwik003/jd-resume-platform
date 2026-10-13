from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, EmailStr

from app.schemas.common import ORMModel


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in_minutes: int


class UserResponse(ORMModel):
    id: UUID
    email: str
    full_name: str | None = None
    is_active: bool


class UserCreate(BaseModel):
    email: EmailStr
    password: str
    full_name: str | None = None
