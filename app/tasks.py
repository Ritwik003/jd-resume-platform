"""Section 31 — background sweeper that releases expired reservations.

A thread rather than Celery: the MVP has exactly one periodic job and adding a
broker for it would be infrastructure the requirements do not ask for. The sweep
is also run defensively on every availability read, so the worker is a safety net
rather than the only path.
"""

from __future__ import annotations

import logging
import threading
import time

from sqlalchemy.exc import OperationalError

from app.config import RESERVATION_SWEEP_INTERVAL_SECONDS, RESERVATION_SWEEPER_ENABLED
from app.config.database import SessionLocal
from app.services.availability import expire_stale_reservations

logger = logging.getLogger(__name__)


def _loop() -> None:
    while True:
        time.sleep(RESERVATION_SWEEP_INTERVAL_SECONDS)
        db = SessionLocal()
        try:
            count = expire_stale_reservations(db)
            # Committed on every tick, not only the ticks that found something.
            # The sweep issues a SELECT whatever it finds, and on Postgres that
            # opens a transaction; skipping the commit left it open until the
            # connection was handed back, so a job that runs every sixty
            # seconds and usually finds nothing spent most of its life holding
            # an idle-in-transaction session for no reason.
            db.commit()
            if count:
                logger.info("Reservation sweeper released %s profile(s)", count)
        except OperationalError as exc:
            # Lock contention or a statement timeout: the next tick retries in
            # a minute, so this is a one-line note rather than a stack trace.
            logger.warning("Reservation sweep skipped this tick: %s", str(exc.orig).strip())
            db.rollback()
        except Exception:
            logger.exception("Reservation sweeper failed")
            db.rollback()
        finally:
            db.close()


def start_reservation_sweeper() -> None:
    if not RESERVATION_SWEEPER_ENABLED:
        logger.info("Reservation sweeper disabled")
        return

    # When Celery beat is live it owns this schedule; running both would sweep
    # twice a minute from two places and make the logs lie about who released what.
    try:
        from app.celery_worker import celery_available

        if celery_available():
            logger.info("Celery broker reachable — reservation sweeping runs on Celery beat")
            return
    except Exception:  # noqa: BLE001 — Celery is optional; never block startup
        pass

    threading.Thread(target=_loop, daemon=True, name="reservation-sweeper").start()
    logger.info("Reservation sweeper started (every %ss)", RESERVATION_SWEEP_INTERVAL_SECONDS)
