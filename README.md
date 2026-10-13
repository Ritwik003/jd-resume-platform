# JD-Driven Profile Allocation & AI Resume Generation Platform

Backend for the internal platform described in the requirements document: a
reusable pool of people, projects, companies and technologies, plus an agentic
workspace that composes vendor-specific resumes from that pool.

**Status: backend + web UI complete and running end to end.** The section 44
Definition-of-Done workflow passes as an automated test, and the Resume Studio
UI of section 21 is served by the API itself.

---

## Quickstart — reviewing this on a new machine

Needs only Docker Desktop installed and running. No Python, no Postgres, no
`pip install` on your machine at all.

```powershell
git clone https://github.com/Ritwik-Kumar-TSM/jd-resume-platform.git
cd jd-resume-platform
copy .env.example .env

docker compose --profile local-db up --build -d
docker compose exec api alembic upgrade head
docker compose exec api python scripts/seed.py
```

Then open **http://localhost:8000/app/** and log in with
`admin@example.com` / `admin123`.

That's the whole thing — one throwaway Postgres, Redis, the API and the
workers, all in containers, none of it touching anything else on the machine.
Shut it down with:

```powershell
docker compose --profile local-db down
```

---

## Starting it

There is **one command**. The React frontend is served by FastAPI from
`app/static/`, so backend and frontend start together on a single port and talk
to each other same-origin — no second process, no npm install, no CORS.

```powershell
# from the project root, in a fresh terminal
.venv\Scripts\activate
uvicorn app.main:app --reload
```

| URL | What it is |
| --- | --- |
| http://localhost:8000/app/ | **Resume Studio** — the web UI, opening on the Dashboard |
| http://localhost:8000/docs | Swagger, all endpoints |
| http://localhost:8000/health | DB / LLM / Celery status |
| http://localhost:8000/ | redirects to `/app/` |

Log in with `admin@example.com` / `admin123`.

### First-time setup

```powershell
.venv\Scripts\activate
pip install -e ".[dev]"
alembic upgrade head        # create the schema
```

> **Do not run `python scripts/seed.py` on a live database.** It inserts demo
> people, companies and vendors (Rahul Giri, Company A, ABC Recruitment…). It is
> only for a throwaway environment; the test suite runs it against its own
> temporary SQLite file. Running it against real data mixes fake profiles into
> the bank.

The database currently holds real data only: the technology master (with
first-release dates that drive the section 25 timeline checks) and the thirteen
built-in resume layouts. Add your own people via
**Knowledge base → People → Import from resume**, and your own vendors via
**Knowledge base → Vendors → New Vendor**.

### Optional: Celery + Redis

Reservation expiry and document rendering run in-process by default. Point
`CELERY_BROKER_URL` at a running Redis and they move to Celery; if Redis is
down the app silently falls back, so these two are never required.

```powershell
docker compose up -d redis

# separate terminals, both with the venv activated
celery -A app.celery_worker.celery_app worker --loglevel=info --pool=solo
celery -A app.celery_worker.celery_app beat   --loglevel=info
```

`/health` reports which path is active.

### Everything in Docker

```bash
docker compose up --build      # redis + api + worker + beat
```

The `db` service is **not** started by this. If you already have Postgres
installed on the host (the usual case day to day), it has real data in it,
sitting on the same default port 5432 this container would also try to bind —
starting both is a port clash at best and two silently diverging databases at
worst. So `api`/`worker`/`beat` talk to your existing local Postgres by
default instead, via `host.docker.internal` — set `DOCKER_DATABASE_URL` in
your own `.env` to that (see `.env.example`).

Want a fully throwaway, isolated stack instead — no local Postgres involved
at all? This is the path the Quickstart above uses.

```bash
docker compose --profile local-db up --build -d   # db + redis + api + worker + beat
docker compose exec api alembic upgrade head       # first time only — fresh db, no schema yet
docker compose exec api python scripts/seed.py     # first time only
```

