# Open questions and the assumptions made in their absence

Every item below is a place where the requirements document was silent or
ambiguous and I had to choose something to keep building. Each says what I did
and where to change it. Take this list to your team lead.

**Item 1 needs an answer before this touches real candidate data.** The rest can
be settled as you go.

---

## 1. Section 17 — attaching a project to a different company

The document asks for the ability to compose `Project 1 → Company C` when the
pool says `Project 1 → Company A`. Read one way this is routine: a person
employed by a services firm delivers a project for an end client, and which
entity the resume names is a genuine question with a legitimate answer. Read
another way it means presenting work as having been done somewhere it wasn't,
which is misrepresentation to a third party who is relying on that document to
make a hiring decision.

The document does not say which is meant. I built the first reading and made the
second one impossible to do by accident:

- `Project.company_id` remains the company where the work was actually delivered.
- `ProjectCompany.relationship_type` (`EMPLOYER` / `END_CLIENT` / `PARTNER` /
  `BILLING_ENTITY`) models the real multi-company case honestly.
- `ResumeSelectedProject.display_company_id` holds the resume-specific
  attribution, and `display_company_reason` is **required** — the API rejects the
  change without one.
- Any mismatch raises a CRITICAL `COMPANY_REATTRIBUTED` warning and writes an
  audit entry naming the source company, the displayed company, the reason and
  the user.

**Ask:** is re-attribution meant for real delivery relationships (employer vs end
client), or for presenting work as done at a company where it wasn't? If it's the
first, the current design is right. If it's the second, that decision needs to
come from whoever owns the legal exposure, in writing, not from the two of us.

## 2. Section 24 — "the AI is allowed to generate new claims"

Section 24 permits generated claims; section 24 also requires warnings for
problematic ones and section 18 leaves responsibility with the user.

**Assumed:** the model grounds every fact in the pool and puts anything it can't
ground into a separate `unsupported_additions` list, which becomes an
`UNGROUNDED_CLAIM` warning the user explicitly keeps or modifies. Nothing
ungrounded is silently written into the resume body. See
`app/prompts/templates.py`.

**Ask:** is that the intended reading, or should the model write freely and rely
on the warnings alone?

## 3. Sections 8 vs 33 — two overlapping state machines

Section 8 gives profile states; section 33 gives submission statuses. They
overlap and the document never reconciles them.

**Assumed:** the submission status is authoritative and drives the profile
status through `SUBMISSION_TO_PROFILE_STATUS` in
`app/common/enums/submission_status.py`. SUBMITTED/UNDER_REVIEW/INTERVIEW/
SELECTED → ENGAGED; REJECTED → REJECTED; RELEASED → RELEASED.

## 4. Section 30 — what "available again" means after rejection

**Assumed:** the stored status stays `REJECTED` so the history remains visible in
the candidate list, and `ProfileVendorStatusEnum.is_selectable` treats REJECTED
as choosable. No manual restoration, as the section requires.

**Ask:** should the UI show "Rejected — available again", or plain "Available"?

## 5. Section 31 — reservation expiring mid-edit

The document says an expired reservation frees the profile but not what happens
to the half-finished resume.

**Assumed:** the session survives; edits return HTTP 409 `reservation_expired`
with a message telling the user to extend. `POST /reservations/{id}/extend`
exists for exactly this. Nothing is discarded.

## 6. Client vs vendor

Section 7's table has a "Current Engagement" column reading "Client A — GenAI",
but clients are never modelled — only vendors.

**Assumed:** a free-text `current_engagement_label` on `ProfileVendorStatus`.
If clients need to be first-class (reporting, "who is at Client A"), that's a
new entity and should be decided now rather than after data accumulates.

## 7. Concurrent reservation attempts

**Assumed:** first writer wins; the second gets 409. There is a unique
constraint on `(person_id, vendor_id)`. Under PostgreSQL with real concurrency
you'll want `SELECT ... FOR UPDATE` in `availability.create_reservation`.

## 8. Multiple candidates per requirement

**Assumed:** allowed — nothing prevents several sessions against one requirement.
Confirm this is wanted.

## 9. Who marks a submission SUBMITTED

Approval does not send anything (section 23 forbids automatic sending). Someone
must mark it sent.

**Assumed:** a human calls `POST /submissions/{id}/status` after emailing the
vendor. That is the moment the profile leaves the vendor's pool.

## 10. Smaller ones

- **Template format** — assumed JSON layout definitions, not uploaded .docx files.
  If the org has branded Word templates, this changes the rendering layer.
- **Which LLM / does PII leave the network** — assumed a hosted API is
  acceptable. Resumes contain personal data; if that's not acceptable the
  provider abstraction in `app/services/llm/` takes a local model.
- **Pool seeding** — assumed manual entry via the API. If existing resumes need
  importing, that's a parser project of its own.
- **Success metric** — no baseline given. Worth capturing current
  minutes-per-resume before rollout so the improvement is measurable.
- **Technology release dates** — hardcoded in `scripts/seed.py` and in
  `ANACHRONISTIC_TERMS` in the validation engine. Fine for MVP; a maintained
  source would be better long-term.
