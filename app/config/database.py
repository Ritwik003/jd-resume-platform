"""SQLAlchemy engine, session factory and declarative Base."""

from __future__ import annotations

import logging
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from app.config import DATABASE_URL, LLM_TIMEOUT_SECONDS, SQL_ECHO

logger = logging.getLogger(__name__)

#: How long a transaction may sit open without issuing a query before Postgres
#: terminates it. Generating a resume holds one open across the moulding call
#: and does NO database work while it waits, so this ceiling has to clear the
#: longest LLM call the app is configured to make, with room to spare. Derived
#: from LLM_TIMEOUT_SECONDS rather than written as a constant: the two were
#: independent before, a flat five minutes here silently killed generations the
#: moment LLM_TIMEOUT_SECONDS was raised past it, and the failure surfaced as
#: "server closed the connection unexpectedly" -- which reads like a database
#: fault and says nothing about the setting that actually caused it.
_IDLE_TX_TIMEOUT_MS = int(max(300.0, LLM_TIMEOUT_SECONDS * 1.5 + 300.0) * 1000)

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},
        pool_pre_ping=True,
        echo=SQL_ECHO,
    )
else:
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        pool_recycle=1800,
        echo=SQL_ECHO,
        # statement_timeout bounds one query. idle_in_transaction_session_timeout
        # bounds a whole transaction that was opened and then abandoned -- a
        # crashed request, or (as happened during this project's own
        # development) a duplicate server process left running after a
        # restart. Without it that connection can sit holding row locks
        # indefinitely, and the next ordinary request for the same row times
        # out with no slow query of its own to explain why. The ceiling tracks
        # the configured LLM timeout (see _IDLE_TX_TIMEOUT_MS above), because a
        # generation legitimately holds a transaction open for the whole of one
        # moulding call.
        #
        # statement_timeout stays at 30s: it bounds a single query, and no query
        # this app issues is slow -- the waiting happens between them.
        connect_args={
            "options": (
                "-c statement_timeout=30000 "
                f"-c idle_in_transaction_session_timeout={_IDLE_TX_TIMEOUT_MS}"
            )
        },
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, expire_on_commit=False)
Base = declarative_base()


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
