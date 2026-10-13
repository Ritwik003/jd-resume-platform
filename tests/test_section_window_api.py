"""The content window over HTTP, on a session composed against a real template.

Uploads a design, composes a resume in it, and then does what the studio does:
hide a section, rename one, add one the design has no block for, reorder them.
Every change has to become a version and has to be visible in the window the
next time it is read — that is the contract the left-hand pane relies on.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("LLM_OFFLINE_MODE", "true")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

DESIGN = Path(__file__).resolve().parent.parent / "app" / "resume_templates" / "default_resume.tex"


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="module")
def auth(client: TestClient) -> dict:
    resp = client.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": "admin123"})
    assert resp.status_code == 200, "run `python scripts/seed.py` first"
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def template(client: TestClient, auth: dict) -> dict:
    resp = client.post(
        "/api/v1/resume-templates/upload",
        headers=auth,
        data={"name": "Window Test Design", "code": "WINDOW_01"},
        files=[("files", ("design.tex", DESIGN.read_text(encoding="utf-8"), "text/plain"))],
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["section_titles"], "the design's sections should have been read at upload"
    return body


@pytest.fixture(scope="module")
def session(client: TestClient, auth: dict, template: dict) -> dict:
    """A composed resume in that design, for whichever person is still free."""
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
        "/api/v1/requirements",
        headers=auth,
        json={
            "vendor_id": vendor["id"],
            "template_id": template["id"],
            "raw_jd_text": "Platform Engineer\nRequired:\nPython\nAWS\n",
        },
    ).json()
    created = client.post(
        "/api/v1/resume-sessions",
        headers=auth,
        json={"requirement_id": req["id"], "person_id": pick["person_id"], "template_id": template["id"]},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    client.post(f"/api/v1/resume-sessions/{body['id']}/generate", headers=auth)
    return body


def _window(client, auth, session) -> dict:
    resp = client.get(f"/api/v1/resume-sessions/{session['id']}/sections", headers=auth)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _edit(client, auth, session, payload) -> dict:
    resp = client.post(
        f"/api/v1/resume-sessions/{session['id']}/sections", headers=auth, json=payload
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_the_window_is_seeded_from_the_chosen_design(client, auth, session, template):
    window = _window(client, auth, session)
    assert window["template_name"] == "Window Test Design"
    assert [s["title"] for s in window["sections"]] == template["section_titles"]
    assert all("content_label" in s for s in window["sections"])


def test_hiding_a_section_versions_the_change_and_shows_in_the_window(client, auth, session):
    window = _window(client, auth, session)
    victim = window["sections"][-1]

    result = _edit(client, auth, session, {"action": "hide", "section": victim["key"]})
    assert victim["title"] in result["message"]
    assert result["version_no"] >= 2

    after = {s["key"]: s for s in result["window"]["sections"]}
    assert after[victim["key"]]["enabled"] is False
    # And it is still there to be put back, with its content.
    assert after[victim["key"]]["content_label"] == victim["content_label"]

    restored = _edit(client, auth, session, {"action": "show", "section": victim["key"]})
    assert {s["key"]: s for s in restored["window"]["sections"]}[victim["key"]]["enabled"] is True


def test_a_section_can_be_renamed_and_reordered(client, auth, session):
    window = _window(client, auth, session)
    first, second = window["sections"][0], window["sections"][1]

    renamed = _edit(
        client, auth, session, {"action": "rename", "section": first["key"], "title": "Overview"}
    )
    assert renamed["window"]["sections"][0]["title"] == "Overview"
    # The heading changed; what fills it did not.
    assert renamed["window"]["sections"][0]["kind"] == first["kind"]

    moved = _edit(client, auth, session, {"action": "reorder", "order": [second["key"]]})
    assert moved["window"]["sections"][0]["key"] == second["key"]


def test_a_section_the_design_lacks_can_be_added_and_removed(client, auth, session):
    added = _edit(
        client,
        auth,
        session,
        {"action": "add", "title": "Publications", "items": ["A paper, 2024"]},
    )
    row = next(s for s in added["window"]["sections"] if s["title"] == "Publications")
    assert row["added"] is True and row["enabled"] is True and row["empty"] is False

    removed = _edit(client, auth, session, {"action": "remove", "section": "Publications", "purge": True})
    assert all(s["title"] != "Publications" for s in removed["window"]["sections"])


def test_an_impossible_change_is_refused_rather_than_silently_ignored(client, auth, session):
    resp = client.post(
        f"/api/v1/resume-sessions/{session['id']}/sections",
        headers=auth,
        json={"action": "remove", "section": "Nonexistent Section"},
    )
    assert resp.status_code == 422, resp.text

    resp = client.post(
        f"/api/v1/resume-sessions/{session['id']}/sections",
        headers=auth,
        json={"action": "explode", "section": "summary"},
    )
    assert resp.status_code == 422, resp.text
