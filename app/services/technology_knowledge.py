"""What the platform already knows about a technology it has never stored.

The generation form asks the operator to confirm a release date for any JD
skill the master has no row for. Making them look every one up is how that
step gets skipped, and a technology added without a date silently opts out of
every era check — which is the one thing these dates exist for.

So the form arrives pre-filled. Two sources, in order:

1. a table of names the platform can date outright, including the ones that
   only ever appear as vocabulary rather than as a product ("RAG", "agentic")
2. a family guess from the name itself — anything called "GPT-something" is
   not older than GPT, anything "…GraphQL" is not older than GraphQL

Both are suggestions shown in an editable field, never silent writes. A guess
the operator does not correct is still a guess, so it errs EARLY: an
over-late date would flag correct work, while an early one merely declines to
warn.
"""

from __future__ import annotations

import re

from app.services.era import CONCEPT_FIRST_USED

#: Families whose members cannot predate the family. Longest key wins, so
#: "langgraph" is not answered by the "lang" of "langchain".
_FAMILY_FLOOR: dict[str, int] = {
    "langgraph": 2023,
    "langchain": 2022,
    "langsmith": 2023,
    "llamaindex": 2022,
    "autogen": 2023,
    "crewai": 2023,
    "semantic kernel": 2023,
    "guardrails": 2023,
    "gpt-5": 2025,
    "gpt-4": 2023,
    "gpt-3": 2020,
    "gpt": 2018,
    "claude": 2023,
    "gemini": 2023,
    "llama": 2023,
    "mistral": 2023,
    "copilot": 2021,
    "chatgpt": 2022,
    "agentic": 2023,
    "vector": 2020,
    "embedding": 2018,
    "transformer": 2018,
    "kubernetes": 2015,
    "docker": 2013,
    "terraform": 2014,
    "serverless": 2015,
    "graphql": 2015,
    "grpc": 2015,
    "kafka": 2011,
    "spark": 2014,
    "hadoop": 2006,
    "react": 2013,
    "angular": 2010,
    "vue": 2014,
    "svelte": 2016,
    "next.js": 2016,
    "nuxt": 2016,
    "tailwind": 2017,
    "typescript": 2012,
    "kotlin": 2011,
    "swift": 2014,
    "rust": 2010,
    "golang": 2009,
}

#: Words that place a name in a section of the skills block.
_CATEGORY_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("llm", "gpt", "agent", "rag", "embedding", "langchain", "langgraph", "openai",
      "claude", "gemini", "llama", "mistral", "prompt", "ml", "ai", "model", "nlp",
      "vision", "tensor", "torch", "learn"), "AI & ML"),
    (("sql", "db", "database", "postgres", "mongo", "redis", "cassandra", "oracle",
      "warehouse", "snowflake"), "Database"),
    (("aws", "azure", "gcp", "cloud", "kubernetes", "docker", "terraform", "jenkins",
      "ci/cd", "devops", "helm", "ansible"), "Cloud & DevOps"),
    (("react", "vue", "angular", "css", "html", "tailwind", "svelte", "frontend",
      "ui", "next.js", "nuxt"), "Frontend"),
    (("api", "rest", "graphql", "grpc", "server", "backend", "django", "flask",
      "spring", "express", "fastapi", "nest"), "Backend"),
    (("test", "pytest", "junit", "cypress", "selenium", "mock", "lint"), "Testing & Tooling"),
    (("auth", "oauth", "saml", "jwt", "rbac", "security", "vault"), "Security & Auth"),
    (("kafka", "queue", "rabbit", "pubsub", "celery", "async", "stream"), "Messaging & Async"),
    (("android", "ios", "mobile", "flutter", "swift", "kotlin"), "Mobile"),
)


def _normalise(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def suggested_release_year(name: str) -> int | None:
    """The year to pre-fill, or ``None`` when there is nothing to go on.

    ``None`` is an honest answer: the operator is asked rather than handed a
    number the platform invented.
    """
    text = _normalise(name)
    if not text:
        return None

    if text in CONCEPT_FIRST_USED:
        return CONCEPT_FIRST_USED[text]

    for key in sorted(_FAMILY_FLOOR, key=len, reverse=True):
        if key in text:
            return _FAMILY_FLOOR[key]

    # A version number in the name dates it no earlier than that release's
    # family, but nothing here can date "Acme Widgets 3.2" on its own.
    return None


def suggested_category(name: str) -> str:
    """Where this name most likely belongs in the skills block."""
    text = _normalise(name)
    for hints, category in _CATEGORY_HINTS:
        if any(h in text for h in hints):
            return category
    return "Other"
