"""The timeline rules: nothing may be claimed before it existed.

These are the checks a reader can make themselves — LangGraph shipped in 2023,
so a 2019 job cannot have used it — which is exactly why they have to hold.
"""

from __future__ import annotations

import os

os.environ.setdefault("LLM_OFFLINE_MODE", "true")

from app.services import era  # noqa: E402
from app.services import role_timeline as rt  # noqa: E402


def _timeline(**over):
    """A timeline shaped like one the resolver returns for an AI Engineer JD."""
    base = {
        "role": "AI Engineer (Azure)",
        "discipline": "AI Engineer",
        "emerged": 2023,
        "ladder": [
            rt.Rung("Software Engineer", 1990),
            rt.Rung("Backend Engineer", 2005),
            rt.Rung("Machine Learning Engineer", 2016),
        ],
        "seniority": ["{role}", "{role} II", "Senior {role}", "Lead {role}"],
        "resolved": True,
    }
    base.update(over)
    return rt.Timeline(**base)


def test_a_title_cannot_predate_the_job() -> None:
    """A 7-year AI Engineer JD reaches back years before that title existed."""
    timeline = _timeline()
    assert timeline.title_for(2019) == "Machine Learning Engineer"
    assert timeline.title_for(2012) == "Backend Engineer"
    assert timeline.title_for(2001) == "Software Engineer"

    # Once the title exists it is used unchanged — the point of moulding.
    assert timeline.title_for(2023) == "AI Engineer"
    assert timeline.title_for(2025) == "AI Engineer"


def test_the_jds_stack_qualifier_never_becomes_a_job_title() -> None:
    """A JD says "AI Engineer (Azure)"; nobody's 2019 job was called that.

    The qualifier is positioning — right under the candidate's name, wrong on
    an employment row, and it makes a seniority numeral read as nonsense
    besides ("AI Engineer (Azure) II"). Separating the two is the resolver's
    first job, so every title it hands back is the discipline.
    """
    timeline = _timeline()
    assert timeline.title_for(2024) == "AI Engineer"
    assert "(" not in " ".join(timeline.climb(4, 4))


def test_the_ladder_climbs_and_repeats_only_on_the_oldest_jobs() -> None:
    """Two consecutive employers with one title read as no career at all.

    When there are more employers than rungs the repeat has to land on the
    OLDEST jobs, where holding a title two years is ordinary — never on the
    two most recent, which is the first place a reader looks.
    """
    titles = _timeline().climb(4, 3)  # oldest first
    assert titles[-1] != titles[-2], titles
    assert titles == ["AI Engineer", "AI Engineer", "AI Engineer II", "Senior AI Engineer"]

    assert _timeline().climb(4, 4) == [
        "AI Engineer",
        "AI Engineer II",
        "Senior AI Engineer",
        "Lead AI Engineer",
    ]


def test_the_jds_stated_years_cap_the_climb() -> None:
    """Three years asked for does not open the resume on a Lead title."""
    assert rt.ceiling_for(2) == 1
    assert rt.ceiling_for(4) == 2
    assert rt.ceiling_for(7) == 3
    assert rt.ceiling_for(12) == 4
    assert _timeline().climb(2, rt.ceiling_for(2)) == ["AI Engineer", "AI Engineer"]


def test_an_unresolved_role_declines_to_demote() -> None:
    """No table, no guess: an undated role prints what the advert asked for.

    This is the direction that fails safely. Failing to step a title back is
    ordinary; stepping it back to a title the discipline never used would be a
    new kind of wrong, invented by the platform.
    """
    unknown = rt.Timeline(role="Quantum Ops Lead", discipline="Quantum Ops Lead")
    assert unknown.title_for(1999) == "Quantum Ops Lead"
    assert unknown.climb(3, 4) == ["Quantum Ops Lead"] * 3


def test_one_row_per_role_however_the_advert_punctuates_it() -> None:
    assert rt.normalise("AI Engineer (Azure)") == rt.normalise("ai engineer(azure)")
    assert rt.normalise("  Senior  Data   Engineer ") == "senior data engineer"
    assert rt.normalise(None) == ""


def test_an_unusable_reply_is_refused_rather_than_half_used() -> None:
    """A malformed timeline decides what a resume may claim about a past.

    Half-parsing one is worse than rejecting it: the caller's unresolved path
    prints the advert's role, while a mangled ladder invents a career.
    """
    assert rt._parse("not json at all", "AI Engineer") is None
    assert rt._parse('{"discipline": ""}', "AI Engineer") is None
    assert rt._parse('["a", "list"]', "AI Engineer") is None

    good = rt._parse(
        '{"discipline": "Data Engineer", "emerged": 2014,'
        ' "ladder": [{"title": "ETL Developer", "emerged": 2000},'
        '            {"title": "Software Engineer", "emerged": 1990}],'
        ' "seniority": ["{role}", "Senior {role}", "not a template"]}',
        "Data Engineer - GCP",
    )
    assert good is not None and good.resolved
    # Sorted oldest first whatever order the model returned them in.
    assert [r.title for r in good.ladder] == ["Software Engineer", "ETL Developer"]
    # A rung with no {role} placeholder would print one title repeatedly.
    assert good.seniority == ["{role}", "Senior {role}"]
    assert good.title_for(2005) == "ETL Developer"


