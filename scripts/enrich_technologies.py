"""Fill in ``sub_category`` and ``aliases`` on the technology master.

Both columns earn their keep, and both were empty for almost every row:

* ``aliases`` is how a JD's own wording reaches the master. ``jd_parser`` and
  ``technology_repository`` look a skill up by name OR alias, so without them
  a JD asking for "ML" or "k8s" matches nothing and the skill is reported as
  one the bank cannot evidence.
* ``sub_category`` is read by ``resume_composer`` when it scores how well a
  technology answers the JD topically, so an empty one is a row that can only
  match on its name.

Run:  python scripts/enrich_technologies.py [--limit N] [--dry-run]

Idempotent and resumable: only rows that are still missing a value are sent,
and an existing alias list is never overwritten. Safe to re-run after adding
technologies.

ALIASES ARE MATCHING KEYS, NOT SYNONYMS. A careless alias is worse than none:
"go" as an alias for Go would match the word "go" in any sentence of a JD, and
every resume would then claim it. The prompt below refuses short, ambiguous
and generic words for that reason, and anything that slips through is filtered
again here before it is written.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.model_registry import import_all_models  # noqa: E402

import_all_models()

from app.config import LLM_MODEL, LLM_TIMEOUT_SECONDS  # noqa: E402
from app.config.database import SessionLocal  # noqa: E402
from app.models.technology import Technology  # noqa: E402
from app.services.llm.base import extract_json  # noqa: E402
from app.services.llm.factory import get_llm_provider  # noqa: E402

BATCH = 40

SYSTEM = """TASK: TECHNOLOGY_ALIASES

You label software technologies for a skills-matching index.

Return ONLY a JSON object, no preamble and no markdown fences:
  {"results": [{"name": "...", "sub_category": "...", "aliases": ["...", "..."]}]}

Return one entry for every name given, in the same order, with the name copied
back exactly as supplied.

sub_category
- A two-to-four word grouping INSIDE the category you are given, e.g. for
  category "Cloud & DevOps": "Container orchestration", "CI/CD", "Infrastructure
  as code". For "Frontend": "UI component library", "State management".
- It must say what KIND of thing this is, not repeat the category or the name.

aliases
- Other spellings a job description might genuinely use for this exact
  technology: abbreviations, expansions, punctuation variants, the old name.
  "Kubernetes" -> ["k8s"]. "PostgreSQL" -> ["postgres"]. "Node.js" ->
  ["nodejs", "node js"]. "Machine Learning" -> ["ml"].
- These are used as MATCHING KEYS against free job-description text, so an
  ambiguous alias makes every resume claim a skill nobody has. Therefore:
  * never an ordinary English word on its own ("go", "rust", "swift", "dart",
    "spark" alone) — if the only short form is a common word, return no alias
  * never a broader category ("cloud", "database", "framework")
  * never a related-but-different product (React is not an alias of React Native)
  * never the name itself, and never something already in the list you are given
- Two or three good aliases beat a long list. An empty list is a correct answer
  and is much better than a risky one.
"""

USER = """Category: {category}
Technologies:
{names}

