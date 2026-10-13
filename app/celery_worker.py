"""Optional Celery worker for the two jobs that genuinely want to run off-request.

The platform is designed to work with *or* without a broker. Section 31 requires
reservations to expire on their own, and the in-process thread sweeper already
does that correctly for a single-process deployment. Celery becomes worthwhile
once the API runs more than one worker process, because then the thread sweeper
runs N times concurrently instead of once.

``celery_available()`` is what the rest of the app asks. It returns False when no
broker is configured or the broker cannot be reached, and every caller then falls
back to doing the work inline, so a stopped Redis degrades rather than breaks.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from celery import Celery
from celery.schedules import crontab

from app.config import (
    CELERY_BROKER_URL,
    CELERY_ENABLED,
    CELERY_RESULT_BACKEND,
    CELERY_TASK_ALWAYS_EAGER,
    RESERVATION_SWEEP_INTERVAL_SECONDS,
)
from app.model_registry import import_all_models

logger = logging.getLogger(__name__)

# main.py does this before its own first query, but this module IS the entry
# point for the worker/beat processes -- nobody else imports the model
# registry for them. Every model has to be loaded before SQLAlchemy resolves
# ANY mapper, because a string relationship() argument (e.g. Person's
# "PersonTechnology") is looked up in the class registry at configuration
# time, not at import time. Skip this and the first real query in this
# process -- here, the reservation sweep -- fails with
# "expression 'PersonTechnology' failed to locate a name", not because
# anything is wrong with the model, but because this process never imported it.
import_all_models()

celery_app = Celery(
    "jd_resume_platform",
    broker=CELERY_BROKER_URL or None,
    backend=CELERY_RESULT_BACKEND or None,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_always_eager=CELERY_TASK_ALWAYS_EAGER,
    task_acks_late=True,
    worker_max_tasks_per_child=200,
    broker_connection_retry_on_startup=True,
    beat_schedule={
        "sweep-expired-reservations": {
            "task": "app.celery_worker.sweep_expired_reservations",
            # Section 9/31 — the same cadence the thread sweeper uses.
            "schedule": float(RESERVATION_SWEEP_INTERVAL_SECONDS),
        },
        "nightly-storage-audit": {
            "task": "app.celery_worker.log_storage_summary",
            "schedule": crontab(hour=2, minute=0),
        },
    },
)


@lru_cache(maxsize=1)
def celery_available() -> bool:
    """True only when a broker is configured and actually answering.

    Cached because it opens a socket: callers hit this on every request that
    could offload work, and a per-request connection attempt to a dead Redis
    would cost more than the work being offloaded.
    """
    if not CELERY_ENABLED:
        return False
    try:
        conn = celery_app.connection()
        conn.ensure_connection(max_retries=0, timeout=2)
        conn.release()
        return True
    except Exception as exc:  # noqa: BLE001 — any broker failure means "run inline"
        logger.warning("Celery broker unreachable (%s); running work in-process", exc)
        return False


# ─────────────────────────────────────────────────────────────── tasks
@celery_app.task(name="app.celery_worker.sweep_expired_reservations")
def sweep_expired_reservations() -> int:
    """Section 31 — release reservations whose expiry has passed."""
    from app.config.database import SessionLocal
    from app.services.availability import expire_stale_reservations

    db = SessionLocal()
    try:
        released = expire_stale_reservations(db)
        db.commit()
        if released:
            logger.info("Celery released %s expired reservation(s)", released)
        return released
    finally:
        db.close()


@celery_app.task(name="app.celery_worker.render_version_files_task", bind=True, max_retries=3)
def render_version_files_task(self, session_id: str, version_id: str, formats: list[str]) -> list[str]:
    """Section 27 — produce the DOCX/PDF for a version off the request thread."""
    from uuid import UUID

    from app.config.database import SessionLocal
    from app.models.resume_version import ResumeVersion
    from app.services import resume_session_service as svc

    db = SessionLocal()
    try:
        session = svc.get_session(db, UUID(session_id))
        version = db.query(ResumeVersion).filter(ResumeVersion.id == UUID(version_id)).first()
        if version is None:
            return []
        files = svc.render_version_files(db, session, version, formats)
        db.commit()
        return [str(f.id) for f in files]
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        raise self.retry(exc=exc, countdown=10) from exc
    finally:
        db.close()


@celery_app.task(name="app.celery_worker.log_storage_summary")
def log_storage_summary() -> dict:
    """Cheap housekeeping probe so beat has something verifiable to run."""
    from app.services import storage

    root = storage.ROOT
    if not root.exists():
        return {"files": 0, "bytes": 0}
    files = [p for p in root.rglob("*") if p.is_file()]
    summary = {"files": len(files), "bytes": sum(p.stat().st_size for p in files)}
    logger.info("Storage: %(files)s files, %(bytes)s bytes", summary)
    return summary