def test_a_rung_the_model_left_undated_is_dropped() -> None:
    """An undated rung cannot answer "was this title available in 2019"."""
    parsed = rt._parse(
        '{"discipline": "AI Engineer", "emerged": 2023,'
        ' "ladder": [{"title": "Backend Engineer"}, {"title": "ML Engineer", "emerged": 2016}],'
        ' "seniority": ["{role}"]}',
        "AI Engineer",
    )
    assert parsed is not None
    assert [r.title for r in parsed.ladder] == ["ML Engineer"]


def test_technology_named_before_its_release_is_found() -> None:
    releases = {"langgraph": 2023, "rag": 2020, "docker": 2013, "python": 1991}
    text = "Built RAG pipelines on LangGraph, containerised with Docker in Python."

    hits = {a.term.lower(): a.released for a in era.anachronisms_in(text, 2019, releases)}
    assert hits == {"rag": 2020, "langgraph": 2023}, "2019 predates RAG and LangGraph"

    assert era.anachronisms_in(text, 2024, releases) == [], "everything existed by 2024"


def test_scanning_does_not_fire_on_substrings() -> None:
    """'React' must not be found inside 'React Native', and a bare word that
    merely contains a technology name is not a claim about that technology."""
    releases = {"react": 2013, "react native": 2015, "go": 2009}
    found = {a.term.lower() for a in era.anachronisms_in("Built with React Native.", 2014, releases)}
    assert found == {"react native"}, "the longer, more specific name wins"

    # 'go' inside 'going' is not the language Go.
    assert era.anachronisms_in("The work was going well.", 2005, releases) == []


def test_a_period_is_judged_on_when_it_ended_not_when_it_started() -> None:
    """A job running 2023-2025 may legitimately use something released in 2024.

    Judging content on the start year would flag correct resumes, and these
    findings block approval — a false positive is not a harmless extra warning.
    """
    assert era.end_year_of("Feb 2023") == 2023
    assert era.start_year_of("Feb 2023") == 2023
    # "Present" has no year of its own and must not read as "no period".
    assert era.end_year_of("Present") >= 2026
    assert era.end_year_of(None) >= 2026


# ─────────────────────────────────────────────── template self-containment
def test_a_local_documentclass_counts_as_a_missing_dependency() -> None:
    r"""A template whose \documentclass names a local .cls is NOT
    self-contained, however complete it otherwise looks.

    Without this the file passes every check and then dies at compile time on
    "File `resume.cls' not found" — which is a demo failing in front of
    somebody. Detecting it routes the template to whole-document
    reconstruction, which works.
    """
    from app.services import latex_composer as lc

    local = r"\documentclass{resume}" "\n" r"\begin{document}X\end{document}"
    assert lc.missing_dependencies(local) == ["resume.cls"]
    assert not lc.is_self_contained(local)

    standard = r"\documentclass[10pt]{article}" "\n" r"\begin{document}X\end{document}"
    assert lc.missing_dependencies(standard) == []
    assert lc.is_self_contained(standard)


def test_the_shipped_templates_are_self_contained() -> None:
    """The four built-in designs must never need a file we do not ship."""
    from pathlib import Path

    from app.services import latex_composer as lc

    for code in ("modern_professional", "classic_serif", "technical_detailed", "faang_compact"):
        source = Path("storage/templates", f"{code}.tex").read_text(encoding="utf-8")
        assert lc.missing_dependencies(source) == [], code
        assert lc.is_self_contained(source), code


# ──────────────────────────────────────────── project -> employer pairing
def _content(projects, employers):
    from app.schemas.resume_content import ExperienceBlock, ProjectBlock, ResumeContent

    return ResumeContent(
        experience=[
            ExperienceBlock(source_company_id=cid, company=cid, start_label=s, end_label=e)
            for cid, s, e in employers
        ],
        projects=[ProjectBlock(name=n, technologies=t) for n, t in projects],
    )


def test_the_fallback_gives_every_project_an_employer() -> None:
    """Which project sits under which employer is the composer's call.

    This is only the fallback: it fills in anything the model did not assign,
    spreading evenly so no employer is left as a bare heading. Nothing in the
    bank is dated, so there is no constraint to satisfy here — only a gap.
    """
    from app.services.resume_composer import pair_projects_to_companies

    content = _content(
        projects=[(f"P{i}", ["Python"]) for i in range(5)],
        employers=[("a", None, None), ("b", None, None), ("c", None, None)],
    )
    pair_projects_to_companies(None, None, content)

    counts: dict[str, int] = {}
    for project in content.projects:
        assert project.source_company_id, f"{project.name} was left with no employer"
        counts[project.source_company_id] = counts.get(project.source_company_id, 0) + 1

    assert len(counts) == 3, f"every employer should hold something: {counts}"
    assert sorted(counts.values()) == [1, 2, 2], counts


