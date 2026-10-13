"""Environment-driven application configuration.

Mirrors the reference backend's flat-module config style: read once at import,
expose plain module-level constants, fall back to a local SQLite file so the
project runs on a laptop with zero infrastructure.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

#: The repository root. This file sits at app/config/config.py, so the root is
#: three levels up — storage/, tools/ and alembic/ are resolved against it.
BASE_DIR = Path(__file__).resolve().parent.parent.parent


def _bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


# ---------------------------------------------------------------- application
APP_NAME = os.getenv("APP_NAME", "JD Resume Platform API")
APP_ENV = os.getenv("APP_ENV", "local")
DEBUG = _bool("DEBUG", "true")
API_V1_PREFIX = os.getenv("API_V1_PREFIX", "/api/v1")
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]

# ------------------------------------------------------------------- database
_raw_database_url = os.getenv("DATABASE_URL", "").strip()
if _raw_database_url:
    DATABASE_URL = _raw_database_url
else:
    _local_sqlite = BASE_DIR / "data" / "jd_resume_local.db"
    _local_sqlite.parent.mkdir(parents=True, exist_ok=True)
    DATABASE_URL = f"sqlite:///{_local_sqlite.as_posix()}"

SQL_ECHO = _bool("SQL_ECHO", "false")

# ----------------------------------------------------------------------- auth
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "dev-only-insecure-secret-change-me")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = _int("ACCESS_TOKEN_EXPIRE_MINUTES", 480)

# ------------------------------------------------------------------------ LLM
# Section 36: no external integrations required for MVP beyond the LLM itself.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").strip().lower()
LLM_MODEL = os.getenv("LLM_MODEL", "claude-sonnet-4-6")
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.2"))
# The ceiling on one reply, and the floor under every call in the platform:
# each site asks for max(LLM_MAX_TOKENS, its own need), so this lifts them all.
#
# Sized for a REASONING model, because that is what these are. Such a model
# spends most of its budget thinking before it writes a character, so a
# ceiling sized for the answer alone truncates the answer. Measured on this
# platform's provider: a 16k ceiling returned an EMPTY reply — sixteen
# thousand tokens of reasoning and nothing after it — where 48k finished
# comfortably in 27k. On the shorter calls the same starvation shows up as
# quality rather than emptiness: a validator that runs out of room mid-
# judgement returns a worse judgement, and nobody sees an error.
#
# This is a ceiling, not a spend. A short answer still bills as a short
# answer; the room only has to be there for the replies that need it.
LLM_MAX_TOKENS = _int("LLM_MAX_TOKENS", 32000)
# Raising the token ceiling without raising this only converts truncation into
# timeouts: tens of thousands of tokens take minutes on a reasoning model.
# Measured on the small calls here — 20 to 70 seconds each — and several
# minutes for a whole resume.
LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "600"))
LLM_MAX_RETRIES = _int("LLM_MAX_RETRIES", 3)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

# When true, LLM calls are replaced by a deterministic offline stub. Lets the
# whole end-to-end workflow be demoed and tested without any API key.
LLM_OFFLINE_MODE = _bool("LLM_OFFLINE_MODE", "false")

# ---------------------------------------------------------------- reservation
# Section 9 / 31: "The reservation duration should be configurable."
# How long selecting somebody holds them, whether their resume is still being
# composed (EDITING) or already approved and waiting on the vendor's answer
# (RESERVED). Short by design: an abandoned tab or a vendor going quiet should
# not shelve a person indefinitely — need more time, ask for it via
# ``extend_reservation`` instead of starting from a long default. When it runs
# out the profile returns to AVAILABLE and the session it belonged to goes
# back to EDITING, ready to be picked up again.
RESERVATION_DURATION_MINUTES = _int("RESERVATION_DURATION_MINUTES", 120)
RESERVATION_SWEEP_INTERVAL_SECONDS = _int("RESERVATION_SWEEP_INTERVAL_SECONDS", 60)
RESERVATION_SWEEPER_ENABLED = _bool("RESERVATION_SWEEPER_ENABLED", "true")

# --------------------------------------------------------------- file storage
FILE_STORAGE_ROOT = os.getenv("FILE_STORAGE_ROOT", str(BASE_DIR / "storage"))

# The PDF comes from compiling the template's LaTeX (see rendering/latex.py),
# which is the only route that can reflow around content of a different length.
# The old DOCX-to-PDF and draw-it-directly strategies, and the PDF_RENDERER /
# LIBREOFFICE_BINARY settings that chose between them, went with it.

# ----------------------------------------------------------------- validation
VALIDATION_ENABLED = _bool("VALIDATION_ENABLED", "true")

# Only three things are warned about, and all three are checkable facts: a stated
# experience total, a technology placed in a period before it was released, and a
# skill name that matches nothing in the technology master. Everything else the
# composition does — reframing a project, showing it under another company,
# taking the JD's job title — is a deliberate act by the operator, not a defect,
# and warning about it only teaches people to click through warnings.
VALIDATION_STRICT_FACTS_ONLY = _bool("VALIDATION_STRICT_FACTS_ONLY", "true")


# ------------------------------------------------------------ JD fit / moulding
# A profile that already matches the JD is transformed into the template as-is;
# below the threshold the history is moulded toward the role first. 90 is the
# operator-stated cut-off: at that point the reframing costs more than it adds.
JD_FIT_SKIP_MOULD_THRESHOLD = _int("JD_FIT_SKIP_MOULD_THRESHOLD", 90)

# The gate that actually decides it (see app/services/jd_fit.py): the profile is
# already the role when the job titles match AND enough of the JD's mandatory
# stack is evidenced. Both must hold; either alone is the case that needs
# moulding, not the case that skips it.
JD_FIT_ROLE_MATCH_MIN = _int("JD_FIT_ROLE_MATCH_MIN", 90)
JD_FIT_SKILL_MATCH_MIN = _int("JD_FIT_SKILL_MATCH_MIN", 70)

# Ground rules applied to version 1 for EVERY uploaded template (see
# app/services/ground_rules.py). They are settings rather than constants because
# they are house style, not laws of the domain.
RULE_DROP_EDUCATION = _bool("RULE_DROP_EDUCATION", "true")
RULE_NO_TIMELINE_BARS = _bool("RULE_NO_TIMELINE_BARS", "true")
RULE_MEET_JD_EXPERIENCE = _bool("RULE_MEET_JD_EXPERIENCE", "true")
RULE_ONE_PAGE = _bool("RULE_ONE_PAGE", "true")


# ------------------------------------------------------- the page budget
# Two pages, not one. Almost every requirement this platform serves asks for
# five or more years, and a career that long does not fit on one page without
# cutting the projects that win the shortlist. One page was the wrong default:
# it forced the composition to drop work the JD actually asked about.
#
# These are the DEFAULTS. An operator sets the counts per resume on the
# generation form (see GenerationOptions), and what they choose overrides
# everything here for that session.
#
# The floor is a target, not a rejection: a bank holding three projects cannot
# be made to yield four. The ceiling is enforced.
RESUME_MAX_PAGES = _int("RESUME_MAX_PAGES", 2)
RESUME_MIN_PROJECTS = _int("RESUME_MIN_PROJECTS", 4)
RESUME_MAX_PROJECTS = _int("RESUME_MAX_PROJECTS", 8)
RESUME_MIN_COMPANIES = _int("RESUME_MIN_COMPANIES", 3)
RESUME_MAX_COMPANIES = _int("RESUME_MAX_COMPANIES", 6)

#: Projects shown under each employer when the operator does not say otherwise.
RESUME_PROJECTS_PER_COMPANY = _int("RESUME_PROJECTS_PER_COMPANY", 2)

# What two pages hold once the header, summary and skills have taken their
# share. Applied per project so the trim is idempotent — rebuilding a resume
# twice removes the same bullets, never a second helping.
RESUME_MAX_BULLETS_PER_PROJECT = _int("RESUME_MAX_BULLETS_PER_PROJECT", 5)
# The whole document's bullet budget, spent across however many projects there
# are: fewer projects keep their full detail, more projects each give a little.
RESUME_MAX_TOTAL_BULLETS = _int("RESUME_MAX_TOTAL_BULLETS", 34)
# Two to three printed lines. The description sets up what the system was; the
# bullets say what was done to it.
RESUME_MAX_DESCRIPTION_CHARS = _int("RESUME_MAX_DESCRIPTION_CHARS", 320)
RESUME_MAX_SUMMARY_CHARS = _int("RESUME_MAX_SUMMARY_CHARS", 620)
RESUME_MAX_SKILL_GROUPS = _int("RESUME_MAX_SKILL_GROUPS", 8)
RESUME_MAX_SKILLS_PER_GROUP = _int("RESUME_MAX_SKILLS_PER_GROUP", 14)


# ------------------------------------------------------------------- celery
# Optional. When a broker is configured AND reachable, reservation sweeping and
# document rendering move to Celery; otherwise the in-process thread sweeper
# keeps running, so the platform works with or without Redis installed.
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "").strip()
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "").strip() or CELERY_BROKER_URL
CELERY_ENABLED = _bool("CELERY_ENABLED", "true") and bool(CELERY_BROKER_URL)
CELERY_TASK_ALWAYS_EAGER = _bool("CELERY_TASK_ALWAYS_EAGER", "false")


# ------------------------------------------------------------------- latex
# Section 26 templates are LaTeX, so a PDF needs an engine. Tectonic is not
# distributed through winget, so it is commonly dropped in as a loose binary —
# point LATEX_ENGINE_PATH at it, or leave it in ./tools and it is found there.
LATEX_ENGINE_PATH = os.getenv("LATEX_ENGINE_PATH", "").strip()
LATEX_TOOLS_DIR = os.getenv("LATEX_TOOLS_DIR", str(BASE_DIR / "tools"))
