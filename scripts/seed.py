"""Seed demo data reproducing the section 43 worked example.

Run:  python scripts/seed.py
Then: LLM_OFFLINE_MODE=true uvicorn app.main:app --reload

Creates the three people from section 7 with different vendor statuses, so the
vendor-scoped availability rule is visible immediately: Neha is ENGAGED with ABC
but AVAILABLE to XYZ.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.auth.hashing import hash_password  # noqa: E402
from app.config.database import Base, SessionLocal, engine  # noqa: E402
from app.model_registry import import_all_models  # noqa: E402
from app.models.availability import ProfileVendorStatus  # noqa: E402
from app.models.company import Company  # noqa: E402
from app.models.person import Employment, Person, PersonProfile, ProfileType  # noqa: E402
from app.models.project import Project, ProjectTechnology  # noqa: E402
from app.models.resume_template import ResumeTemplate
from app.models.technology import PersonTechnology, Technology  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.vendor import Vendor  # noqa: E402
from app.resume_templates import library  # noqa: E402
from app.services import storage  # noqa: E402
from app.utils.enums import ProfileVendorStatusEnum  # noqa: E402

# name, category, first_release_date — release dates power the validation engine
TECHNOLOGIES = [
    ("Python", "Languages", date(1991, 2, 20), ["python3", "py"]),
    ("Java", "Languages", date(1995, 5, 23), []),
    ("TypeScript", "Languages", date(2012, 10, 1), ["ts"]),
    ("SQL", "Languages", None, []),
    ("FastAPI", "Backend", date(2018, 12, 5), []),
    ("Django", "Backend", date(2005, 7, 21), []),
    ("Node.js", "Backend", date(2009, 5, 27), ["node", "nodejs"]),
    ("React", "Frontend", date(2013, 5, 29), ["react.js", "reactjs"]),
    ("Next.js", "Frontend", date(2016, 10, 25), ["nextjs"]),
    ("PostgreSQL", "Data", date(1996, 7, 8), ["postgres"]),
    ("MongoDB", "Data", date(2009, 2, 11), []),
    ("Redis", "Data", date(2009, 5, 10), []),
    ("Spark", "Data", date(2014, 5, 30), ["apache spark", "pyspark"]),
    ("Kafka", "Data", date(2011, 1, 1), ["apache kafka"]),
    ("Power BI", "Data", date(2015, 7, 24), ["powerbi"]),
    ("AWS", "Cloud", date(2006, 3, 14), ["amazon web services"]),
    ("Azure", "Cloud", date(2010, 2, 1), []),
    ("Docker", "Cloud", date(2013, 3, 20), []),
    ("Kubernetes", "Cloud", date(2015, 7, 21), ["k8s"]),
    ("Terraform", "Cloud", date(2014, 7, 28), []),
    ("LLM", "GenAI", date(2020, 6, 11), ["large language model", "llms"]),
    ("RAG", "GenAI", date(2021, 4, 1), ["retrieval augmented generation"]),
    ("LangChain", "GenAI", date(2022, 10, 17), []),
    ("Vector Database", "GenAI", date(2021, 1, 1), ["vectordb", "pinecone", "weaviate"]),
    ("PyTorch", "ML", date(2017, 1, 18), []),
    ("TensorFlow", "ML", date(2015, 11, 9), []),
    ("scikit-learn", "ML", date(2010, 6, 1), ["sklearn"]),
]

PROFILE_TYPES = [
    "AI Engineer", "GenAI Engineer", "Backend Engineer",
    "Frontend Engineer", "Full Stack Engineer", "Data Engineer",
]

#: name, role (matched against a JD's role, independent of any project)
COMPANIES = [
    ("Company A", "AI Engineer"),
    ("Company B", "Backend Engineer"),
    ("Company C", "Data Scientist"),
    ("Company D", "Healthcare Software Engineer"),
]

VENDORS = ["ABC Recruitment", "XYZ Recruitment", "Talent Provider"]


def _seed_demo_data(db) -> None:
    """The section 43 worked example — people, companies, vendors, one legacy
    starter template. Only ever runs against a database with no people yet."""
    user = User(
        email="admin@example.com",
        full_name="Demo Admin",
        hashed_password=hash_password("admin123"),
    )
    db.add(user)

    techs = {}
    for name, category, released, aliases in TECHNOLOGIES:
        t = Technology(name=name, category=category, first_release_date=released, aliases=aliases)
        db.add(t)
        techs[name] = t

    ptypes = {}
    for name in PROFILE_TYPES:
        p = ProfileType(name=name)
        db.add(p)
        ptypes[name] = p

    companies = {}
    for name, role in COMPANIES:
        c = Company(name=name, role=role)
        db.add(c)
        companies[name] = c

    vendors = {}
    for name in VENDORS:
        v = Vendor(name=name, contact_email=f"{name.split()[0].lower()}@example.com")
        db.add(v)
        vendors[name] = v

    # Section 26 — templates are LaTeX. One bundled default is seeded so a
    # fresh environment can render immediately; real templates are uploaded.
    default_tex = Path(__file__).resolve().parent.parent / "app" / "resume_templates" / "default_resume.tex"
    source = default_tex.read_text(encoding="utf-8")
    stored = storage.template_dir() / "default-resume.tex"
    stored.write_text(source, encoding="utf-8", newline="\n")
    db.add(
        ResumeTemplate(
            code="DEFAULT_TEX_01",
            name="Default LaTeX Resume",
            description="Bundled starter template. Upload your own .tex to replace it.",
            layout_definition={},
            source_format="LATEX",
            source_file_name="default_resume.tex",
            source_storage_path=storage.relative_path(stored),
            source_size_bytes=len(source.encode("utf-8")),
        )
    )
    db.flush()

    # ---------------------------------------------------------- people
    people = [
        {
            "name": "Rahul Giri", "code": "EMP001", "designation": "Senior AI Engineer",
            "education": "B.Tech Computer Science, Delhi Technological University, 2018",
            "certs": ["AWS Certified Solutions Architect – Associate"],
            "profiles": [
                ("AI Engineer", "Senior AI Engineer",
                 "AI engineer with seven years building production machine learning and "
                 "retrieval systems across retail and financial services."),
                ("GenAI Engineer", "GenAI Engineer",
                 "Builds retrieval-augmented assistants and LLM tooling on AWS."),
            ],
            "techs": [("Python", 7, "ADVANCED", True), ("RAG", 2.5, "ADVANCED", True),
                      ("LLM", 3, "ADVANCED", True), ("AWS", 5, "ADVANCED", True),
                      ("FastAPI", 4, "ADVANCED", True), ("Docker", 5, "INTERMEDIATE", False),
                      ("LangChain", 2, "INTERMEDIATE", False), ("PyTorch", 4, "ADVANCED", False),
                      ("PostgreSQL", 6, "ADVANCED", False), ("Vector Database", 2, "INTERMEDIATE", False)],
            "employments": [("Company C", "Data Scientist", "2018-07-01", "2021-03-31"),
                            ("Company A", "Senior AI Engineer", "2021-04-01", None)],
            "projects": [
                ("Retail Demand Forecasting Platform", "Company C", "Data Scientist",
                 date(2019, 1, 1), date(2021, 3, 1),
                 "Forecasting platform predicting SKU-level demand across 400 stores.",
                 ["Built feature pipelines over three years of sales history",
                  "Deployed gradient-boosted models behind a batch scoring service"],
                 ["Reduced stockouts by 18% in the first pilot region"],
                 ["Python", "scikit-learn", "PostgreSQL", "Spark"]),
                ("Banking Document Assistant", "Company A", "Lead Engineer",
                 date(2023, 2, 1), None,
                 "Retrieval-augmented assistant answering policy questions over internal banking documents.",
                 ["Designed the chunking and embedding pipeline",
                  "Built the evaluation harness for retrieval quality",
                  "Led a team of four engineers"],
                 ["Cut average research time for compliance queries from 20 minutes to under 3"],
                 ["Python", "RAG", "LLM", "LangChain", "FastAPI", "AWS", "Vector Database"]),
                ("Internal ML Platform", "Company A", "Senior Engineer",
                 date(2021, 6, 1), date(2023, 1, 31),
                 "Shared training and deployment platform used by three data teams.",
                 ["Built model registry and rollout tooling", "Containerised training jobs"],
                 [], ["Python", "Docker", "AWS", "PyTorch"]),
            ],
        },
        {
            "name": "Amit Shah", "code": "EMP002", "designation": "Backend Engineer",
            "education": "B.E. Information Technology, University of Mumbai, 2019",
            "certs": [],
            "profiles": [("Backend Engineer", "Senior Backend Engineer",
                          "Backend engineer specialising in Python services and event-driven systems.")],
            "techs": [("Python", 6, "ADVANCED", True), ("FastAPI", 4, "ADVANCED", True),
                      ("PostgreSQL", 6, "ADVANCED", True), ("Kafka", 3, "INTERMEDIATE", False),
                      ("Docker", 4, "ADVANCED", False), ("Redis", 4, "INTERMEDIATE", False),
                      ("AWS", 3, "INTERMEDIATE", False)],
            "employments": [("Company B", "Backend Engineer", "2019-06-01", None)],
            "projects": [
                ("Payments Reconciliation Service", "Company B", "Backend Engineer",
                 date(2020, 1, 1), date(2022, 12, 31),
                 "Event-driven service reconciling millions of daily payment records.",
                 ["Designed the Kafka consumer topology", "Owned the idempotency model"],
                 ["Brought reconciliation lag from hours down to under five minutes"],
                 ["Python", "Kafka", "PostgreSQL", "Redis"]),
                ("Customer API Gateway", "Company B", "Senior Backend Engineer",
                 date(2023, 1, 1), None,
                 "Public API gateway consolidating twelve internal services.",
                 ["Built the FastAPI service layer", "Introduced contract testing"],
                 [], ["Python", "FastAPI", "Docker", "AWS"]),
            ],
        },
        {
            "name": "Neha Sharma", "code": "EMP003", "designation": "GenAI Engineer",
            "education": "M.Tech Artificial Intelligence, IIT Delhi, 2020",
            "certs": ["Azure AI Engineer Associate"],
            "profiles": [("GenAI Engineer", "GenAI Engineer",
                          "Focused on retrieval systems and LLM application development.")],
            "techs": [("Python", 5, "ADVANCED", True), ("RAG", 2, "ADVANCED", True),
                      ("LLM", 2.5, "ADVANCED", True), ("LangChain", 2, "ADVANCED", True),
                      ("Azure", 3, "INTERMEDIATE", False), ("TensorFlow", 3, "INTERMEDIATE", False)],
            "employments": [("Company C", "ML Engineer", "2020-07-01", None)],
            "projects": [
                ("Knowledge Base Search Assistant", "Company C", "ML Engineer",
                 date(2022, 6, 1), None,
                 "Semantic search assistant over a 50,000-document internal knowledge base.",
                 ["Built the ingestion and embedding pipeline", "Tuned retrieval ranking"],
                 [], ["Python", "RAG", "LLM", "LangChain", "Azure"]),
            ],
        },
    ]

    person_objs = {}
    for spec in people:
        person = Person(
            employee_code=spec["code"], full_name=spec["name"],
            email=f"{spec['name'].split()[0].lower()}@example.com",
            current_designation=spec["designation"],
            education=spec["education"], certifications=spec["certs"],
        )
        db.add(person)
        db.flush()
        person_objs[spec["name"]] = person

        for tname, yrs, prof, primary in spec["techs"]:
            db.add(PersonTechnology(
                person_id=person.id, technology_id=techs[tname].id,
                years_of_experience=yrs, proficiency=prof,
                is_primary="true" if primary else "false",
            ))

        # _start/_end are narrative context for this seed spec only. An
        # employment records THAT somebody worked somewhere, never when:
        # the timeline is composed per resume from the JD's own years
        # (see app/models/person.py).
        for cname, desig, _start, _end in spec["employments"]:
            db.add(Employment(
                person_id=person.id, company_id=companies[cname].id, designation=desig,
            ))

        created_projects = []
        # _cname/_start/_end/_ach are narrative context for this seed spec
        # only — a project carries no person, company or dates of its own
        # any more (see app/models/project.py), so none of it is persisted.
        for pname, _cname, role, _start, _end, desc, resp, _ach, ptechs in spec["projects"]:
            project = Project(
                name=pname, role=role, short_description=desc,
                responsibilities=resp,
                keywords=[t.lower() for t in ptechs],
            )
            db.add(project)
            db.flush()
            created_projects.append(project)
            for tname in ptechs:
                db.add(ProjectTechnology(
                    project_id=project.id, technology_id=techs[tname].id, is_core="true"
                ))

        for ptype_name, title, summary in spec["profiles"]:
            profile = PersonProfile(
                person_id=person.id, profile_type_id=ptypes[ptype_name].id,
                title=title, summary=summary,
            )
            profile.projects = created_projects
            db.add(profile)

    db.flush()

    # ------------------------------------------- vendor-scoped statuses
    # Section 8: the same person differs per vendor. Neha is engaged with
    # ABC but still available to XYZ — this is the rule made visible.
    statuses = [
        ("Rahul Giri", "ABC Recruitment", ProfileVendorStatusEnum.AVAILABLE, "Client A — GenAI"),
        ("Rahul Giri", "XYZ Recruitment", ProfileVendorStatusEnum.AVAILABLE, "Client A — GenAI"),
        ("Amit Shah", "ABC Recruitment", ProfileVendorStatusEnum.AVAILABLE, "Client B — Python"),
        ("Amit Shah", "XYZ Recruitment", ProfileVendorStatusEnum.ENGAGED, "Client B — Python"),
        ("Neha Sharma", "ABC Recruitment", ProfileVendorStatusEnum.ENGAGED, "Client C — RAG"),
        ("Neha Sharma", "XYZ Recruitment", ProfileVendorStatusEnum.AVAILABLE, "Client C — RAG"),
    ]
    for pname, vname, status, label in statuses:
        db.add(ProfileVendorStatus(
            person_id=person_objs[pname].id, vendor_id=vendors[vname].id,
            status=status, current_engagement_label=label,
        ))

    db.commit()
    print("Seeded demo data.")
    print("  Login: admin@example.com / admin123")
    print(f"  {len(people)} people, {len(TECHNOLOGIES)} technologies, "
          f"{len(VENDORS)} vendors, 1 starter template")
    print("  Note: Neha is ENGAGED with ABC Recruitment but AVAILABLE to XYZ.")


def seed() -> None:
    import_all_models()
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        if db.query(Person).count():
            print("Demo people already present — leaving them as they are.")
        else:
            _seed_demo_data(db)
    finally:
        db.close()

    # The shipped template library. ALWAYS refreshed here, demo data just
    # created or not, because scripts/seed_template_library.py is itself
    # idempotent and specifically designed to be safe to rerun any time --
    # that is what lets `git pull` followed by `python scripts/seed.py`
    # deliver a new or changed base template to a database that already has
    # real people in it. Nesting this inside the "no people yet" branch above
    # would defeat that: the whole point of the library installer refreshing
    # AND re-activating every design on every run (see its own module
    # docstring) is that a database is never stuck on an old template set
    # just because it was seeded once, weeks ago.
    from scripts.seed_template_library import seed_library

    changes = seed_library()
    print(
        "Template library: "
        + (", ".join(changes) if changes else "up to date")
        + f" ({len(library.TEMPLATES)} designs, all base templates active)"
    )


if __name__ == "__main__":
    seed()