Return the JSON object described above and nothing else."""

#: Rejected regardless of what the model says — see the module docstring.
_TOO_GENERIC = {
    "cloud", "database", "db", "framework", "library", "language", "tool",
    "tooling", "platform", "service", "services", "api", "apis", "web",
    "backend", "frontend", "data", "ml", "ai", "app", "apps", "server",
    "testing", "test", "code", "software", "system", "systems", "dev",
}


def _clean_aliases(raw, name: str, existing: list[str]) -> list[str]:
    """Keep only aliases that are safe to match a JD against."""
    if not isinstance(raw, list):
        return []
    seen = {name.strip().lower(), *(a.strip().lower() for a in existing)}
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        alias = " ".join(item.split()).strip().lower()
        # Two characters or fewer matches far too much free text to be worth
        # the recall it buys ("ai" is the obvious casualty, and deliberately).
        if len(alias) < 3 or alias in seen or alias in _TOO_GENERIC:
            continue
        seen.add(alias)
        out.append(alias)
    return out[:4]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="only this many rows")
    parser.add_argument("--dry-run", action="store_true", help="print, write nothing")
    args = parser.parse_args()

    provider = get_llm_provider()
    filled_sub = filled_alias = 0

    with SessionLocal() as db:
        rows = (
            db.query(Technology)
            .filter(Technology.is_active.is_(True))
            .order_by(Technology.category, Technology.name)
            .all()
        )
        todo = [t for t in rows if not t.sub_category or not t.aliases]
        if args.limit:
            todo = todo[: args.limit]
        print(f"{len(rows)} technologies, {len(todo)} needing work")

        for start in range(0, len(todo), BATCH):
            batch = todo[start : start + BATCH]
            category = batch[0].category or "Other"
            names = "\n".join(f"- {t.name}" for t in batch)
            try:
                reply = provider.chat(
                    [{"role": "user", "content": USER.format(category=category, names=names)}],
                    model=LLM_MODEL,
                    system=SYSTEM,
                    temperature=0.0,
                    max_tokens=16000,
                    timeout=LLM_TIMEOUT_SECONDS,
                )
                data = json.loads(extract_json(reply.text or ""))
            except Exception as exc:  # noqa: BLE001 — one bad batch must not stop the rest
                print(f"  batch {start // BATCH + 1}: FAILED ({type(exc).__name__}: {exc}); skipped")
                continue

            by_name = {
                str(r.get("name", "")).strip().lower(): r
                for r in (data.get("results") or [])
                if isinstance(r, dict)
            }
            for tech in batch:
                got = by_name.get(tech.name.strip().lower())
                if not got:
                    continue
                if not tech.sub_category and isinstance(got.get("sub_category"), str):
                    sub = " ".join(got["sub_category"].split()).strip()
                    if sub and sub.lower() != (tech.category or "").lower():
                        tech.sub_category = sub[:100]
                        filled_sub += 1
                if not tech.aliases:
                    aliases = _clean_aliases(got.get("aliases"), tech.name, [])
                    if aliases:
                        tech.aliases = aliases
                        filled_alias += 1
            print(
                f"  batch {start // BATCH + 1}/{(len(todo) + BATCH - 1) // BATCH} "
                f"({category}, {len(batch)} rows) done"
            )
            if not args.dry_run:
                db.commit()

        # ── cross-row pass ────────────────────────────────────────────────
        # The per-row filter cannot see the rest of the table, and two kinds of
        # ambiguity only exist between rows:
        #
        #   * an alias that is also another technology's NAME. jd_parser
        #     resolves a JD skill by name OR alias, so "azure" pointing at both
        #     the row called Azure and the row called Microsoft Azure makes the
        #     match arbitrary. The name lookup already covers that word.
        #   * an alias claimed by more than one technology, for the same reason.
        #
        # Both are dropped rather than picked between: an ambiguous key is
        # worse than a missing one, which is the same rule the per-row filter
        # follows. Note this surfaces genuine duplicates in the master (MCP and
        # "MCP frameworks", Azure and "Microsoft Azure") -- those rows are left
        # alone here, since merging technologies moves existing links.
        all_rows = db.query(Technology).all()
        names_lower = {t.name.strip().lower() for t in all_rows}
        claimed: dict[str, list] = {}
        for t in all_rows:
            for alias in t.aliases or []:
                claimed.setdefault(alias.strip().lower(), []).append(t)

        removed = 0
        for t in all_rows:
            keep = []
            for alias in t.aliases or []:
                key = alias.strip().lower()
                if key in names_lower and key != t.name.strip().lower():
                    removed += 1
                    continue
                if len(claimed.get(key, [])) > 1:
                    removed += 1
                    continue
                keep.append(alias)
            if keep != list(t.aliases or []):
                t.aliases = keep
        if removed:
            print(f"dropped {removed} ambiguous alias(es) that collided across rows")
            if not args.dry_run:
                db.commit()

        if args.dry_run:
            db.rollback()
            print("\n--dry-run: nothing written")
        print(f"\nfilled sub_category on {filled_sub}, aliases on {filled_alias}")
        remaining_sub = sum(1 for t in db.query(Technology).all() if not t.sub_category)
        remaining_alias = sum(1 for t in db.query(Technology).all() if not t.aliases)
        print(f"still empty: sub_category {remaining_sub}, aliases {remaining_alias}")


if __name__ == "__main__":
    main()
