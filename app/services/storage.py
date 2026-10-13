"""Local filesystem storage for generated resumes.

Deliberately a thin, swappable seam: section 37 lists "Object storage / internal
file storage", so the interface is path-in/path-out and the backend can move to
S3 or MinIO by replacing this module only.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import UUID

from app.config import FILE_STORAGE_ROOT
from app.utils.helper import slugify

ROOT = Path(FILE_STORAGE_ROOT)


def session_dir(session_id: UUID) -> Path:
    path = ROOT / "resumes" / str(session_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def template_dir() -> Path:
    """Where uploaded resume templates live (section 26)."""
    path = ROOT / "templates"
    path.mkdir(parents=True, exist_ok=True)
    return path


def build_filename(person_name: str, profile_title: str, vendor_name: str, version_no: int, extension: str) -> str:
    """Matches the naming in the section 43 worked example."""
    stem = "_".join(
        part for part in (slugify(person_name), slugify(profile_title), slugify(vendor_name)) if part
    )
    return f"{stem}_V{version_no}.{extension.lower()}"


def relative_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def absolute_path(stored: str) -> Path:
    candidate = Path(stored)
    return candidate if candidate.is_absolute() else ROOT / candidate


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()