---

Run the end-to-end test:

```bash
LLM_OFFLINE_MODE=true pytest -q
```

### Offline mode

`LLM_OFFLINE_MODE=true` swaps the LLM for a deterministic rule-based stub so the
entire workflow — JD parsing, generation, chat editing, validation, DOCX/PDF —
runs with no API key and no network. Set it to `false` and add
`ANTHROPIC_API_KEY` (or `OPENAI_API_KEY` / `GROQ_API_KEY`) for real generation.

### PostgreSQL

```bash
docker compose up -d db
# set DATABASE_URL in .env, then:
alembic upgrade head
python scripts/seed.py
```

---

## The workflow, as endpoints

| Step (section 44) | Endpoint |
|---|---|
| Paste JD, preview the parse | `POST /api/v1/requirements/parse-preview` |
| Store the requirement | `POST /api/v1/requirements` |
| Correct the parse | `PATCH /api/v1/requirements/{id}` |
| See available profiles | `GET /api/v1/availability/requirements/{id}/profiles` |
| Select profile → reserve | `POST /api/v1/resume-sessions` |
| Base resume generated | `POST /api/v1/resume-sessions/{id}/generate` |
| Live preview | `GET /api/v1/resume-sessions/{id}/preview` |
| Chat with the AI | `POST /api/v1/resume-sessions/{id}/chat` |
| Change projects/companies | `PUT /api/v1/resume-sessions/{id}/selections` |
| Direct manual edit | `PUT /api/v1/resume-sessions/{id}/content` |
| Validation warnings | `GET /api/v1/resume-sessions/{id}/warnings` |
| Keep / modify a warning | `POST .../warnings/{id}/decision` |
| Approve | `POST /api/v1/resume-sessions/{id}/approve` |
| Download DOCX/PDF | `GET /api/v1/files/{id}/download` |
| Version history | `GET /api/v1/resume-sessions/{id}/versions` |
| The content window | `GET /api/v1/resume-sessions/{id}/sections` |
| Add / remove / rename / reorder a section | `POST /api/v1/resume-sessions/{id}/sections` |
| A template's own sections | `GET /api/v1/resume-templates/{id}/blueprint` |
| Upload a template project | `POST /api/v1/resume-templates/upload` |
| Re-convert an uploaded design | `POST /api/v1/resume-templates/{id}/retemplatize` |
| Filter the history by any column | `GET /api/v1/resume-sessions?person=…&vendor=…&status=…` |
| Submission recorded | `GET /api/v1/submissions` |
| Vendors, with a count per state | `GET /api/v1/vendor-tracking` |
| Everything needing attention, all vendors | `GET /api/v1/vendor-tracking/profiles` |
| Profiles put forward to one vendor | `GET /api/v1/vendor-tracking/{vendor_id}/profiles` |
| Engage / release / reject a pair | `POST /api/v1/vendor-tracking/{vendor_id}/profiles/{person_id}/status` |
| Availability updated | `GET /api/v1/availability/persons/{pid}/vendors/{vid}` |
| The landing screen, in one request | `GET /api/v1/dashboard/summary` |

---

## Templates and the content window

A resume is **content** plus a **design**, and the two are kept apart.

The design's sections — its own headings, in its own order — are read once and
become the **content window**: the section list the studio edits and the
renderer fills. Removing Education, renaming "Key Projects" to "Selected
Work", adding a Publications section the design never had, reordering the
page: each is one edit to that list, it becomes a new version, and the next
render reads it. The HTML preview, the compiled PDF and the Word download all
read the same list, so they cannot disagree about what the resume contains.

### The shipped library

Thirteen designs live in `app/resume_templates/library/`, written by hand
against that one context — classic and modern, one column and two, banner,
sidebar, timeline, boxed panels, academic, executive, and several deliberately
plain ATS layouts. They fit the data by construction rather than by inference,
so rendering one is a Jinja fill and a compile: about a second, no model call,
and byte-identical for the same content every time.

