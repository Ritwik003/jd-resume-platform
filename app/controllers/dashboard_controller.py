"""The dashboard — the one screen that answers "what is happening" in one request.

Every other module here answers a question you already knew you had: the JD
list is for when you are looking for a JD, vendor tracking for when you are
thinking about a vendor. This screen is for the moment before that, when the
question is only "what changed, and what needs me". So it is deliberately
narrow — six things, in the order somebody actually reads them:

    1. who was engaged today, with the fortnight behind it for shape
    2. what is reserved right now, with a clock running on it
    3. which vendors we took on this month
    4. where every profile stands, per vendor
    5. what came back from vendors over the last eight weeks
    6. what JDs demand against what the bank actually holds

**One endpoint, one payload.** Six panels firing six requests is what makes a
landing screen feel slow, and this is the screen that decides whether the app
feels fast. Everything below is a handful of grouped counts plus three bounded
scans, assembled here rather than in the browser.

Two conventions, both taken from the rest of the codebase rather than invented
for this file:

* **Time arithmetic happens in Python, on ``as_aware()`` values.** SQLite hands
  datetimes back naive and Postgres hands them back aware; comparing them after
  the read is where that difference turns into a wrong answer. SQL-side ``>=``
  against an aware cutoff is used only to *bound a scan*, which both dialects
  agree on — SQLite binds the UTC components, and ``func.now()`` there is UTC.
* **Nothing here ranks people.** Sections 7 and 42 say the platform does not
  rank candidates, and a dashboard is exactly where a "top profiles" table
  would look harmless and be the same thing. Vendors are ordered by volume,
  technologies by demand; people appear only as facts with a date on them.

The status event stream is read from ``AuditLog``, not from
``SubmissionStatusHistory``. Every status move goes through
``app/services/availability.py``, which records ``PROFILE_ENGAGED`` and its
siblings unconditionally; the submission history table is only written on the
two paths in ``resume_session_service``, so the vendor-tracking screen's own
status changes never reach it. Audit is the stream that is actually complete.

Read-only from end to end. Nothing here writes a row or expires a hold as a
side effect: a screen that changes state by being looked at is one nobody can
safely leave open, and this one is meant to be left open.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth.current_user import get_current_user
from app.config.database import get_db
from app.models.audit import AuditLog
from app.models.availability import ProfileVendorStatus
from app.models.person import Person
from app.models.requirement import Requirement, RequirementTechnology
from app.models.resume_session import ResumeCompositionSession
from app.models.submission import Submission
from app.models.technology import PersonTechnology, Technology
from app.models.user import User
from app.models.vendor import Vendor
from app.utils.enums import ProfileVendorStatusEnum, RequirementStatus
from app.utils.helper import as_aware, utcnow

router = APIRouter()

#: A hold with less than this left is the thing an operator has to see first.
EXPIRING_SOON_MINUTES = 120

#: How far back each series looks. Short enough to stay one screen, long enough
#: that a quiet week does not read as a dead platform.
ENGAGEMENT_DAYS = 14
OUTCOME_WEEKS = 8
VENDOR_MONTHS = 6

#: Row caps. Every list here is a summary with a real screen behind it, so
#: these are display limits, never the count printed next to the heading.
ENGAGED_EARLIER_ROWS = 12
RESERVATION_ROWS = 8
NEW_VENDOR_ROWS = 10
PIPELINE_VENDORS = 8
SKILL_ROWS = 10
UNMATCHED_SKILL_ROWS = 8

#: The one status vocabulary, in lifecycle order — the order the stacked bar
#: reads left to right, so the picture matches the state machine.
PIPELINE_ORDER: tuple[str, ...] = (
    ProfileVendorStatusEnum.AVAILABLE,
    ProfileVendorStatusEnum.EDITING,
    ProfileVendorStatusEnum.RESERVED,
    ProfileVendorStatusEnum.SUBMITTED,
    ProfileVendorStatusEnum.ENGAGED,
    ProfileVendorStatusEnum.RELEASED,
    ProfileVendorStatusEnum.REJECTED,
)

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _day_start(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def _month_start(moment: datetime) -> datetime:
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _previous_month(month_start: datetime) -> datetime:
    """The first instant of the month before ``month_start``."""
    return _month_start(month_start - timedelta(days=1))


def _minutes_left(expires_at: datetime | None, now: datetime) -> int | None:
    """Minutes until a hold lapses — never negative, so the UI need not clamp."""
    aware = as_aware(expires_at)
    return max(0, int((aware - now).total_seconds() // 60)) if aware else None


@router.get("/summary")
def dashboard_summary(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    """Everything the dashboard prints, in one payload."""
    now = utcnow()
    today_start = _day_start(now)

    return {
        "generated_at": now.isoformat(),
        "kpis": _kpis(db, now),
        "engagements": _engagements(db, now, today_start),
        "reservations": _reservations(db, now),
        "new_vendors": _new_vendors(db, now),
        "pipeline_by_vendor": _pipeline_by_vendor(db),
        "outcomes_weekly": _outcomes_weekly(db, now),
        "skill_demand": _skill_demand(db),
    }


# ───────────────────────────────────────────── the strip across the top
def _kpis(db: Session, now: datetime) -> dict:
    """Six counts, each one clickable through to the screen that owns it."""
    by_status = {
        str(status): count
        for status, count in db.query(
            ProfileVendorStatus.status, func.count(ProfileVendorStatus.id)
        ).group_by(ProfileVendorStatus.status)
    }

    open_jds = (
        db.query(func.count(Requirement.id))
        .filter(Requirement.status.in_([RequirementStatus.OPEN, RequirementStatus.IN_PROGRESS]))
        .scalar()
        or 0
    )

    # Read the expiry column and compare it here rather than in SQL: the two
    # dialects disagree about what a naive column means, and this is the number
    # an operator acts on first, so it has to be right on both.
    holds = (
        db.query(ProfileVendorStatus.reservation_expires_at)
        .filter(
            ProfileVendorStatus.status.in_(
                [ProfileVendorStatusEnum.EDITING, ProfileVendorStatusEnum.RESERVED]
            )
        )
        .all()
    )
    expiring_soon = sum(
        1
        for (expires_at,) in holds
        if (left := _minutes_left(expires_at, now)) is not None and left <= EXPIRING_SOON_MINUTES
    )

    today_start = _day_start(now)
    engaged_today = sum(
        1
        for (started,) in db.query(ProfileVendorStatus.engagement_start).filter(
            ProfileVendorStatus.status == ProfileVendorStatusEnum.ENGAGED
        )
        if (aware := as_aware(started)) is not None and aware >= today_start
    )

    return {
        "open_jds": open_jds,
        "live_holds": (
            by_status.get(str(ProfileVendorStatusEnum.EDITING), 0)
            + by_status.get(str(ProfileVendorStatusEnum.RESERVED), 0)
        ),
        "expiring_soon": expiring_soon,
        "expiring_within_minutes": EXPIRING_SOON_MINUTES,
        "out_with_vendors": by_status.get(str(ProfileVendorStatusEnum.SUBMITTED), 0),
        "engaged": by_status.get(str(ProfileVendorStatusEnum.ENGAGED), 0),
        "engaged_today": engaged_today,
    }


# ──────────────────────────────────────── engaged today, and the fortnight
def _engagements(db: Session, now: datetime, today_start: datetime) -> dict:
    """Today's engagements first, everything still running underneath.

    "Engaged today" is read from the live pair rows, so it says who is engaged
    *and started today*. The fourteen-day bars beside it are read from the
    audit stream instead, so an engagement that has since been released still
    counts on the day it happened. The two answer different questions and are
    allowed to disagree; that is why the bars are labelled as events.
    """
    rows = (
        db.query(ProfileVendorStatus, Person, Vendor, Requirement)
        .join(Person, Person.id == ProfileVendorStatus.person_id)
        .join(Vendor, Vendor.id == ProfileVendorStatus.vendor_id)
        .outerjoin(Requirement, Requirement.id == ProfileVendorStatus.current_requirement_id)
        .filter(ProfileVendorStatus.status == ProfileVendorStatusEnum.ENGAGED)
        .all()
    )

    def as_row(
        pair: ProfileVendorStatus, person: Person, vendor: Vendor, requirement: Requirement | None
    ) -> dict:
        started = as_aware(pair.engagement_start)
        return {
            "person_id": str(person.id),
            "person_name": person.full_name,
            "vendor_id": str(vendor.id),
            "vendor_name": vendor.name,
            "role": requirement.parsed_role if requirement else None,
            "requirement_code": requirement.reference_code if requirement else None,
            "engagement_label": pair.current_engagement_label,
            "engagement_start": started.isoformat() if started else None,
        }

    dated = [
        (as_aware(pair.engagement_start), pair, person, vendor, requirement)
        for pair, person, vendor, requirement in rows
    ]

    # An engaged pair with no start date is a status set by hand before the
    # timestamp was recorded. It is still engaged, so it belongs in the running
    # list — never in "today", which is a claim about a date it has not got.
    today = [
        as_row(pair, person, vendor, requirement)
        for started, pair, person, vendor, requirement in dated
        if started and started >= today_start
    ]
    earlier = [
        (started, as_row(pair, person, vendor, requirement))
        for started, pair, person, vendor, requirement in dated
        if not (started and started >= today_start)
    ]

    today.sort(key=lambda row: row["engagement_start"] or "", reverse=True)
    # Undated rows last: `None` cannot be compared to a datetime, and it is the
    # oldest thing on the list in every sense that matters.
    earlier.sort(
        key=lambda pair: (pair[0] is not None, pair[0] or datetime.min.replace(tzinfo=now.tzinfo)),
        reverse=True,
    )

    return {
        "today": today,
        "earlier": [row for _started, row in earlier[:ENGAGED_EARLIER_ROWS]],
        "earlier_total": len(earlier),
        "daily": _daily_engagement_events(db, now),
    }


def _daily_engagement_events(db: Session, now: datetime) -> list[dict]:
    """One bar per day for the last fortnight, today last and marked.

    Days with nothing are emitted as zeroes rather than skipped: a gap that
    silently closes up turns a quiet week into a busy-looking one.
    """
    since = _day_start(now) - timedelta(days=ENGAGEMENT_DAYS - 1)
    per_day: Counter[str] = Counter()
    for (created_at,) in db.query(AuditLog.created_at).filter(
        AuditLog.action == "PROFILE_ENGAGED", AuditLog.created_at >= since
    ):
        moment = as_aware(created_at)
        if moment is not None and moment >= since:
            per_day[moment.date().isoformat()] += 1

    days = []
    for offset in range(ENGAGEMENT_DAYS):
        day = (since + timedelta(days=offset)).date()
        days.append(
            {
                "date": day.isoformat(),
                "label": f"{day.day} {_MONTHS[day.month - 1]}",
                "count": per_day.get(day.isoformat(), 0),
                "is_today": day == now.date(),
            }
        )
    return days


# ─────────────────────────────────────────────── what is held right now
def _reservations(db: Session, now: datetime) -> dict:
    """The holds with the least time left, and how many there are in total.

    A digest of the Reservations board, not a replacement for it: eight rows,
    read-only, with the real count beside the heading so a short list never
    reads as the whole list. Every action stays on the board that owns it.
    """
    rows = (
        db.query(ProfileVendorStatus, Person, Vendor, Requirement)
        .join(Person, Person.id == ProfileVendorStatus.person_id)
        .join(Vendor, Vendor.id == ProfileVendorStatus.vendor_id)
        .outerjoin(Requirement, Requirement.id == ProfileVendorStatus.current_requirement_id)
        .filter(ProfileVendorStatus.status == ProfileVendorStatusEnum.RESERVED)
        .all()
    )
    if not rows:
        return {"total": 0, "rows": []}

    # The newest session per pair, and its submission reference, in one query
    # each rather than two per row.
    person_ids = {pair.person_id for pair, _p, _v, _r in rows}
    vendor_ids = {pair.vendor_id for pair, _p, _v, _r in rows}
    sessions: dict[tuple, ResumeCompositionSession] = {}
    for session in (
        db.query(ResumeCompositionSession)
        .filter(
            ResumeCompositionSession.person_id.in_(person_ids),
            ResumeCompositionSession.vendor_id.in_(vendor_ids),
        )
        .order_by(ResumeCompositionSession.created_at.asc(), ResumeCompositionSession.id.asc())
    ):
        # Ascending, so the last write for a pair leaves the newest session.
        sessions[(session.person_id, session.vendor_id)] = session

    session_ids = [s.id for s in sessions.values()]
    references = {
        session_id: reference
        for session_id, reference in db.query(Submission.session_id, Submission.reference_code)
        .filter(Submission.session_id.in_(session_ids or [None]))
        .order_by(Submission.created_at.asc(), Submission.id.asc())
    }

    out = []
    for pair, person, vendor, requirement in rows:
        session = sessions.get((pair.person_id, pair.vendor_id))
        expires_at = as_aware(pair.reservation_expires_at)
        out.append(
            {
                "person_id": str(person.id),
                "person_name": person.full_name,
                "vendor_id": str(vendor.id),
                "vendor_name": vendor.name,
                "role": requirement.parsed_role if requirement else None,
                "requirement_code": requirement.reference_code if requirement else None,
                "session_id": str(session.id) if session else None,
                "reference": ((references.get(session.id) or session.reference_code) if session else None),
                "version_no": session.current_version_no if session else None,
                "hold_expires_at": expires_at.isoformat() if expires_at else None,
                "hold_minutes_left": _minutes_left(pair.reservation_expires_at, now),
            }
        )

    # Soonest to lapse first. A hold with no clock on it sorts last rather than
    # jumping the queue on a null.
    out.sort(key=lambda row: row["hold_minutes_left"] if row["hold_minutes_left"] is not None else 10**9)
    return {"total": len(out), "rows": out[:RESERVATION_ROWS]}


# ───────────────────────────────────────────── vendors taken on this month
def _new_vendors(db: Session, now: datetime) -> dict:
    """Vendors added since the first of this month, and the five months behind it.

    The window is the calendar month, recomputed from ``now`` on every request
    — so it empties by itself on the first and there is nothing to reset. Each
    row carries the two facts that say whether a new vendor turned into
    anything: JDs raised, and profiles put forward.
    """
    month_start = _month_start(now)
    vendors = (
        db.query(Vendor).filter(Vendor.created_at >= month_start).order_by(Vendor.created_at.desc()).all()
    )
    shown = vendors[:NEW_VENDOR_ROWS]
    vendor_ids = [v.id for v in shown]

    requirement_counts = dict(
        db.query(Requirement.vendor_id, func.count(Requirement.id))
        .filter(Requirement.vendor_id.in_(vendor_ids or [None]))
        .group_by(Requirement.vendor_id)
        .all()
    )
    profile_counts = dict(
        db.query(ProfileVendorStatus.vendor_id, func.count(ProfileVendorStatus.id))
        .filter(ProfileVendorStatus.vendor_id.in_(vendor_ids or [None]))
        .group_by(ProfileVendorStatus.vendor_id)
        .all()
    )

    return {
        "month_label": f"{_MONTHS[month_start.month - 1]} {month_start.year}",
        "since": month_start.isoformat(),
        "count": len(vendors),
        "vendors": [
            {
                "vendor_id": str(v.id),
                "name": v.name,
                "contact_person": v.contact_person,
                "contact_email": v.contact_email,
                "created_at": (as_aware(v.created_at).isoformat() if v.created_at else None),
                "requirements": requirement_counts.get(v.id, 0),
                "profiles": profile_counts.get(v.id, 0),
                "is_active": bool(v.is_active),
            }
            for v in shown
        ],
        "monthly": _vendors_per_month(db, now),
    }


def _vendors_per_month(db: Session, now: datetime) -> list[dict]:
    """Vendors added per calendar month, oldest first, this month marked."""
    first_month = _month_start(now)
    for _ in range(VENDOR_MONTHS - 1):
        first_month = _previous_month(first_month)

    per_month: Counter[str] = Counter()
    for (created_at,) in db.query(Vendor.created_at).filter(Vendor.created_at >= first_month):
        moment = as_aware(created_at)
        if moment is not None and moment >= first_month:
            per_month[f"{moment.year:04d}-{moment.month:02d}"] += 1

    months, cursor = [], first_month
    for _ in range(VENDOR_MONTHS):
        key = f"{cursor.year:04d}-{cursor.month:02d}"
        months.append(
            {
                "month": key,
                "label": _MONTHS[cursor.month - 1],
                "count": per_month.get(key, 0),
                "is_current": cursor.year == now.year and cursor.month == now.month,
            }
        )
        # +32 days from the 1st always lands in the next month, whatever its length.
        cursor = _month_start(cursor + timedelta(days=32))
    return months


# ─────────────────────────────────────── where every profile stands, per vendor
def _pipeline_by_vendor(db: Session) -> dict:
    """One stacked bar per vendor, in the lifecycle's own order.

    This is the (person, vendor) model made visible: the same person can be one
    colour on one row and another colour on the next, which is what the unique
    constraint on ``profile_vendor_statuses`` exists for. Ordered by volume —
    vendors are a business unit, and ordering them is not the candidate ranking
    sections 7 and 42 rule out.
    """
    counts = (
        db.query(
            ProfileVendorStatus.vendor_id,
            ProfileVendorStatus.status,
            func.count(ProfileVendorStatus.id),
        )
        .group_by(ProfileVendorStatus.vendor_id, ProfileVendorStatus.status)
        .all()
    )
    if not counts:
        return {"statuses": list(PIPELINE_ORDER), "rows": []}

    by_vendor: dict = defaultdict(dict)
    for vendor_id, status, count in counts:
        by_vendor[vendor_id][str(status)] = count

    names = dict(db.query(Vendor.id, Vendor.name).filter(Vendor.id.in_(by_vendor.keys())).all())
    rows = [
        {
            "vendor_id": str(vendor_id),
            "vendor_name": names[vendor_id],
            "total": sum(states.values()),
            "counts": {status: states.get(status, 0) for status in PIPELINE_ORDER},
        }
        for vendor_id, states in by_vendor.items()
        if vendor_id in names
    ]
    rows.sort(key=lambda row: (-row["total"], row["vendor_name"].lower()))
    return {"statuses": list(PIPELINE_ORDER), "rows": rows[:PIPELINE_VENDORS]}


# ──────────────────────────────────────── what vendors did with what we sent
def _outcomes_weekly(db: Session, now: datetime) -> list[dict]:
    """Sent, taken and turned down, per week, for the last eight.

    Deliberately not drawn as a funnel. This is a loop rather than a funnel — a
    rejection returns the profile to that vendor's pool and a lapsed hold does
    the same — so a narrowing shape would be a picture of a process the
    platform does not run. Three counts of what happened in a week is a claim
    that survives the loop.
    """
    this_week = _day_start(now) - timedelta(days=now.weekday())
    first_week = this_week - timedelta(weeks=OUTCOME_WEEKS - 1)

    actions = {
        "PROFILE_SUBMITTED": "submitted",
        "PROFILE_ENGAGED": "engaged",
        "PROFILE_REJECTED": "rejected",
    }
    buckets: dict[str, Counter[str]] = defaultdict(Counter)
    for action, created_at in db.query(AuditLog.action, AuditLog.created_at).filter(
        AuditLog.action.in_(list(actions)), AuditLog.created_at >= first_week
    ):
        moment = as_aware(created_at)
        if moment is None or moment < first_week:
            continue
        start = _day_start(moment) - timedelta(days=moment.weekday())
        buckets[start.date().isoformat()][actions[str(action)]] += 1

    weeks = []
    for offset in range(OUTCOME_WEEKS):
        start = (first_week + timedelta(weeks=offset)).date()
        counts = buckets.get(start.isoformat(), Counter())
        weeks.append(
            {
                "week_start": start.isoformat(),
                "label": f"{start.day} {_MONTHS[start.month - 1]}",
                "submitted": counts.get("submitted", 0),
                "engaged": counts.get("engaged", 0),
                "rejected": counts.get("rejected", 0),
                "is_current": start == this_week.date(),
            }
        )
    return weeks


# ──────────────────────────────────── what JDs ask for vs what the bank holds
def _skill_demand(db: Session) -> dict:
    """The technologies JDs demand, against the number of people who have them.

    Two counts on one row, which is the only way the gap is visible: a
    technology named in nine JDs and held by one person is a hiring decision,
    and neither number says that on its own. Mandatory demand only — a
    preferred skill nobody has is not a problem.

    ``unmatched`` underneath is the harder version of the same gap: skills a JD
    asked for that matched no technology row at all, so no resume drawn from
    this bank could ever evidence them. They never appear in the paired bars,
    because there is nothing to pair them with.
    """
    demand = (
        db.query(
            RequirementTechnology.technology_id,
            func.count(func.distinct(RequirementTechnology.requirement_id)),
        )
        .filter(RequirementTechnology.importance == "MANDATORY")
        .group_by(RequirementTechnology.technology_id)
        .all()
    )
    rows: list[dict] = []
    if demand:
        top = sorted(demand, key=lambda pair: -pair[1])[:SKILL_ROWS]
        ids = [technology_id for technology_id, _count in top]
        names = dict(db.query(Technology.id, Technology.name).filter(Technology.id.in_(ids)).all())
        supply = dict(
            db.query(PersonTechnology.technology_id, func.count(func.distinct(PersonTechnology.person_id)))
            .join(Person, Person.id == PersonTechnology.person_id)
            .filter(PersonTechnology.technology_id.in_(ids), Person.is_active.is_(True))
            .group_by(PersonTechnology.technology_id)
            .all()
        )
        rows = [
            {
                "technology": names[technology_id],
                "demand": count,
                "supply": supply.get(technology_id, 0),
            }
            for technology_id, count in top
            if technology_id in names
        ]

    unmatched: Counter[str] = Counter()
    for (skills,) in db.query(Requirement.unmatched_mandatory_skills):
        for skill in skills or []:
            label = str(skill).strip()
            if label:
                unmatched[label] += 1

    return {
        "rows": rows,
        "unmatched": [
            {"skill": skill, "count": count} for skill, count in unmatched.most_common(UNMATCHED_SKILL_ROWS)
        ],
    }