def test_an_assignment_the_model_made_is_not_overwritten() -> None:
    """The split is the model's decision, and an uneven one is usually the
    better story — so the fallback must fill gaps without redistributing what
    has already been decided."""
    from app.services.resume_composer import pair_projects_to_companies

    content = _content(
        projects=[("kept", ["Python"]), ("kept2", ["Python"]), ("orphan", ["Python"])],
        employers=[("a", None, None), ("b", None, None)],
    )
    # Stand in for what _apply_mould wrote back from the model's reply: both
    # onto one employer, deliberately lopsided.
    for project in content.projects[:2]:
        project.company = "a"
        project.source_company_id = "a"

    pair_projects_to_companies(None, None, content)

    placed = {p.name: p.source_company_id for p in content.projects}
    assert placed["kept"] == "a" and placed["kept2"] == "a", "a decided split stands"
    assert placed["orphan"] == "b", "the gap is filled with the emptier employer"


# ──────────────────────────────────────────────── operator-added sections
def test_the_studio_can_add_and_remove_an_arbitrary_section() -> None:
    """A JD occasionally wants a section the schema has no field for —
    Publications, Patents, Clearance. Adding one affects this composition
    only, and it has to come back off the same way it went on."""
    from app.schemas.resume_content import ResumeContent
    from app.services.rendering import html_renderer
    from app.services.resume_tools import apply_tool

    content = ResumeContent()

    added = apply_tool(content, "add_section", {"title": "Publications", "items": ["Paper A"]})
    assert added.applied
    assert [s.title for s in content.custom_sections] == ["Publications"]

    # Same title again extends rather than printing the section twice.
    again = apply_tool(content, "add_section", {"title": "publications", "items": ["Paper B"]})
    assert again.applied
    assert len(content.custom_sections) == 1
    assert content.custom_sections[0].items == ["Paper A", "Paper B"]

    # An empty section is refused rather than printing a bare heading.
    assert not apply_tool(content, "add_section", {"title": "Awards"}).applied

    html = html_renderer.render_html(content)
    assert "PUBLICATIONS" in html.upper() and "Paper B" in html

    removed = apply_tool(content, "remove_section", {"section": "Publications"})
    assert removed.applied
    assert content.custom_sections == []


def test_a_custom_section_survives_a_content_round_trip() -> None:
    """Versions are stored as JSON, so a section added in the studio has to
    survive being written and read back or it silently vanishes on reload."""
    from app.schemas.resume_content import ResumeContent
    from app.services.resume_tools import apply_tool

    content = ResumeContent()
    apply_tool(content, "add_section", {"title": "Patents", "items": ["US1234567"]})

    restored = ResumeContent.model_validate(content.model_dump(mode="json"))
    assert [s.title for s in restored.custom_sections] == ["Patents"]
    assert restored.custom_sections[0].items == ["US1234567"]


def test_a_project_does_not_print_technology_its_period_predates() -> None:
    """The one place the era rules still touch the composition.

    Any project may sit under any employer — the split is the composer's call
    — but a project under a job dated 2018-2020 must not PRINT "LangGraph",
    which shipped in 2023. The prose is constrained by the timeline in the
    prompt; the technology list is copied from the bank and written by nobody,
    so it is filtered here instead.

    Nothing is invented and nothing is moved: the bank keeps every technology,
    this resume simply does not claim the ones its period cannot carry.
    """
    from app.services.resume_composer import strip_anachronistic_technologies

    releases = {"langgraph": 2023, "rag": 2020, "python": 1991}
    content = _content(
        projects=[("Old Work", ["Python", "RAG", "LangGraph"]),
                  ("Recent Work", ["Python", "LangGraph"])],
        # Ends 2019, so RAG (2020) and LangGraph (2023) are both too new. A job
        # running INTO 2020 could legitimately use RAG, which is why the bound
        # is the end year, not the start.
        employers=[("old", "Jan 2018", "Jan 2019"), ("new", "Jan 2024", "Present")],
    )
    content.projects[0].source_company_id = "old"
    content.projects[1].source_company_id = "new"

    notes = strip_anachronistic_technologies(content, releases)

    assert content.projects[0].technologies == ["Python"], "RAG and LangGraph postdate 2019"
    assert content.projects[1].technologies == ["Python", "LangGraph"], "2024 carries both"
    assert len(notes) == 1 and "Old Work" in notes[0]


def test_a_project_is_never_stripped_down_to_nothing() -> None:
    """If every technology a project names is too new for its period, leaving
    it bare would read as an omission. The era warning is the honest outcome
    there, so the list is left alone for the check to fire on."""
    from app.services.resume_composer import strip_anachronistic_technologies

    releases = {"langgraph": 2023}
    content = _content(
        projects=[("All Modern", ["LangGraph"])],
        employers=[("old", "Jan 2018", "Jan 2020")],
    )
    content.projects[0].source_company_id = "old"

    strip_anachronistic_technologies(content, releases)
    assert content.projects[0].technologies == ["LangGraph"], "left for the warning to catch"
