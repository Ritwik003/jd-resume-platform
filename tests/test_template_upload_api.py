"""Uploading a template project, over HTTP.

The unit tests prove the pipeline; this proves the endpoint an operator
actually uses — several files at once, the design's sections read from them,
and the project kept on disk so the .cls travels with the .tex to the
compiler.
"""

from __future__ import annotations

import io
import os
import zipfile

os.environ.setdefault("LLM_OFFLINE_MODE", "true")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.services import storage  # noqa: E402

MAIN = r"""\documentclass[11pt]{article}
\usepackage{acmecv}
\begin{document}
\cvsection{Profile}
  A backend engineer.
\input{parts/history}
\cvsection{Education}
  B.E., 2018
\end{document}
"""

HISTORY = r"""\cvsection{Work Experience}
  \begin{cvsubsection}{Acme}{2019 -- 2021}{Engineer}{Pune}\end{cvsubsection}
"""

STY = "\\ProvidesPackage{acmecv}\n"


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="module")
def auth(client: TestClient) -> dict:
    resp = client.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": "admin123"})
    assert resp.status_code == 200, "run `python scripts/seed.py` first"
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _upload(client, auth, code, files):
    return client.post(
        "/api/v1/resume-templates/upload",
        headers=auth,
        data={"name": f"Test {code}", "code": code},
        files=files,
    )


def test_a_multi_file_project_uploads_and_its_sections_are_read(client, auth):
    resp = _upload(
        client,
        auth,
        "MULTIFILE_01",
        [
            ("files", ("main.tex", MAIN, "text/plain")),
            ("files", ("parts/history.tex", HISTORY, "text/plain")),
            ("files", ("acmecv.sty", STY, "text/plain")),
        ],
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()

    # The design's own headings, in its own order — including the one that
    # only exists in an \input file.
    assert body["section_titles"] == ["Profile", "Work Experience", "Education"]
    assert body["main_tex_name"] == "main.tex"

    detail = client.get(f"/api/v1/resume-templates/{body['id']}/blueprint", headers=auth).json()
    kinds = {s["title"]: s["kind"] for s in detail["sections"]}
    assert kinds == {"Profile": "summary", "Work Experience": "experience", "Education": "education"}

    # The whole project is on disk, so the .sty compiles beside the .tex.
    project = storage.absolute_path(f"templates/{body['code'].lower()}")
    assert (project / "acmecv.sty").is_file()
    assert (project / "parts" / "history.tex").is_file()


def test_a_zip_uploads_the_same_way(client, auth):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("cv/main.tex", MAIN)
        archive.writestr("cv/parts/history.tex", HISTORY)
        archive.writestr("cv/acmecv.sty", STY)
    resp = _upload(client, auth, "ZIPPED_01", [("files", ("cv.zip", buffer.getvalue(), "application/zip"))])
    assert resp.status_code == 201, resp.text
    assert resp.json()["section_titles"] == ["Profile", "Work Experience", "Education"]


def test_a_project_with_no_main_document_is_refused(client, auth):
    resp = _upload(
        client, auth, "NOMAIN_01", [("files", ("part.tex", "\\cvsection{Skills}\n", "text/plain"))]
    )
    assert resp.status_code == 422, resp.text
    assert "documentclass" in resp.text.lower()
    # Nothing is left behind for a template that was refused.
    assert not storage.absolute_path("templates/nomain_01").exists()


def test_uploading_nothing_says_so(client, auth):
    resp = client.post(
        "/api/v1/resume-templates/upload", headers=auth, data={"name": "Empty", "code": "EMPTY_01"}
    )
    assert resp.status_code in (400, 422)
