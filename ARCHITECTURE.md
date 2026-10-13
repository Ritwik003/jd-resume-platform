# Architecture

## The three layers (section 4)

```
MASTER DATA          Person, PersonProfile, ProfileType, Technology,
(reusable pool)      PersonTechnology, Project, Company, Vendor, Employment
       |                        read-only during a resume run
       v
OPERATIONAL DATA     Requirement, ProfileVendorStatus, ProfileReservation
(demand + state)
       |
       v
GENERATED DATA       ResumeCompositionSession, ResumeSelected{Project,Company,
(per requirement)    Technology}, ResumeVersion, ResumeFile, Submission,
                     AIInteraction, ValidationWarning
```

**The rule that matters:** arrows point downward only. A resume session reads the
pool and writes its own selections and versions; it never mutates master data.
Section 17 states this directly and it is what makes the pool reusable rather
than something that degrades with every resume produced.

## Request flow for the core workflow

```
paste JD ──> jd_parser ──> Requirement (+RequirementTechnology, importance
                            stored here, never on the Technology master)
                                    |
select vendor ──> availability.list_profiles_for_vendor
                  (filters on vendor status; no ranking, per section 7)
                                    |
select person ──> availability.create_reservation ──> ResumeCompositionSession
                                    |
                  resume_composer.generate_base_content
                    1. autoselect projects/companies/technologies
                       (4-5 projects and 4-5 companies, ranked against the JD)
                    2. build_pool_content  -> deterministic skeleton
                    3. LLM pass            -> summary + descriptions only
                    4. ground_rules        -> one page, no education, no bars,
                                              timeline meets the JD's minimum
                                    |
                            ResumeVersion V1  (content_json)
                                    |
                  validation_engine.run_validation -> ValidationWarning rows
                                    |
studio ──> chat_edit ──> resume_tools.apply_tool ──> ResumeVersion V2, V3...
                                    |
approve ──> render_files (docx + pdf from the same content_json)
        ──> Submission (APPROVED)
        ──> ProfileVendorStatus = RESERVED, on a clock, for that vendor
                                    |
vendor tracking ──> vendor_tracking.profiles_for_vendor
                    (per (person, vendor): status + newest session + submission)
        ──> operator sets ENGAGED / RELEASED / REJECTED
        ──> or the hold lapses ──> EXPIRED, and the profile is offerable again
```

## The two state machines, and why they stay apart

`SubmissionStatus` is about one document's reception by a vendor — submitted,
under review, interview, selected. `ProfileVendorStatusEnum` is about whether we
may offer that person to anybody else. They overlap but are not the same
question, and `app/utils/enums/submission_status.py` holds the one authoritative
mapping from the first to the second. The vendor tracking module shows both, in
two tables, on purpose.

## Why the resume is structured data

`ResumeVersion.content_json` holds a `ResumeContent` (see
`app/schemas/resume_content.py`), not prose. Three consequences:

1. **Preview, DOCX and PDF cannot diverge** — each is a projection of the same
   object through the same layout definition.
2. **Agent edits are deterministic** — "move AWS higher" is a list reorder, not a
   regeneration that might change five other things.
3. **Versions are diffable** — every change is a new row, so "reject the AI's
   change" is a restore, not an undo, and the rejected attempt stays in history.

## The agent loop

```
user message
  -> EDIT_AGENT_SYSTEM prompt (includes current resume state,
     the person's real technologies, and the available project pool)
  -> model returns tool calls
  -> apply_tool() mutates a *clone* of the content
  -> if anything applied: new ResumeVersion + re-run validation
  -> AIInteraction row records the message, the tool calls and the version
```

The model never emits a resume. It emits function calls. Adding a capability
means adding a function to `TOOL_FUNCTIONS` and a schema to `TOOL_SPECS` in
`app/services/resume_tools.py`.

## Validation philosophy (section 25)

Warn, never block. Every rule is deterministic and cites its evidence — the
dates it compared, the release year it checked — because a warning the user
can't verify is one they learn to click through. Approval succeeds with open
warnings, but the count and the full list are written to the audit log, so
"approved with three unresolved warnings" is a fact someone can look up later.

## Extension points

| Want to... | Change |
|---|---|
| Add a resume template | `INSERT` into `resume_templates` (or `POST /resume-templates`) |
| Teach the chatbot a new edit | add to `TOOL_FUNCTIONS` + `TOOL_SPECS` |
| Add a validation rule | add a `_check_*` function to `validation_engine.py` and list it in `run_validation` |
| Swap the LLM | set `LLM_PROVIDER`; add a class in `app/services/llm/` if it's a new vendor |
| Move files to S3 | replace `app/services/storage.py` (path in, path out) |
| Add a new entity | `app/models/`, then register in `app/model_registry.py` |
