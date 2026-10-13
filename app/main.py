"""FastAPI application entrypoint."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import API_V1_PREFIX, APP_NAME, BASE_DIR, CORS_ORIGINS
from app.config.database import Base, engine
from app.config.logging_setup import configure_logging, register_exception_handlers
from app.controllers import (
    auth_controller,
    availability_controller,
    company_controller,
    dashboard_controller,
    file_controller,
    insight_controller,
    person_controller,
    profile_type_controller,
    project_controller,
    requirement_controller,
    resume_session_controller,
    resume_template_controller,
    search_controller,
    submission_controller,
    technology_controller,
    vendor_controller,
    vendor_tracking_controller,
)
from app.model_registry import import_all_models
from app.tasks import start_reservation_sweeper

configure_logging()
import_all_models()

@asynccontextmanager
async def lifespan(_: FastAPI):
    # SQLite only. Alembic owns the schema everywhere else, and calling
    # create_all() against Postgres takes catalogue locks on every table at
    # startup — with --reload spawning an overlapping process, that is enough
    # to block the reservation sweeper until its statement_timeout fires.
    if engine.dialect.name == "sqlite":
        Base.metadata.create_all(engine)
    start_reservation_sweeper()
    yield


app = FastAPI(
    lifespan=lifespan,
    title=APP_NAME,
    version="0.1.0",
    description="JD-Driven Profile Allocation & AI Resume Generation Platform",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_exception_handlers(app)

# Section 38 — one router per functional module.
app.include_router(auth_controller.router, prefix=f"{API_V1_PREFIX}/auth", tags=["auth"])
app.include_router(profile_type_controller.router, prefix=f"{API_V1_PREFIX}/profile-types", tags=["profile-types"])
app.include_router(technology_controller.router, prefix=f"{API_V1_PREFIX}/technologies", tags=["technologies"])
app.include_router(company_controller.router, prefix=f"{API_V1_PREFIX}/companies", tags=["companies"])
app.include_router(vendor_controller.router, prefix=f"{API_V1_PREFIX}/vendors", tags=["vendors"])
app.include_router(person_controller.router, prefix=f"{API_V1_PREFIX}/profiles", tags=["profiles"])
app.include_router(project_controller.router, prefix=f"{API_V1_PREFIX}/projects", tags=["projects"])
app.include_router(requirement_controller.router, prefix=f"{API_V1_PREFIX}/requirements", tags=["requirements"])
app.include_router(availability_controller.router, prefix=f"{API_V1_PREFIX}/availability", tags=["availability"])
app.include_router(
    availability_controller.reservations_router,
    prefix=f"{API_V1_PREFIX}/reservations",
    tags=["reservations"],
)
app.include_router(
    resume_template_controller.router, prefix=f"{API_V1_PREFIX}/resume-templates", tags=["resume-templates"]
)
app.include_router(resume_session_controller.router, prefix=f"{API_V1_PREFIX}/resume-sessions", tags=["resume-studio"])
app.include_router(submission_controller.router, prefix=f"{API_V1_PREFIX}/submissions", tags=["submissions"])
# §29 — the vendor's side of the ledger: who is ours to talk about, per vendor,
# and where each one stands. Its own module because the unit is the
# (person, vendor) pair rather than the document that went out.
app.include_router(
    vendor_tracking_controller.router, prefix=f"{API_V1_PREFIX}/vendor-tracking", tags=["vendor-tracking"]
)
app.include_router(file_controller.router, prefix=f"{API_V1_PREFIX}/files", tags=["files"])
app.include_router(search_controller.router, prefix=f"{API_V1_PREFIX}/search", tags=["search"])
# Cross-entity drill-downs: a role to its people, a technology to its depth,
# a person to their resume history, and the reservations currently held.
app.include_router(insight_controller.router, prefix=API_V1_PREFIX, tags=["insights"])
# The landing screen: today's engagements, the holds with a clock on them, the
# vendors taken on this month, and four trends across the pool. One endpoint
# because it is one screen, and a landing page that fires seven requests is
# the one that feels slow.
app.include_router(
    dashboard_controller.router, prefix=f"{API_V1_PREFIX}/dashboard", tags=["dashboard"]
)


@app.get("/health", tags=["meta"])
def health() -> dict:
    """Liveness plus the infrastructure facts worth knowing at a glance."""
    from sqlalchemy import text

    from app.config import (
        CELERY_BROKER_URL,
        DATABASE_URL,
        LLM_MODEL,
        LLM_OFFLINE_MODE,
        LLM_PROVIDER,
        OPENAI_BASE_URL,
    )

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        database = "connected"
    except Exception as exc:  # noqa: BLE001 — health must report, never raise
        database = f"error: {exc}"

    try:
        from app.celery_worker import celery_available

        celery = "connected" if celery_available() else "unavailable — running in-process"
    except Exception:  # noqa: BLE001
        celery = "not installed"

    return {
        "status": "ok",
        "service": APP_NAME,
        "database": {
            # Never echo the password back out of a health endpoint.
            "engine": DATABASE_URL.split("://", 1)[0],
            "name": DATABASE_URL.rsplit("/", 1)[-1],
            "status": database,
        },
        "llm": {
            "provider": "offline-stub" if LLM_OFFLINE_MODE else LLM_PROVIDER,
            "model": LLM_MODEL,
            "base_url": OPENAI_BASE_URL or None,
        },
        "celery": {"broker": CELERY_BROKER_URL or None, "status": celery},
    }


# Section 21/37 — the Resume Studio web client, served by the same process so the
# browser talks to the API same-origin and no CORS or separate host is involved.
#
# The whole UI is one index.html with no build step and no filename hashing,
# so without an explicit Cache-Control the browser is free to keep serving a
# stale copy after every edit here — a real bug that cost real time on this
# project: a fix would land, the server would restart, and the browser would
# still be running yesterday's JS with no visible sign that was happening.
# no-store is deliberately blunt (never cache, not even "revalidate first")
# because this is an internal tool edited constantly, not a public site where
# that would cost meaningful bandwidth.
class _NoCacheStatic(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return response


app.mount("/app", _NoCacheStatic(directory=BASE_DIR / "app" / "static", html=True), name="ui")


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/app/")
