"""Section 44 Definition of Done, exercised end to end in offline LLM mode."""

from __future__ import annotations

import os

os.environ.setdefault("LLM_OFFLINE_MODE", "true")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import (  # noqa: E402
    RESUME_MAX_COMPANIES,
    RESUME_MAX_PAGES,
    RESUME_MAX_PROJECTS,
)
from app.main import app  # noqa: E402

JD = """GenAI Engineer
Experience: 5+ years
Required:
Python
LLM
RAG
LangChain
AWS
Preferred:
FastAPI
Docker
"""


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="module")
def token(client: TestClient) -> str:
    resp = client.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": "admin123"})
    assert resp.status_code == 200, "run `python scripts/seed.py` first"
    return resp.json()["access_token"]


@pytest.fixture(scope="module")
def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_full_workflow(client: TestClient, auth: dict) -> None:
    # --- paste JD, select vendor, store requirement -----------------------
    vendors = client.get("/api/v1/vendors", headers=auth).json()["items"]
    abc = next(v for v in vendors if v["name"] == "ABC Recruitment")

    req = client.post(
        "/api/v1/requirements",
        headers=auth,
        json={"vendor_id": abc["id"], "raw_jd_text": JD},
    ).json()
    assert req["parsed_role"] == "GenAI Engineer"
    assert req["min_experience_years"] == 5.0
    mandatory = {t["technology_name"] for t in req["technologies"] if t["importance"] == "MANDATORY"}
    assert {"Python", "LLM", "RAG", "AWS"} <= mandatory

    # --- see available profiles (vendor-scoped, unranked) -----------------
    rows = client.get(
        f"/api/v1/availability/requirements/{req['id']}/profiles?only_selectable=false",
        headers=auth,
    ).json()["items"]
    by_name = {r["person_name"]: r for r in rows}
    assert by_name["Neha Sharma"]["vendor_status"] == "ENGAGED"
    assert by_name["Neha Sharma"]["is_selectable"] is False
    assert by_name["Rahul Giri"]["is_selectable"] is True
    assert "score" not in by_name["Rahul Giri"]  # section 7: no ranking

    # The row answers "is this person free for this vendor" and nothing else.
    # A person holds no role and no skills, so a column for either could only
    # report some other JD's answer as though it were a fact about them.
    for absent in ("profile_title", "profile_type_name", "matched_skills",
                   "missing_mandatory_skills"):
        assert absent not in by_name["Rahul Giri"], absent

    rahul = by_name["Rahul Giri"]

    # --- select profile -> session created and person reserved ------------
    # No role is passed: selecting somebody for this JD is what puts them
    # forward as its role, so the API creates or reuses that row itself.
    session = client.post(
        "/api/v1/resume-sessions",
        headers=auth,
        json={"requirement_id": req["id"], "person_id": rahul["person_id"]},
    ).json()
    assert session["reservation_id"]
    assert session["profile_title"] == req["parsed_role"], (
        "the role on the session is the one the JD asked for"
    )

    # Selecting opens a composition, not a commitment (section 8).
    status = client.get(
        f"/api/v1/availability/persons/{rahul['person_id']}/vendors/{abc['id']}", headers=auth
    ).json()
    assert status["status"] == "EDITING"

    # the same person is untouched for the other vendor (section 8)
    xyz = next(v for v in vendors if v["name"] == "XYZ Recruitment")
    other = client.get(
        f"/api/v1/availability/persons/{rahul['person_id']}/vendors/{xyz['id']}", headers=auth
    ).json()
    assert other["status"] == "AVAILABLE"

    # --- base generation --------------------------------------------------
    v1 = client.post(f"/api/v1/resume-sessions/{session['id']}/generate", headers=auth).json()
    assert v1["version_no"] == 1
    assert v1["content"]["contact"]["full_name"] == "Rahul Giri"
    assert v1["content"]["projects"], "projects should be auto-selected"

    # One page, composed for this JD. The bank holds more of both than fits, so
    # this asserts the cap rather than the size of the seed data.
    assert 0 < len(v1["content"]["projects"]) <= RESUME_MAX_PROJECTS
    assert 0 < len(v1["content"]["experience"]) <= RESUME_MAX_COMPANIES
    assert v1["content"]["render_hints"]["max_pages"] == RESUME_MAX_PAGES

    # And the API refuses a selection that would not print, rather than
    # silently dropping the overflow after the operator saved it.
    overfull = client.put(
        f"/api/v1/resume-sessions/{session['id']}/selections",
        headers=auth,
        json={"projects": [
            {"title_override": f"Extra {i}", "display_order": i}
            for i in range(RESUME_MAX_PROJECTS + 2)
        ]},
    )
    assert overfull.status_code == 422, overfull.text

    # --- live preview -----------------------------------------------------
    preview = client.get(f"/api/v1/resume-sessions/{session['id']}/preview", headers=auth)
    assert preview.status_code == 200
    assert "Rahul Giri" in preview.text

    # --- chat edit creates a new version ----------------------------------
    chat = client.post(
        f"/api/v1/resume-sessions/{session['id']}/chat",
        headers=auth,
        json={"message": "Move AWS higher in the skills."},
    ).json()
    assert chat["version_no"] == 2
    first_group = chat["content"]["skill_groups"][0]
    assert first_group["items"][0] == "AWS"

    # --- §27/28 history filters, applied server-side ----------------------
    # Every column the history table prints can be narrowed, and `total`
    # counts matches rather than rows that happened to fit on the page. The
    # browser-side alternative looks identical until the history outgrows one
    # page and then quietly hides matches.
    def hist(**params):
        return client.get("/api/v1/resume-sessions", headers=auth,
                          params={"limit": 50, **params}).json()

    everything = hist()["meta"]["total"]
    assert everything >= 1

    by_ref = hist(reference_code=session["reference_code"])
    assert by_ref["meta"]["total"] == 1
    assert by_ref["items"][0]["id"] == session["id"]

    assert hist(person="Rahul")["meta"]["total"] >= 1
    assert hist(vendor="ABC")["meta"]["total"] >= 1
    assert hist(profile="GenAI")["meta"]["total"] >= 1
    assert hist(person="no-such-person")["meta"]["total"] == 0

    # Two columns narrow together rather than replacing one another.
    both = hist(person="Rahul", vendor="ABC")["meta"]["total"]
    assert 0 < both <= hist(person="Rahul")["meta"]["total"]

    # --- version history retained (section 27) ----------------------------
    versions = client.get(f"/api/v1/resume-sessions/{session['id']}/versions", headers=auth).json()
    assert [v["version_no"] for v in versions] == [1, 2]

    # --- restore rejects the AI change without deleting it ----------------
    # Restoring REPOINTS the session at the version asked for; it does not copy
    # it forward as a new one. Copying forward meant every undo produced a
    # version of its own, and a few rounds of trying-and-reverting buried the
    # versions carrying real work under rows reading "Restored from V3".
    restored = client.post(
        f"/api/v1/resume-sessions/{session['id']}/versions/{v1['id']}/restore", headers=auth
    ).json()
    assert restored["version_no"] == 1
    assert restored["id"] == v1["id"]

    after = client.get(
        f"/api/v1/resume-sessions/{session['id']}/versions", headers=auth
    ).json()
    # No third version, and the one that was current is still there to go back to.
    assert [v["version_no"] for v in after] == [1, 2]
    # The restore is recorded as a note on the version, which is the only trace
    # of it now — the studio prints it as one quiet line.
    assert any(v["restore_note"] for v in after), after

    session_now = client.get(f"/api/v1/resume-sessions/{session['id']}", headers=auth).json()
    assert session_now["current_version_no"] == 1

    # --- approve -> files + submission ------------------------------------
    # Approval runs a fresh verification pass and blocks on any undecided
    # warning it raises (section 25's decision requirement moved to the gate).
    # The pass itself can only raise something new on the FIRST attempt (the
    # content is not changing between retries), so one clear-and-retry is
    # always enough.
    for _ in range(2):
        for w in client.get(
            f"/api/v1/resume-sessions/{session['id']}/warnings?only_open=true", headers=auth
        ).json():
            client.post(
                f"/api/v1/resume-sessions/{session['id']}/warnings/{w['id']}/decision",
                headers=auth, json={"status": "KEPT", "note": "test"},
            )
        attempt = client.post(
            f"/api/v1/resume-sessions/{session['id']}/approve",
            headers=auth,
            json={"formats": ["TEX", "PDF", "DOCX"]},
        )
        if attempt.status_code != 409:
            break
    approved = attempt.json()
    formats = {f["file_format"] for f in approved["files"]}

    # Section 26 templates are LaTeX: the .tex is always composed, and the PDF
    # only exists where a LaTeX engine is installed. Asserting PDF
    # unconditionally would make this suite fail on any machine without one.
    from app.services.rendering import latex

    expected = {"TEX", "DOCX"} | ({"PDF"} if latex.available() else set())
    assert formats == expected

    # Approval commits the resume to this vendor and starts the clock on their
    # answer: RESERVED with a hold, not ENGAGED. Nobody has agreed anything yet.
    held = client.get(
        f"/api/v1/availability/persons/{rahul['person_id']}/vendors/{abc['id']}", headers=auth
    ).json()
    assert held["status"] == "RESERVED"
    assert held["reservation_expires_at"], "an approved resume holds the profile on a clock"
    assert all(f["size_bytes"] > 200 for f in approved["files"])
    tex = next(f for f in approved["files"] if f["file_format"] == "TEX")
    source = client.get(f"/api/v1/files/{tex['id']}/download", headers=auth).text
    assert "\\documentclass" in source
    assert source.rstrip().endswith("\\end{document}")

    # The preamble is COPIED from the template, never regenerated. This is the
    # property that makes an arbitrary uploaded .tex compile: a model asked to
    # retype five kilobytes of \usepackage and \newcommand eventually mistypes
    # one, and the compile dies on a line nobody wrote. Byte equality is the
    # only assertion that catches a regression back to retyping it.
    from app.config.database import SessionLocal
    from app.models.resume_template import ResumeTemplate
    from app.services import storage
    from app.services.latex_composer import split_document

    with SessionLocal() as db:
        template = db.query(ResumeTemplate).filter(ResumeTemplate.is_active.is_(True)).first()
        original = storage.absolute_path(template.source_storage_path).read_text(encoding="utf-8")

    template_parts = split_document(original)
    composed_parts = split_document(source)
    assert template_parts is not None and composed_parts is not None
    # Contact placeholders are the one thing substituted, so compare the lines
    # that define nothing about the candidate.
    original_structure = [
        line for line in template_parts.preamble.splitlines() if "\\def" not in line
    ]
    composed_structure = [
        line for line in composed_parts.preamble.splitlines() if "\\def" not in line
    ]
    assert composed_structure == original_structure, "the template preamble must survive verbatim"

    # --- download works ---------------------------------------------------
    docx = next(f for f in approved["files"] if f["file_format"] == "DOCX")
    download = client.get(f"/api/v1/files/{docx['id']}/download", headers=auth)
    assert download.status_code == 200
    assert download.content[:2] == b"PK"  # a real .docx zip container

    # --- mark submitted -> profile leaves ABC's pool only -----------------
    # One vocabulary: SUBMITTED means submitted, not "engaged, near enough".
    client.post(
        f"/api/v1/submissions/{approved['submission_id']}/status",
        headers=auth,
        json={"status": "SUBMITTED"},
    )
    after_abc = client.get(
        f"/api/v1/availability/persons/{rahul['person_id']}/vendors/{abc['id']}", headers=auth
    ).json()
    assert after_abc["status"] == "SUBMITTED"

    after_xyz = client.get(
        f"/api/v1/availability/persons/{rahul['person_id']}/vendors/{xyz['id']}", headers=auth
    ).json()
    assert after_xyz["status"] == "AVAILABLE", "section 8: other vendors are unaffected"

    # --- §29 vendor tracking: the vendor's side of the same facts ---------
    summary = client.get("/api/v1/vendor-tracking", headers=auth).json()
    abc_row = next(v for v in summary if v["vendor_id"] == abc["id"])
    assert abc_row["tracked_profiles"] >= 1
    assert abc_row["submitted"] >= 1

    # The cross-vendor view is the one that does not get worse as vendors are
    # added: same rows, attention first, vendor as a column rather than a
    # navigation step. It must carry the vendor on every row to be usable.
    everywhere = client.get("/api/v1/vendor-tracking/profiles", headers=auth).json()
    assert all(r["vendor_id"] and r["vendor_name"] for r in everywhere)
    assert any(r["vendor_id"] == abc["id"] for r in everywhere)
    lead = [r["status"] for r in everywhere]
    settled = {"RELEASED", "REJECTED", "AVAILABLE"}
    open_after_settled = [
        i for i, st in enumerate(lead)
        if st in ("EDITING", "RESERVED") and any(x in settled for x in lead[:i])
    ]
    assert not open_after_settled, "work needing attention must sort above settled rows"

    tracked = client.get(f"/api/v1/vendor-tracking/{abc['id']}/profiles", headers=auth).json()
    mine = next(t for t in tracked if t["person_id"] == rahul["person_id"])
    assert mine["status"] == "SUBMITTED"
    assert mine["role"] == "GenAI Engineer", "the row says what they were put forward for"
    assert mine["session_reference"] and mine["submission_reference"]
    assert mine["approved_resumes"] >= 1

    # A status the clock or the resume owns cannot be typed in by hand.
    refused = client.post(
        f"/api/v1/vendor-tracking/{abc['id']}/profiles/{rahul['person_id']}/status",
        headers=auth,
        json={"status": "EXPIRED"},
    )
    assert refused.status_code == 422, refused.text

    # RELEASED only follows ENGAGED — you can only let go of somebody who was
    # actually taken, so a SUBMITTED profile cannot jump straight to RELEASED.
    too_soon = client.post(
        f"/api/v1/vendor-tracking/{abc['id']}/profiles/{rahul['person_id']}/status",
        headers=auth,
        json={"status": "RELEASED"},
    )
    assert too_soon.status_code == 409, too_soon.text

    # The vendor takes the person first...
    engaged = client.post(
        f"/api/v1/vendor-tracking/{abc['id']}/profiles/{rahul['person_id']}/status",
        headers=auth,
        json={"status": "ENGAGED"},
    ).json()
    assert next(t for t in engaged if t["person_id"] == rahul["person_id"])["status"] == "ENGAGED"

    # ...and only now may the operator release them.
    released = client.post(
        f"/api/v1/vendor-tracking/{abc['id']}/profiles/{rahul['person_id']}/status",
        headers=auth,
        json={"status": "RELEASED", "notes": "Engagement wrapped up"},
    ).json()
    assert next(t for t in released if t["person_id"] == rahul["person_id"])["status"] == "RELEASED"

    # --- RELEASED is a dead end (unlike REJECTED — section 30) ------------
    # A released profile never becomes selectable for this vendor again, and
    # no further hand-set status may follow it either.
    rows = client.get(
        f"/api/v1/availability/vendors/{abc['id']}/profiles?only_selectable=true", headers=auth
    ).json()["items"]
    assert "Rahul Giri" not in {r["person_name"] for r in rows}, \
        "a released profile must never return to this vendor's pool"

    dead_end = client.post(
        f"/api/v1/vendor-tracking/{abc['id']}/profiles/{rahul['person_id']}/status",
        headers=auth,
        json={"status": "REJECTED"},
    )
    assert dead_end.status_code == 409, dead_end.text


