"""Sections 8/33 — the one status vocabulary.

Availability is keyed on (person, vendor), never on profile type: the same
person can be AVAILABLE to one vendor and ENGAGED with another.

**There is exactly one set of statuses in this platform**, and it lives here.
The composition session, the submission and the (person, vendor) pair all speak
it, so the History tab and the Vendor tracking module cannot describe the same
situation in two vocabularies — which is what happened when each had its own.

    AVAILABLE  nobody holds this person for this vendor
      │  someone selects them for a requirement
      ▼
    EDITING    a resume is being composed. The hold is real — nobody else may
               select them — but nothing has been committed.
      │  the resume is approved
      ▼
    RESERVED   an approved resume exists and is held for this vendor while
               somebody decides what to do with it. A clock runs on this: if
               nobody acts, the hold lapses back to AVAILABLE.
      │  it is sent to the vendor — the only move from a reserved hold
      ▼
    SUBMITTED  it has gone to the vendor, and now there is a question out
      │        there that only they can answer
      ├─▶ REJECTED  they said no
      └─▶ ENGAGED   they took the person
            │  we let them go
            ▼
          RELEASED

The chain is strictly in that order. ENGAGED and REJECTED are *answers*, and
nothing can be answered before it was asked, so neither is reachable from
RESERVED — the resume has to have gone out first. Likewise RELEASED follows
only ENGAGED: you can only let go of somebody who was actually taken.

REJECTED and AVAILABLE are the ways back into the candidate list: a rejection
returns the profile to this same vendor's pool with no manual restoration, and
a lapsed hold does the same automatically. ENGAGED and RELEASED do not return
on their own — a released profile is a closed chapter with that vendor — but a
release can be reopened deliberately from the vendor's own screen
(``vendor_tracking.reopen_released_pair``) when somebody decides to put that
person back in front of that vendor.

A lapsed hold returns to AVAILABLE rather than to a state of its own, and the
session it belonged to goes back to EDITING — the composition is still good, and
editing it takes the hold again.
"""

from __future__ import annotations

from enum import StrEnum


class ProfileVendorStatusEnum(StrEnum):
    AVAILABLE = "AVAILABLE"
    EDITING = "EDITING"
    RESERVED = "RESERVED"
    SUBMITTED = "SUBMITTED"
    ENGAGED = "ENGAGED"
    RELEASED = "RELEASED"
    REJECTED = "REJECTED"

    @property
    def is_selectable(self) -> bool:
        """States in which a profile may be picked for a new requirement.

        Section 30: a REJECTED profile becomes available to that same vendor
        again with no manual restoration, so REJECTED is selectable.

        RELEASED is deliberately NOT selectable: it means the person was
        engaged and then let go, which is a closed chapter with this vendor,
        not a fresh opening. ENGAGED, SUBMITTED, RESERVED and EDITING are the
        other states where somebody is holding — or has already decided
        something about — the person.
        """
        return self in (
            ProfileVendorStatusEnum.AVAILABLE,
            ProfileVendorStatusEnum.REJECTED,
        )

    @property
    def holds_the_profile(self) -> bool:
        """True while this status locks the person out of other requirements."""
        return not self.is_selectable


#: The statuses an operator may set by hand from the vendor tracking view.
#: Deliberately not every member: EDITING, RESERVED and AVAILABLE are
#: consequences of composing/approving a resume or of a hold's clock running
#: out, and typing them in would assert something no resume or timer backs up.
#: The four below are decisions only a human can make — and even these are
#: only valid from specific current states; see MANUAL_TRANSITIONS.
MANUALLY_SETTABLE: tuple[ProfileVendorStatusEnum, ...] = (
    ProfileVendorStatusEnum.SUBMITTED,
    ProfileVendorStatusEnum.ENGAGED,
    ProfileVendorStatusEnum.RELEASED,
    ProfileVendorStatusEnum.REJECTED,
)

#: For each status a human may set by hand, the current statuses it may
#: legally be set FROM. This is the dependency chain: a decision is only
#: meaningful in the state it follows from, so "ENGAGED" typed against an
#: AVAILABLE pair (nobody ever put them forward) or "RELEASED" typed against a
#: merely-RESERVED pair (nobody ever took them) is refused rather than silently
#: accepted. Anything not a key here (AVAILABLE, EDITING, RESERVED, and the
#: terminal ENGAGED/RELEASED/REJECTED as a *source*) has no manual next step.
MANUAL_TRANSITIONS: dict[ProfileVendorStatusEnum, tuple[ProfileVendorStatusEnum, ...]] = {
    ProfileVendorStatusEnum.SUBMITTED: (ProfileVendorStatusEnum.RESERVED,),
    # ENGAGED and REJECTED are answers, and there is nothing to answer until
    # the resume has actually gone out. Both used to be reachable straight
    # from RESERVED, which let a pair be marked "they took the person" while
    # the platform's own record said nobody had ever sent them anything —
    # three decisions offered at once for a question only one of them fits.
    ProfileVendorStatusEnum.ENGAGED: (ProfileVendorStatusEnum.SUBMITTED,),
    ProfileVendorStatusEnum.REJECTED: (ProfileVendorStatusEnum.SUBMITTED,),
    ProfileVendorStatusEnum.RELEASED: (ProfileVendorStatusEnum.ENGAGED,),
}
