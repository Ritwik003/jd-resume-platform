"""/files — download the generated DOCX and PDF."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.auth.current_user import get_current_user
from app.config.database import get_db
from app.models.resume_version import ResumeFile
from app.models.user import User
from app.repositories.base_repository import get_or_404
from app.services import storage
from app.utils.exceptions import NotFoundError

router = APIRouter()

_MEDIA = {
    "DOCX": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "PDF": "application/pdf",
}


@router.get("/{file_id}/download")
def download(file_id: UUID, db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> FileResponse:
    record = get_or_404(db, ResumeFile, file_id, "File")
    path = storage.absolute_path(record.storage_path)
    if not path.exists():
        raise NotFoundError("The stored file is missing from disk; re-render this version")
    return FileResponse(
        path=str(path),
        filename=record.file_name,
        media_type=_MEDIA.get(record.file_format, "application/octet-stream"),
    )