All thirteen are base templates: `scripts/seed.py` installs them on a fresh
clone and every one is switched on, so a new checkout offers the full set
rather than a subset somebody once shortened. They differ in *shape* as well
as styling — some nest each project under the employer it was built for
instead of printing a separate Key Projects section, and some trim to summary,
skills and experience, leaving education and certifications in the content
window's "unused" list until they are wanted.

    python scripts/seed_template_library.py            # install or refresh
    python scripts/seed_template_library.py --verify    # and compile each one

`tests/test_template_library.py` renders and compiles every design against
four resumes — one of everything, one with sections removed, one with
sections added and reordered, and one nearly empty — so a design that only
works for the easy case never reaches an operator.

### Uploading your own

`POST /resume-templates/upload` takes a whole project: the main `.tex` plus
the `.cls`, `.sty` and image files it needs, as several files or one `.zip`.
They are kept together and compiled together, the way Overleaf compiles them.
At upload the design's sections are read and the design is converted once into
a fillable Jinja template, verified by rendering a full sample resume through
it and compiling that to a PDF.

Two things about uploaded designs are worth knowing, because both were
measured rather than assumed:

* **The engine here is XeTeX (tectonic).** A design written for pdfLaTeX that
  uses `\input{glyphtounicode}` is stripped of those pdfTeX-only lines — they
  affect how text copies out of the PDF, never the page. A design asking for a
  system font by name (`\setmainfont{TeX Gyre Heros}`) is retried without the
  font selection, so it prints in the default face rather than not at all.
* **The conversion needs a real model reply.** A reasoning model spends its
  budget thinking before it writes anything, so the request is made with room
  to finish and retried larger if it comes back empty. If it still fails the
  template is kept with its sections read, marked "not converted", and
  rendered through the older per-resume path; **Convert** in the Templates
  screen (or `POST /resume-templates/{id}/retemplatize`) asks again.

One folder is one template. A folder holding `main.tex`, `main (1).tex` and
`main (2).tex` is several templates that collided when they were downloaded —
each document then loads another one's `.cls` — and the upload says so instead
of failing later with an undefined-macro error.

---

## Layout

```
app/
  config.py            env-driven constants
  database.py          engine, SessionLocal, Base
  model_registry.py    every model module (Alembic reads this)
  common/enums/        the state machines from sections 8, 32, 33
  models/              30 tables — all 22 entities of section 39
  schemas/             Pydantic DTOs
    resume_content.py  THE structured resume — single source of truth
  crud/                generic helpers
  api/                 one router per module in section 38
  services/
    jd_parser.py       section 5/14
    availability.py    sections 7-9, 29-31
    resume_composer.py section 19
    resume_tools.py    section 22 — the 13 agent edit tools
    validation_engine.py sections 24/25
    resume_session_service.py  orchestration
    rendering/         layout.py + html/docx/pdf renderers
    llm/               provider abstraction + offline stub
  workers/             reservation expiry sweeper
scripts/seed.py        demo data from section 43
tests/                 end-to-end Definition of Done
```

---

## Five decisions worth knowing before you extend this

**1. `ResumeVersion.content_json` is a structured document, not text.**
`app/schemas/resume_content.py` defines it. The HTML preview, the DOCX and the
PDF are all projections of that one object, so they cannot drift apart. Storing
structure is also what makes "move AWS higher in the skills" a list reorder
rather than a re-roll of the whole document.

**2. The chatbot edits through tools, never by rewriting.**
`app/services/resume_tools.py` has 13 functions; the model picks which one and
with what arguments. Adding a capability to the chatbot means adding a function
and a JSON schema entry there — nothing else changes.

**3. Templates are data.**
A template is a JSON `layout_definition` row. Adding one is an `INSERT`
(`POST /api/v1/resume-templates`) — no schema change, no code change, exactly as
section 26 requires. See `app/services/rendering/layout.py`.

