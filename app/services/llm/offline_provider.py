"""Deterministic offline provider.

Set ``LLM_OFFLINE_MODE=true`` to exercise the entire workflow — JD parsing, base
generation, chat editing, validation — with no API key and no network. Used by
the test suite and by the seeded demo so a reviewer can run the project cold.

It is a rule-based stand-in, not a model: it recognises the shapes of requests
the platform makes and returns well-formed responses.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.services.llm.base import BaseLLMProvider, LLMResponse

_SKILL_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+#./ -]{1,30}")


#: The user prompt wraps the JD in an instruction line; drop it before parsing so
#: the stub reads the JD itself rather than the instruction.
_PREAMBLE = re.compile(r"^.*job description:\s*$", re.I)


_MANDATORY_HEADINGS = (
    "required", "must have", "must-have", "mandatory", "required skills",
    "mandatory skills", "must have skills", "key skills", "essential skills",
)
_PREFERRED_HEADINGS = (
    "preferred", "good to have", "nice to have", "optional", "preferred skills",
    "desirable", "bonus", "plus",
)
_RESET_HEADINGS = (
    "responsibilities", "role", "about the role", "about you",
    "education", "qualifications", "what you will do",
)


def _heading(line: str) -> tuple[str | None, str]:
    """Classify a line as a skill-section heading and return its inline remainder.

    Section 5 writes headings on their own line, but most real JDs use the
    inline form ("Required: Python, AWS"). Both must yield the same buckets.
    """
    head, sep, rest = line.partition(":")
    key = (head if sep else line).strip().lower()
    if key in _MANDATORY_HEADINGS:
        return "mandatory", rest.strip()
    if key in _PREFERRED_HEADINGS:
        return "preferred", rest.strip()
    if key in _RESET_HEADINGS:
        return "reset", rest.strip()
    return None, line



def _extract_jd(raw: str) -> dict[str, Any]:
    lines = [ln.strip(" -•\t") for ln in raw.splitlines()]
    lines = [ln for ln in lines if ln]
    while lines and _PREAMBLE.match(lines[0]):
        lines.pop(0)

    role = lines[0] if lines else "Engineer"
    role = re.sub(r"^(job title|role|position)\s*[:\-]\s*", "", role, flags=re.I).strip()

    exp = None
    m = re.search(r"(\d+(?:\.\d+)?)\s*\+?\s*(?:to|-)?\s*(\d+(?:\.\d+)?)?\s*years?", raw, re.I)
    if m:
        exp = float(m.group(1))

    mandatory: list[str] = []
    preferred: list[str] = []
    consumed: set[int] = set()
    bucket: list[str] | None = None

    def collect(target: list[str], text: str) -> bool:
        found = False
        for token in text.split(","):
            token = token.strip(" .;")
            if token and _SKILL_TOKEN.fullmatch(token):
                target.append(token)
                found = True
        return found

    for idx, ln in enumerate(lines):
        kind, rest = _heading(ln)
        if kind == "reset":
            bucket = None
            continue
        if kind in ("mandatory", "preferred"):
            bucket = mandatory if kind == "mandatory" else preferred
            consumed.add(idx)
            # Inline form ("Required: Python, AWS") carries the list on the
            # same line; block form leaves it to the lines that follow.
            if rest:
                collect(bucket, rest)
            continue
        if bucket is not None and len(ln) < 60 and ":" not in ln:
            if collect(bucket, ln):
                consumed.add(idx)

    responsibilities = [ln for i, ln in enumerate(lines) if i not in consumed and len(ln) > 45][:8]
    location = None
    m = re.search(r"location\s*[:\-]\s*(.+)", raw, re.I)
    if m:
        location = m.group(1).strip()

    return {
        "role": role,
        "min_experience_years": exp,
        "max_experience_years": None,
        "location": location,
        "domain": None,
        "responsibilities": responsibilities,
        "other_requirements": [],
        "mandatory_skills": mandatory,
        "preferred_skills": preferred,
    }


class OfflineProvider(BaseLLMProvider):
    @property
    def provider_name(self) -> str:
        return "offline"

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        system: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        timeout: float = 60.0,
    ) -> LLMResponse:
        last = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                content = m.get("content")
                if isinstance(content, list):
                    content = "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
                last = content or ""
                break

        task = ""
        if system:
            m = re.search(r"TASK:\s*(\w+)", system)
            if m:
                task = m.group(1)

        if task == "PARSE_JD":
            payload = _extract_jd(last)
            return LLMResponse(text=json.dumps(payload), model="offline-stub")

        if task == "COMPOSE_RESUME":
            return LLMResponse(text=json.dumps(self._compose(last)), model="offline-stub")

        if task == "TECH_DEPTH":
            return LLMResponse(text=json.dumps(self._depth(last)), model="offline-stub")

        if task == "COMPOSE_LATEX_RESUME":
            return LLMResponse(text=self._compose_latex(last), model="offline-stub")

        if task == "COMPOSE_LATEX_BODY":
            # The composer copies the preamble itself, so the stub returns only
            # what belongs between the document delimiters.
            return LLMResponse(
                text=self._compose_latex(last, body_only=True), model="offline-stub"
            )

        if task == "EDIT_RESUME":
            return LLMResponse(
                text="Applied your requested change.",
                tool_calls=self._infer_tool_calls(last),
                model="offline-stub",
            )

        return LLMResponse(text="", model="offline-stub")

    # ------------------------------------------------------------------ parts
    @staticmethod
    def _compose(prompt: str) -> dict[str, Any]:
        """Mould deterministically: same shape as the real reply, no invention.

        The stub cannot rewrite prose, so it does the parts of the moulding that
        are mechanical — adopt the JD's title, lead the summary with the target
        role — and leaves every project and designation exactly as supplied.
        That keeps the offline path a truthful subset of the online one rather
        than a differently-shaped response the caller has to special-case.
        """
        def field(label: str, default: str = "") -> str:
            m = re.search(rf"^{label}:\s*(.+)$", prompt, re.M)
            return m.group(1).strip() if m else default

        role = field("Role") or "Engineer"
        # The JD's figure, not a stored career total — nothing stores one.
        years = field("TARGET EXPERIENCE").removesuffix("years").strip()
        mandatory = [t.strip() for t in field("Mandatory skills").split(",") if t.strip() and t.strip() != "n/a"]
        available = [t.strip() for t in field("Available technologies").split(",") if t.strip()]

        # Only claim the overlap: skills the JD wants AND the candidate has.
        low = {t.lower() for t in available}
        overlap = [t for t in mandatory if t.lower() in low] or available[:4]
        phrase = ", ".join(overlap[:4]) if overlap else "modern engineering practices"
        exp = f"{years} years of " if years and years != "n/a" else ""

        # One entry per employer, carrying the two fields the templates now
        # need. The stub cannot write prose, so it assembles them out of facts
        # already in the prompt — the projects listed under that employer and
        # the stack they are on record with — rather than inventing anything.
        # Returning an empty list instead would leave every offline resume
        # printing a column of bare "Company | Title -- dates" lines, which is
        # exactly the shape the templates were changed to stop producing, so
        # the offline path would no longer be a subset of the online one.
        projects_by_company: dict[str, list[str]] = {}
        stack_by_company: dict[str, list[str]] = {}
        for entry in re.finditer(
            r"^- \[[^\]]+\]\s*(?P<name>.+?)\s*\(under (?P<company>[^)]*)\),"
            r"[^\n]*\n\s*stack on record:\s*(?P<stack>[^\n]*)",
            prompt,
            re.M | re.S,
        ):
            company = entry.group("company").strip()
            if not company or company == "no employer":
                continue
            projects_by_company.setdefault(company, []).append(entry.group("name").strip())
            for tech in entry.group("stack").split(","):
                tech = tech.strip()
                if tech and tech != "n/a" and tech not in stack_by_company.setdefault(company, []):
                    stack_by_company[company].append(tech)

        experience = []
        for line in re.finditer(r"^- (?P<company>.+?) as (?P<title>.+?) \(", prompt, re.M):
            company = line.group("company").strip()
            if company in {e["company"] for e in experience}:
                continue
            names = projects_by_company.get(company, [])
            stack = stack_by_company.get(company, [])
            if not names:
                continue
            built = ", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else names[0]
            description = f"Delivered {built}"
            if stack:
                description += f", built on {', '.join(stack[:6])}"
            experience.append(
                {
                    "company": company,
                    "description": description + ".",
                    "highlights": [
                        f"Owned {built} end to end, from design through release.",
                        f"Worked across {', '.join(stack[:4])}."
                        if stack
                        else "Worked across the platform's backend services.",
                    ],
                }
            )

        return {
            "headline": role,
            "summary": (
                f"{role} with {exp}hands-on delivery experience across {phrase}. "
                f"Builds and ships production systems end to end, working closely with "
                f"stakeholders and mentoring engineers."
            ),
            "projects": [],
            "experience": experience,
            "unsupported_additions": [],
        }

    @staticmethod
    def _depth(prompt: str) -> dict[str, Any]:
        """Count, do not guess: how often a technology appears is the evidence.

        A technology named in the projects section is treated as used; one that
        only appears in the skills list is recorded as known but not primary.
        """
        names: list[str] = []
        if "TECHNOLOGIES TO ASSESS" in prompt:
            block = prompt.split("TECHNOLOGIES TO ASSESS", 1)[1]
            names = [ln.strip() for ln in block.splitlines() if ln.strip()][1:]
        body = prompt.split("TECHNOLOGIES TO ASSESS")[0].lower()
        projects = body.split("projects")[-1] if "projects" in body else ""

        results = []
        for n in names:
            hits = projects.count(n.lower())
            results.append({
                "name": n,
                "years_of_experience": None,
                "proficiency": "ADVANCED" if hits >= 2 else ("INTERMEDIATE" if hits == 1 else None),
                "is_primary": hits >= 2,
                "basis": f"named in {hits} project entr{'y' if hits == 1 else 'ies'}",
            })
        return {"results": results}

    # ---------------------------------------------------------- latex
    @staticmethod
    def _compose_latex(prompt: str, *, body_only: bool = False) -> str:
        r"""Deterministically fill the template's preamble with real content.

        With ``body_only`` the preamble and the document delimiters are omitted:
        the real composer copies the template's preamble verbatim and asks only
        for the body, and the stub has to answer the same question.

        The stub cannot write prose, so it does the one thing that is both
        honest and useful offline: keep the template's preamble exactly — that
        is the design, and preserving it is what the real model is judged on —
        then emit the candidate's stored history beneath it as valid LaTeX.

        The result compiles, which is the property the test suite and a
        key-less demo actually need.
        """
        # Split on the section headers rather than the rule beneath them: the
        # rule is drawn with box characters, so matching hyphens silently
        # yields an empty template and an empty candidate.
        def section(start: str, end: str | None) -> str:
            if start not in prompt:
                return ""
            body = prompt.split(start, 1)[1]
            if end and end in body:
                body = body.split(end, 1)[0]
            # The marker sits mid-line ("TEMPLATE (reproduce this design
            # exactly)"), so the rest of that line is heading, not content.
            lines = body.split("\n")[1:]
            # Then drop the rule drawn beneath it, and any blank padding.
            while lines and (not lines[0].strip() or set(lines[0].strip()) <= set("─-=_ ")):
                lines.pop(0)
            return "\n".join(lines).strip()

        template = section("TEMPLATE", "CANDIDATE")

        candidate: dict[str, Any] = {}
        raw = section("CANDIDATE", "TARGET ROLE")
        if raw.startswith("{"):
            try:
                candidate = json.loads(raw[: raw.rfind("}") + 1])
            except json.JSONDecodeError:
                candidate = {}

        role = None
        m = re.search(r'"role":\s*"([^"]*)"', prompt.split("TARGET ROLE")[-1])
        if m:
            role = m.group(1)

        def esc(text: Any) -> str:
            out = str(text or "")
            for ch, rep in (
                ("\\", r"\textbackslash{}"), ("&", r"\&"), ("%", r"\%"), ("$", r"\$"),
                ("#", r"\#"), ("_", r"\_"), ("{", r"\{"), ("}", r"\}"),
                ("~", r"\textasciitilde{}"), ("^", r"\textasciicircum{}"),
            ):
                out = out.replace(ch, rep)
            return out

        # Reuse the template's own preamble so the design survives.
        head = template.split(r"\begin{document}")[0].strip()
        if r"\documentclass" not in head:
            head = "\\documentclass[10pt,a4paper]{article}\n\\usepackage[margin=0.75in]{geometry}\n\\pagestyle{empty}"

        lines: list[str] = [] if body_only else [head, r"\begin{document}", ""]
        title = role or candidate.get("current_title") or ""
        contact = " \u2022 ".join(
            x for x in (candidate.get("email"), candidate.get("phone"), candidate.get("location")) if x
        )
        # An empty {\huge\bfseries }\\ is "There's no line here to end" in
        # LaTeX, so each header line is only emitted when it has content.
        header = [r"\begin{center}"]
        if candidate.get("name"):
            header.append(rf"  {{\huge\bfseries {esc(candidate['name'])}}}\\[2pt]")
        if title:
            header.append(rf"  {{\large\itshape {esc(title)}}}\\[3pt]")
        if contact:
            header.append(rf"  {{\small {esc(contact)}}}")
        if len(header) == 1:
            header.append(r"  {\large\itshape Candidate}")
        header += [r"\end{center}", ""]
        lines += header

        if candidate.get("summary"):
            lines += [r"\section*{Profile Summary}", esc(candidate["summary"]), ""]

        if candidate.get("skills"):
            lines.append(r"\section*{Technical Skills}")
            for group in candidate["skills"]:
                items = ", ".join(esc(i) for i in group.get("items", []))
                lines.append(rf"\textbf{{{esc(group.get('group'))}}}: {items}\\")
            lines.append("")

        if candidate.get("experience"):
            lines.append(r"\section*{Professional Experience}")
            for job in candidate["experience"]:
                span = esc(" -- ".join(x for x in (job.get("from"), job.get("to")) if x))
                lines.append(rf"\textbf{{{esc(job.get('company'))}}} \hfill \textit{{{span}}}\\")
                lines.append(rf"\textit{{{esc(job.get('designation'))}}}")
                if job.get("highlights"):
                    lines.append(r"\begin{itemize}")
                    lines += [rf"  \item {esc(h)}" for h in job["highlights"]]
                    lines.append(r"\end{itemize}")
                lines.append("")

        if candidate.get("projects"):
            lines.append(r"\section*{Key Projects}")
            for proj in candidate["projects"]:
                span = esc(" -- ".join(x for x in (proj.get("from"), proj.get("to")) if x))
                lines.append(rf"\textbf{{{esc(proj.get('name'))}}} \hfill \textit{{{span}}}\\")
                sub = " \u2022 ".join(x for x in (proj.get("role"), proj.get("company")) if x)
                if sub:
                    lines.append(rf"\textit{{{esc(sub)}}}")
                if proj.get("description"):
                    lines.append(esc(proj["description"]))
                bullets = list(proj.get("responsibilities") or []) + list(proj.get("achievements") or [])
                if bullets:
                    lines.append(r"\begin{itemize}")
                    lines += [rf"  \item {esc(b)}" for b in bullets]
                    lines.append(r"\end{itemize}")
                if proj.get("technologies"):
                    lines.append(
                        rf"\textit{{Technologies}}: {', '.join(esc(t) for t in proj['technologies'])}"
                    )
                lines.append("")

        if candidate.get("education"):
            lines.append(r"\section*{Education}")
            lines += [rf"{esc(e)}\\" for e in candidate["education"]]
            lines.append("")

        if candidate.get("certifications"):
            lines.append(r"\section*{Certifications}")
            lines += [rf"{esc(c)}\\" for c in candidate["certifications"]]
            lines.append("")

        if not body_only:
            lines.append(r"\end{document}")
        return "\n".join(lines)

    @staticmethod
    def _infer_tool_calls(instruction: str) -> list[dict[str, Any]]:
        text = instruction.lower()
        calls: list[dict[str, Any]] = []

        m = re.search(r"move ([\w+#. ]+?) (?:higher|to the top|up|first)", text)
        if m:
            calls.append({"id": "c1", "name": "reorder_skill", "arguments": {"technology": m.group(1).strip(), "position": 0}})

        m = re.search(r"remove (?:the )?(education|certifications?|projects?)(?: section)?", text)
        if m:
            calls.append({"id": "c0", "name": "remove_section", "arguments": {"section": m.group(1)}})

        m = re.search(r"remove ([\w+#. ]+?) from", text)
        if m:
            calls.append({"id": "c2", "name": "remove_skill", "arguments": {"technology": m.group(1).strip()}})

        if "summary" in text and any(k in text for k in ("shorter", "three lines", "reduce", "condense")):
            calls.append({"id": "c3", "name": "rewrite_summary", "arguments": {"style": "concise", "max_sentences": 3}})
        elif "summary" in text:
            calls.append({"id": "c4", "name": "rewrite_summary", "arguments": {"style": "focused"}})

        m = re.search(r"(?:use|make) (?:the )?(\w+) project (?:as )?(?:the )?(?:first|primary)", text)
        if m:
            calls.append({"id": "c5", "name": "set_primary_project", "arguments": {"project_query": m.group(1)}})

        if "responsibilities" in text:
            calls.append({"id": "c6", "name": "rewrite_responsibilities", "arguments": {"tone": "senior"}})

        return calls