def test_validation_reports_only_checkable_facts(client: TestClient, auth: dict) -> None:
    """Section 25 — warn on facts, never block, and never on style.

    A misspelled technology is a fact a reader can check, so it is flagged. The
    older opinion-shaped rules (anachronistic wording, a project shown under
    another company, a skill the stored profile does not list) are deliberately
    silent: they fired on every generated resume and taught people to click
    through the warnings that mattered.
    """
    vendors = client.get("/api/v1/vendors", headers=auth).json()["items"]
    vendor = next(v for v in vendors if v["name"] == "Talent Provider")
    req = client.post(
        "/api/v1/requirements", headers=auth,
        json={"vendor_id": vendor["id"], "raw_jd_text": "Backend Engineer\nRequired:\nPython\n"},
    ).json()

    people = client.get("/api/v1/profiles?search=Amit", headers=auth).json()["items"]
    amit = people[0]
    profile = client.get(f"/api/v1/profiles/{amit['id']}/profiles", headers=auth).json()[0]

    session = client.post(
        "/api/v1/resume-sessions", headers=auth,
        json={"requirement_id": req["id"], "person_id": amit["id"], "person_profile_id": profile["id"]},
    ).json()
    version = client.post(f"/api/v1/resume-sessions/{session['id']}/generate", headers=auth).json()

    content = version["content"]
    content["skill_groups"] = [
        {"label": "Backend", "tier": "PRIMARY", "items": ["Kubernets", "Pythonn"]}
    ]
    content["projects"][0]["description"] = "Built an LLM agent in 2019 for reconciliation."
    edited = client.put(
        f"/api/v1/resume-sessions/{session['id']}/content", headers=auth,
        json={"content": content, "change_summary": "test claim"},
    ).json()
    assert edited["version_no"] == version["version_no"] + 1

    warnings = client.get(
        f"/api/v1/resume-sessions/{session['id']}/warnings?only_open=true", headers=auth
    ).json()
    kinds = {w["warning_type"] for w in warnings}

    assert "UNKNOWN_TECHNOLOGY" in kinds, "a misspelled technology is a checkable fact"
    assert "ANACHRONISTIC_TERMINOLOGY" not in kinds, "wording is not a fact"
    assert "TECHNOLOGY_NOT_IN_PROFILE" not in kinds, "moulding legitimately adds JD skills"
    assert "COMPANY_REATTRIBUTED" not in kinds, "mix-and-match is the feature, not a defect"

    flagged = {w["claim_text"] for w in warnings if w["warning_type"] == "UNKNOWN_TECHNOLOGY"}
    assert "Kubernets" in flagged

    # The user decides what a warning means (section 25). Deciding is not a
    # precondition for approval — the decision stays with the user, who
    # composed this resume by hand and owns the facts on it, so approval does
    # not hard-block on an open warning.
    warning = next(w for w in warnings if w["warning_type"] == "UNKNOWN_TECHNOLOGY")
    decided = client.post(
        f"/api/v1/resume-sessions/{session['id']}/warnings/{warning['id']}/decision",
        headers=auth, json={"status": "KEPT", "note": "Confirmed with the candidate"},
    ).json()
    assert decided["status"] == "KEPT"

    approved = client.post(f"/api/v1/resume-sessions/{session['id']}/approve", headers=auth, json={}).json()
    assert approved["submission_id"]