**4. Availability is keyed on (person, vendor).**
The unique constraint on `profile_vendor_statuses` is the whole model. Every
availability read and write is vendor-scoped. The seed data makes this visible:
Neha Sharma is ENGAGED with ABC Recruitment and AVAILABLE to XYZ at the same time.

The lifecycle a pair walks, and who moves it:

```
AVAILABLE ──select──▶ EDITING ──approve──▶ RESERVED ──┬──▶ SUBMITTED
                       ▲                              ├──▶ ENGAGED
                       │                              ├──▶ RELEASED
                       │                              ├──▶ REJECTED
                       └──── edit it again ───────────┴──(clock)──▶ AVAILABLE
```

**These seven words are the only statuses in the platform.** The composition
session, the submission and the (person, vendor) pair all speak them, so History
and Vendor tracking cannot describe one situation two ways. Sessions used to have
DRAFT/GENERATING/GENERATED/APPROVED/ABANDONED and submissions had
UNDER_REVIEW/INTERVIEW/SELECTED, mapped between by a lookup table; that table was
one more thing to keep true, and it drifted.

Only the four on the right of RESERVED are set by a human — EDITING and RESERVED
follow from composing and approving a resume. Setting one moves all three
records together (`vendor_tracking.set_pair_status`).

It is a loop, not a funnel. A hold running out is not a verdict on the work, so
editing a lapsed session **takes the hold again** rather than refusing: the
session and the profile both go back to EDITING with a fresh clock, and the
cycle can run as many times as it needs to. The one thing that still refuses is
a genuine collision — the person was picked up for a different requirement with
that vendor while this session was lapsed.

The session status mirrors the hold, so the History tab and Vendor tracking
never disagree: `EDITING` while a resume exists unapproved, `APPROVED` while the
vendor has it, `EXPIRED` when the hold lapses.

Only the three on the right are typed by a human. EDITING and RESERVED follow
from composing and approving a resume; EXPIRED is what an untended hold becomes.
The **Vendor tracking** tab is built around this. Its default view is *all*
vendors, not one: at fifty agencies the question is "what needs attention?", and
that answer spans them — so vendor is a column and the state counts across the
top are the filter. Picking one vendor narrows a view that was already useful,
rather than being the only way in.

Both hold phases carry the same clock — `RESERVATION_DURATION_MINUTES` — for
the edit lock and for the wait on a vendor's answer alike, so nobody is
shelved forever by an abandoned tab or a submission everyone forgot; a vendor
still deciding gets more time through an explicit extend, not a long default.
EXPIRED,
REJECTED and RELEASED are all selectable again; they are kept apart because
"this lapsed" and "they said no" are different facts about a vendor.

**5. Every resume is one page, composed for one JD.**
A person record is an identity — name, email, phone. No stored job title, no
stored years of experience: the resume is built for the JD in front of it out of
the shared bank, so the only experience figure that means anything is the one
that JD asks for. The shape is fixed at four to five projects and four to five
employers, the ones that answer the JD best, and it is enforced in three places:
what `resume_composer.autoselect_for_session` proposes, what
`PUT /resume-sessions/{id}/selections` accepts, and what
`ground_rules.fit_one_page` lets through on every generation and rebuild. The
counts live in `app/config/config.py` as `RESUME_MIN/MAX_PROJECTS` and
`RESUME_MIN/MAX_COMPANIES`.

---

## What is not built yet

- Frontend (section 21's split-screen studio) — next phase
- Semantic/embedding search — section 34 says "where useful"; only structured
  filtering is implemented, and section 42 keeps embeddings out of MVP
- RBAC — section 35 explicitly defers it
- Email/ATS/CRM ingestion — section 36 puts these out of scope

See `OPEN_QUESTIONS.md` for the decisions that need your team lead's sign-off
before this goes near real candidate data.