def test_composition_accepts_a_project_with_no_bank_row(client: TestClient, auth: dict) -> None:
    """Section 17 mix-and-match, through HTTP and the database.

    A row whose ``project_id`` is null is a project the operator composed in the
    studio. It has to survive the round trip — the column is nullable, the
    payload validator lets it through, and the rebuild renders it.
    """
    # Whichever vendor/person pair is still free: the other tests reserve and
    # engage people as they go, and section 8 rightly refuses a second
    # reservation for the same pair.
    vendors = client.get("/api/v1/vendors", headers=auth).json()["items"]
    vendor = pick = None
    for candidate in vendors:
        rows = client.get(
            f"/api/v1/availability/vendors/{candidate['id']}/profiles?only_selectable=true",
            headers=auth,
        ).json()["items"]
        if rows:
            vendor, pick = candidate, rows[0]
            break
    assert pick is not None, "no selectable profile left to compose with"

    req = client.post(
        "/api/v1/requirements", headers=auth,
        json={"vendor_id": vendor["id"], "raw_jd_text": "DevOps Engineer\nRequired:\nKubernetes\n"},
    ).json()

    created = client.post(
        "/api/v1/resume-sessions", headers=auth,
        json={"requirement_id": req["id"], "person_id": pick["person_id"]},
    )
    assert created.status_code == 201, created.text
    session = created.json()
    client.post(f"/api/v1/resume-sessions/{session['id']}/generate", headers=auth)

    updated = client.put(
        f"/api/v1/resume-sessions/{session['id']}/selections", headers=auth,
        json={"projects": [{
            "project_id": None,
            "display_order": 0,
            "is_primary": True,
            "title_override": "Kubernetes Platform Migration",
            "role_override": "DevOps Engineer",
            "description_override": "Migrated a monolith onto a managed Kubernetes cluster.",
            "technologies_override": ["Kubernetes", "Docker"],
        }]},
    )
    assert updated.status_code == 200, updated.text
    rows = updated.json()["selected_projects"]
    assert len(rows) == 1
    assert rows[0]["project_id"] is None
    assert rows[0]["is_composed"] is True
    assert rows[0]["project_name"] == "Kubernetes Platform Migration"

    rebuilt = client.post(f"/api/v1/resume-sessions/{session['id']}/rebuild", headers=auth, json={})
    assert rebuilt.status_code == 200, rebuilt.text
    projects = rebuilt.json()["content"]["projects"]
    composed = next(p for p in projects if p["name"] == "Kubernetes Platform Migration")
    assert composed["technologies"] == ["Kubernetes", "Docker"]


def test_a_composed_project_needs_a_title(client: TestClient, auth: dict) -> None:
    """A row with neither a bank project nor a name would render as blank."""
    sessions = client.get("/api/v1/resume-sessions", headers=auth).json()["items"]
    assert sessions, "the earlier tests create some"
    rejected = client.put(
        f"/api/v1/resume-sessions/{sessions[0]['id']}/selections", headers=auth,
        json={"projects": [{"project_id": None, "display_order": 0}]},
    )
    assert rejected.status_code == 422
