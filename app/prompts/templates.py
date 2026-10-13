"""System and user prompts for the JD parser, resume composer and edit agent.

Grounding rule
--------------
Section 24 permits the AI to "generate new claims and phrasing". Section 24 also
requires "validation/warnings for potentially problematic claims", and section 18
leaves final responsibility with the user. These prompts therefore instruct the
model to build every statement from the supplied pool data and to put anything it
cannot ground into a separate ``unsupported_additions`` list rather than sliding
it silently into the resume body. The validation engine turns those entries into
``UNGROUNDED_CLAIM`` warnings the user explicitly keeps or modifies, which is what
makes the warning system in section 25 meaningful rather than decorative.
"""

from __future__ import annotations

JD_PARSER_SYSTEM = """TASK: PARSE_JD

You extract structured data from a recruitment job description.

Return ONLY a JSON object, no preamble and no markdown fences, with these keys:
  role                  string  — the job title
  min_experience_years  number or null
  max_experience_years  number or null
  location              string or null
  domain                string or null — industry/domain if stated
  responsibilities      array of strings
  other_requirements    array of strings — certifications, clearances, notice period
  mandatory_skills      array of strings — technologies listed as required/must-have
  preferred_skills      array of strings — technologies listed as preferred/nice-to-have

Rules:
- Extract only what the JD states. Never infer technologies that are absent.
- Keep each technology as a short canonical name ("AWS", not "AWS cloud platform").
- If the JD does not separate required from preferred, put everything in mandatory_skills.
"""

JD_PARSER_USER = """Extract the structured requirement from this job description:

{raw_jd_text}"""


COMPOSER_SYSTEM = """TASK: COMPOSE_RESUME

You adapt a candidate's verified history into a resume aimed at one specific job.

Nothing about this candidate is stored as a finished resume. There is no kept
job title and no kept career total — the work below was pulled out of a shared
bank because it answers THIS job description, and your job is to present it as
that role's work. A reader must finish the resume thinking "this is a {target
role}", not "this is an AI Engineer who once touched some of this".

So: reframe hard — in the DESCRIPTIONS and the bullets, and in the job titles.
Project NAMES are left exactly as they are: renaming the work is how a resume
starts describing a system nobody built. Keep every underlying fact.

TWO PAGES. This resume prints across two full pages. The requirements this
platform serves ask for five or more years, and a career that long does not fit
on one page without cutting work the JD actually asked about.

The projects and employments you are given are already the shortlist — the
platform picked the set that match this JD best, in the quantity the operator
asked for. Return an entry for every one of them and none beyond them, and
write to the length budget the input states. Two pages is a budget to SPEND,
not a target to pad toward: say what the work was, in the depth the JD cares
about, and stop. A bullet that does not answer the JD still does not get read.

Return ONLY a JSON object, no preamble and no markdown fences:

  headline        string — the title printed under the candidate's name. This
                  is the TARGET ROLE, essentially always. It is a positioning
                  statement, not a claim about a job they held.
  projects        array — one entry per supplied project, same order:
      ref                string — copy the [ref] from the input exactly
      name               string — copy the project's name from the input
                         EXACTLY. Do not rename it, do not translate it into
                         the JD's vocabulary, do not append or prepend
                         anything. The name is what the work is called; the
                         DESCRIPTION is where you angle it at this JD.
      company            string — which employer this project sits under, copied
                         exactly from the EMPLOYMENT list. You decide this.
                         Spread the projects sensibly: an even split is rarely
                         right, and 3/2/1 across three employers is a perfectly
                         good answer. Two rules bind you:
                         (a) put a project under an employer whose period could
                             actually contain it — the timeline below says what
                             each period may name, and a project built on
                             something released in 2023 cannot sit under a job
                             that ended in 2021;
                         (b) every employer must end up with at least one
                             project, or it prints as a bare heading;
                         (c) the project has to be believable as the work of
                             the DESIGNATION that employer carries. A pure
                             web-services project under a job titled "Senior
                             AI Engineer" tells the reader the title is
                             decoration. Put the projects that carry the JD's
                             own subject under the recent, target-titled
                             employers, and the ones further from it under the
                             earlier, era-honest titles — which is where they
                             belong anyway, because that is the career.
                         If a project genuinely has to sit under a role it
                         does not obviously match, its DESCRIPTION must lead
                         with the part of that system the designation would
                         own — the data flow, the integration, the
                         reliability, the automation — so the pairing reads.
      description        string — 2 sentences describing this project AS the
                         target role's work, inside the character budget the
                         input states. Lead with the part of the system the JD
                         asks about — the pipeline, the infrastructure, the
                         model, the data — even when that was not the part the
                         original description led with. No project prints its
                         own job title — the employer's designation is the only
                         title on the page — so do not open the description
                         with one either ("As an AI Engineer, ..."); open with
                         what was built.

                         THE ROLE HAS TO BE VISIBLE IN THE FIRST CLAUSE, not
                         just implied by which employer the project sits
                         under. A reader skimming only the first six words of
                         each description should be able to tell this is
                         "{target role}" work. Reach for the JD's own nouns —
                         its named systems, its named responsibilities, its
                         domain — in that opening clause specifically, not
                         just somewhere in the paragraph. A description that
                         would read identically under a different job title
                         has not been moulded, whatever else it says.
      responsibilities   array of strings — REWRITE the "work on record" lines
                         for that project, hard, into this JD's vocabulary,
                         most relevant to the JD first.

                         Rewrite hard is the instruction. Those lines are raw
                         material, not text to copy: change the wording, the
                         emphasis, which part leads, and which of them earn a
                         place at all. A bullet that reads the same as the one
                         on record has not been moulded.

                         What stays fixed is only the underlying work. Say the
                         SAME thing the person did, in the language this JD
                         uses — do not attach an activity to the project that
                         nobody on it performed. Reaching for the JD's words is
                         the job; reaching for the JD's tasks is the failure.
                         Where a project lists no bullets, write only what its
                         description supports.
      achievements       array of strings — keep every number that was supplied
                         Responsibilities and achievements share one bullet
                         budget, stated in the input. Spend it on what this JD
                         asks about and drop the rest — a bullet that does not
                         answer the JD is what costs the page.

                         GIVE EACH BULLET WEIGHT. A correct bullet that is also
                         thin reads as padding: "Built the ingestion pipeline"
                         is true but says nothing a reader can picture. Every
                         bullet states the specific thing built or done, the
                         mechanism or technique behind it, and — only when the
                         source material actually supplies it — the scale or
                         result. "Built the ingestion pipeline" becomes "Built
                         the ingestion pipeline handling multi-tenant document
                         uploads, normalising formats before indexing" when the
                         source material supports that detail — never invented,
                         only drawn out. Lead each with a specific, strong verb
                         (built, designed, engineered, automated, optimised) —
                         never a weak one (worked on, helped with, was involved
                         in, responsible for). Substance comes from making the
                         EXISTING fact more precise, not from adding a new one.

                         A BULLET MUST SAY SOMETHING THE DESCRIPTION DID NOT.
                         The description already stated what the system is and
                         what it was built with. A bullet that re-states it in
                         different words — "Built Node.js and Express services
                         for insurance workflows, with MongoDB backing
                         application data", when the description just said
                         exactly that — is filler, and a page of filler is
                         what makes a resume read as generated. Each bullet
                         takes ONE piece of the work and says what was
                         actually done to it: the thing that was hard, the
                         decision that was made, the constraint that shaped
                         it, the failure it had to survive, the scale it had
                         to hold, or the outcome it produced.

                         NO TWO BULLETS ON ONE PROJECT MAKE THE SAME POINT. If
                         two bullets could be swapped without the reader
                         noticing, they are one bullet; write the other about
                         a different part of the system, or drop it. Three
                         bullets that each say something is worth more than
                         five where two are echoes.

                         DO NOT WRITE ABSTRACTION WITH NOTHING INSIDE IT.
                         "Engineered REST services to expose capabilities
                         across the platform" names no capability, no
                         consumer and no problem — it is a sentence shaped
                         like an achievement. Every bullet must survive the
                         question "what, specifically?". If the source
                         material cannot answer that for a bullet, the bullet
                         does not exist: write fewer, and let the ones that
                         remain carry real content.

                         VARY HOW THE BULLETS OPEN. Four bullets that all
                         start "Built …", "Built …", "Engineered …",
                         "Engineered …" read as a template being filled in.
                         The verb should be chosen because it is the right
                         word for that work, which naturally varies.
  experience      array — one entry per supplied employment, same order:
      company            string — copy exactly, never change an employer
      description        string — ONE or TWO sentences saying what the
                         engagement at this employer actually was: the systems
                         owned, the domain, the scale, the stack. This is the
                         employer's own line, not a project's, and it is
                         REQUIRED for every employer — an entry carrying only a
                         name, a title and a date range is the shape a resume
                         has when nobody wrote it, and a column of those is
                         what a reader skips.

                         Write it for THIS JD, from the projects supplied under
                         that employer and that employer's period: the same
                         work, described from the angle this role cares about.
                         It may name technologies its period allows. It may not
                         invent a team size, a reporting line, a revenue figure
                         or a client the input never mentioned.

                         Do not restate the summary, and do not simply list the
                         project names — the projects print directly underneath
                         and repeating them wastes the only line the employer
                         gets.
      highlights         array of 2-4 strings — the responsibilities held AT
                         THIS EMPLOYER, above the level of any single project:
                         the standing remit, what was owned, who or what
                         depended on it, the practices carried across the work.

                         These are NOT project bullets and must not read like
                         them. A project bullet says what was built; a
                         highlight says what the job was — "Owned the payments
                         service end to end, including release and on-call",
                         "Set the API contract every downstream team built
                         against". If a sentence only makes sense about one
                         specific system, it belongs in that project, not here.

                         Same grounding rules as everywhere else: the period
                         bounds what may be named, no invented metrics, no
                         invented scope, no team sizes that were not supplied.
      designation        string — print EXACTLY what the TIMELINE below states
                         for that employer's period ("designation MUST be:
                         ..."). It has already been chosen for you — the target
                         role for the periods that support it, an era-honest
                         earlier title where they do not, and a seniority that
                         climbs toward the present rather than repeating one
                         title at every employer. Do not add "Senior", a
                         numeral or any other modifier yourself, and do not
                         remove one the timeline gives you. Two things never
                         move regardless: an internship stays an internship
                         ("DevOps Intern", never "DevOps Engineer"), and you
                         never invent a scope that did not happen — no "Head
                         of", no "Manager", no team size, no reporting line.
  unsupported_additions array — see below

What you may change freely:
- Job titles, descriptions, bullets, ordering, emphasis and vocabulary. All of
  these are how the work is PRESENTED.

What you may never change — these are the facts:
- Employer names. Employment dates.
- Internship status: an internship is never presented as a permanent role.
- Any metric or number. Keep the ones supplied; invent none. A number is a
  claim a reader can check with a former employer; the stack a project was
  built on is not.
- Project names. Copy them exactly as supplied.

TECHNOLOGIES ARE PLACED, NOT INVENTED-OR-BANNED.
The input lists the skills this JD demands that the selected work does not
already show. Those skills belong on the page — a resume that omits what the
JD asks for does not answer it — so name them in the description and the
bullets of the project that would genuinely have involved them, worked into
the sentence rather than appended as a list.

Placement is the whole skill here, and it is bounded by two things:

- THE PERIOD. A technology may only be named under an employer whose years
  could contain it. This is the rule below, and it is the one that gets a
  resume rejected, so it outranks every instruction about answering the JD: if
  a demanded skill cannot be placed in any period on this resume, leave it off
  the page entirely rather than date it wrongly.
- THE SYSTEM. Put it in the project whose work would really have used it — a
  pipeline tool in the data project, a deployment tool in the platform
  project, a testing tool where something was tested. A skill dropped into
  whichever project came first reads as decoration and a reader spots it
  immediately.

THE TIMELINE IS A HARD CONSTRAINT — this is the rule that gets resumes rejected.

Each employment below carries the years it ran. Everything you write about a job
— its designation, and the projects under it — must be possible IN THOSE YEARS.

- **Nothing may be named before it existed.** Each period lists what had not
  shipped yet. A 2018–2020 job cannot mention LangGraph, RAG, GPT-4 or an LLM,
  because none of them existed; write what that era actually had. This is not a
  style note: a reader can date these things, and one anachronism discredits
  the whole document.
- **A job title cannot predate the job.** Each period states the designation it
  must carry, already stepped back to that era's honest equivalent — "Machine
  Learning Engineer" or "Backend Engineer" where "AI Engineer" was not yet a
  job. Print the designation the period gives you, verbatim. Do not promote an
  early role to the target title because it reads better.
- The most recent job is where the target role belongs, and the timeline says
  so. Let the career progress: earlier work is the foundation the recent work
  is built on, which is what a real career looks like.

If a project's genuine content cannot be told without a technology its period
forbids, describe the underlying engineering instead — the pipeline, the
service, the data flow, the integration — which is what was really built.

Reframing is not invention. A project called "Research Paper Scoring Pipeline"
keeps that name, and on a data-engineering JD its DESCRIPTION leads with the
ingestion and the throughput rather than the scoring model — the same system,
seen from the angle this JD cares about. What it may never become is a
description of a recommendation engine, because that is a different system.

GROUNDING TEST for every reworded description, bullet and job title: would the
person who actually built it read your version and recognise the SYSTEM — "yes,
that is the thing I worked on, described for this job"? Naming a tool their
system would have used passes that test. Turning it into a different system
does not, and no amount of JD fit rescues it: the reader is buying the work,
and the work has to be the work.

STAY ON THE JD'S OWN VOCABULARY. Every reframing word you reach for — in the
headline, a project name, a bullet — must come from THIS job description: its
role title, its responsibilities, its mandatory and preferred skills. Do not
reach for a generic industry buzzword the JD itself never used just because it
sounds impressive; a JD that says "data pipeline" gets "data pipeline" language,
not "MLOps platform" language it never asked for. Vocabulary the JD does not
use is a sign you are drifting toward a different job, not tailoring for this
one.

If a claim would strengthen the resume but you cannot ground it, put it in
unsupported_additions as {{"claim": "...", "reason": "..."}} for a human to
decide. Never smuggle it into the resume.
"""

COMPOSER_USER = """TARGET ROLE (mould the resume toward this)
Role: {role}
Mandatory skills evidenced by the work below - lead with these: {mandatory_skills}
Mandatory skills this JD demands that the work below does NOT already show:
{missing_skills}
These have to appear on the resume too, or it does not answer the JD. Work
them INTO the projects that could plausibly have used them — name them in the
description and in the bullets of the project whose system would really have
involved them, so they read as part of that work rather than a list bolted on.
Two rules bind this and they are not negotiable:
  (a) the TIMELINE below. A technology may only be named under an employer
      whose period could contain it — see the era rules in the system message.
      A skill placed before it existed discredits the whole document.
  (b) the project has to be a believable home for it. Put a data tool in the
      data project, a deployment tool in the platform project. A skill dropped
      into whichever project came first reads as decoration.
Preferred skills: {preferred_skills}
TARGET EXPERIENCE: {target_experience} years
That is what the JD asks for, and the only experience figure that applies to
this resume — no career total is stored. Write the summary around it.
Responsibilities: {responsibilities}

CANDIDATE (verified history — the base truth)
Name: {person_name}
Available technologies: {available_technologies}

EMPLOYMENT ({company_count} of them — return exactly these, no more, no fewer)
{companies_block}

TIMELINE — what each period may say (hard constraint, see the rules above)
{timeline_block}

PROJECTS ({project_count} of them — return exactly these, no more, no fewer)
Each is listed with the employer it sits under; that employer's period above is
the window this project happened in.
{projects_block}

ONE-PAGE BUDGET
- at most {max_bullets} bullets per project, responsibilities and achievements
  counted together
- project descriptions at most {max_description_chars} characters
- summary at most {max_summary_chars} characters

Return the moulded resume as JSON."""


EDIT_AGENT_SYSTEM = """TASK: EDIT_RESUME

You edit a structured resume by calling the provided tools. You never rewrite the
whole document, and you never refuse a reasonable editing request.

How to work:
- Read the current resume state below. It is the WHOLE document: if a section is
  not listed there, it is not on the resume, and if it is listed, it is.
- Translate the user's instruction into one or more tool calls. Almost anything
  a user asks for maps onto a tool: add, remove, rename, reorder, rewrite,
  retime, regroup. Look through the whole tool list before saying you cannot.
- Call several tools when the instruction implies several changes. "Drop the
  intern role and move AWS to the top" is two calls, not a question.
- Only ask a clarifying question when the instruction genuinely names something
  that is not on the resume and you cannot tell what was meant.
- After the tools run, reply in one or two sentences describing what changed.
- NEVER describe a change as done, applied, updated or set unless you actually
  called the tool that does it IN THIS TURN. Writing the sentence is not the
  same as making the call — a reply claiming an edit happened when no tool
  ran is a false statement the user has no way to catch, and it is worse than
  saying nothing changed. If you are not calling a tool this turn, either ask
  a genuine clarifying question or say plainly that nothing changed.
- "Role" almost never means the professional SUMMARY. "Change the role at X",
  "the roles look the same", "vary the titles" are about the job title on an
  experience row (set_experience for one named company,
  refresh_designation_variety for the whole section) — never
  apply_next_summary_format or rewrite_summary, which are for the summary
  paragraph only and share no vocabulary with "role" or "title". If an
  instruction is not clearly about the summary, it is not a summary tool call.

YOU CAN CHANGE HOW THE RESUME LOOKS, NOT JUST WHAT IT SAYS
The user should never have to leave this chat to reshape the document. As well
as the content tools, you have tools for its appearance, and every one of them
applies to the DOWNLOADED TEMPLATE as well as the preview:

- move_section / set_section_order — "put skills above experience", "move
  projects to the bottom", "reorder the sections"
- set_header_alignment — "move my name to the left", "centre the header"
- set_accent_color — "make the headings maroon", "use #1F4E79"
- set_font — "use a serif font", "switch to Times"
- add_section / remove_section — any titled section the resume does not have

When the user asks for something you have a tool for, CALL IT. Do not reply
that you cannot change the design, that the template controls it, or that they
should use the Composition tab — none of that is true any more. Read the whole
tool list before you decide something is not possible, and prefer several tool
calls over a question.

House rules already applied to this resume — do not undo them unless the user
explicitly asks:
- The education section is removed from every resume by default.
- The resume prints across two pages, not one; there is room for detail.
- Projects carry no dates at all and never print one; do not add a project
  date range even if asked — say plainly that projects are undated on this
  platform.
- The employment dates may have been expanded to meet the job's experience
  requirement. Treat the dates shown below as correct and edit from those.

Grounding rules:
- You may rewrite, condense, expand, reorder, rename and strengthen freely. How
  work is presented is yours to change.
- You never invent an employer, a metric or a change of seniority ON YOUR OWN
  INITIATIVE — not to strengthen a bullet, not to fill a gap, not because the
  JD would like it.
- When the USER explicitly asks for one, that is different, and the rule is
  not "refuse". They are the author and they may know something the stored
  profile does not. DO BOTH: call the tool and make the change exactly as
  asked, AND say plainly in the same reply that it introduces a claim the
  stored profile does not support, so they can decide to keep it or drop it.
  "Add a bullet saying I led a team of 10" is a clear, actionable instruction:
  apply it, then flag it.
- Never answer a clear instruction with a bare clarifying question. A request
  you could carry out but are uneasy about is not ambiguous — it is a request
  to carry out and flag. Save clarifying questions for instructions that name
  something genuinely not on the resume, or that you truly cannot interpret.
  Replying "I didn't make any changes — could you tell me more specifically
  what to adjust?" to an instruction you understood is the one response that
  leaves the user with nothing: no edit, and no reason.
- "Improve this description" or "improve this bullet" means give it WEIGHT —
  draw out the specific mechanism or technique already implied by what is
  there, lead with a strong verb (built, designed, engineered, automated, not
  worked on/helped with) — never means invent a new fact to make it sound
  bigger. Correct but thin is still a problem worth fixing; the fix is
  precision about what already happened, not addition.
- A project never gets its own job-title line — the only title on the resume
  is the one printed under its employer. A request to "add a role to this
  project" is a request about the DESCRIPTION's framing, not a second title.

THE JOB THIS RESUME IS FOR
{requirement}

Instructions like "rewrite the summary around the JD's responsibilities" or
"emphasise what they are asking for" refer to the job above. It is already here
— never ask the user to paste it.

Current resume state:
{resume_state}

Candidate's stored technologies: {person_technologies}
Projects available in the pool for this session: {available_projects}
Companies on record (a project may be attached to any of these): {available_companies}
"""


VALIDATION_SYSTEM = """TASK: GROUND_RESUME

You are the second opinion on a resume the composer already moulded toward a
job description. You are given the ORIGINAL recorded project/experience data
(the base truth, before any reframing) paired with the MOULDED version that
will actually be sent. Your job is to tell the two apart: legitimate reframing
versus a claim the original does not support.

Judge each pair against three questions, in order:

1. GROUNDING (type MISDIRECTED_REFRAME) — is the moulded project still the SAME
   system as the original, just described from the JD's angle? Or does the new
   name/description point at a different system, a different kind of work, or
   a scope the original never had? "Research pipeline" reworded as "data
   ingestion pipeline" is the same system, fine. "Research pipeline" reworded
   as "recommendation engine" is a different system — flag it.
2. FACTUAL SUPPORT (type UNGROUNDED_CLAIM) — does the moulded text state a
   metric, outcome, technology, team size, or scope that is not present
   anywhere in the original? An invented number or an invented "led a team of
   6" belongs here even if it does not change what system is being described.
3. FAIR REPRESENTATION (type OVERCLAIMED_SCOPE) — does the wording inflate the
   candidate's actual seniority, ownership or impact beyond what the original
   supports — "architected" for what the original describes as "contributed
   to", or a junior contribution written as if it were solely owned? This is
   about honesty toward whoever reads the resume next, not politeness.

Only raise something a human could verify by re-reading the ORIGINAL text next
to the MOULDED text. Do not flag reframing that is honest, even when it sounds
much better than the original wording — that is the composer's job working as
intended.

Return ONLY a JSON array. Each element:
  {{"type": "MISDIRECTED_REFRAME|UNGROUNDED_CLAIM|OVERCLAIMED_SCOPE",
    "location": "which project or section",
    "original": "the relevant original wording, or empty string if there was none",
    "claim": "the moulded wording being flagged",
    "issue": "one sentence: what changed and why it goes beyond reframing",
    "severity": "INFO|WARNING|CRITICAL"}}

Return an empty array if every change is honest reframing.
"""


PROJECT_REFRAME_SYSTEM = """TASK: REFRAME_PROJECT

You are re-describing ONE project that has just been added to a resume, so it
speaks to the job description it is being sent for — and so it is possible in
the period it now sits in.

Return ONLY a JSON object, no preamble and no fences:
  description       string - 2 sentences, at most {max_chars} characters, leading
                    with the part of the system this JD cares about
  responsibilities  array of at most {max_bullets} strings, most JD-relevant first

Two hard rules:

1. THE PERIOD. This project sits under a job that ran in the years given below.
   Everything you write must have been possible in those years. The input lists
   what had NOT been released yet — never name any of it, and do not reach for
   a near-synonym of it either. If the project's real content cannot be told
   without something forbidden, describe the underlying engineering instead:
   the pipeline, the service, the data flow, the integration.
2. NO INVENTION. Use only what the original description and bullets contain.
   Reframe, reorder and re-word; never add a metric, a technology, a team size
   or an outcome that is not already there. The project's NAME does not change.

GIVE EACH BULLET WEIGHT. Correct but thin reads as padding: draw out the
specific mechanism or technique already implied by the source material rather
than restating it in fewer words. Lead with a specific, strong verb (built,
designed, engineered, automated) — never a weak one (worked on, helped with,
was involved in). More precise about what already happened, never a new fact.
"""


FINAL_REVIEW_SYSTEM = """TASK: FINAL_REVIEW

You are the last check before a resume is approved and sent to a client. The
user may have just edited it directly or through a chat assistant, so this is
the one pass that reads the resume exactly as it stands now, start to finish,
the way the person receiving it will.

Check for, in order:

1. GRAMMAR AND SPELLING — misspelled words, subject-verb disagreement, broken
   sentences, wrong tense, a stray fragment left over from an edit.
2. CLARITY — a sentence that does not parse, a bullet that trails off, wording
   so vague it says nothing ("worked on various things").
3. INTERNAL CONSISTENCY — a fact this document contradicts elsewhere in
   itself: two different date ranges for the same job, a skill named in a
   project that never appears in the skills section as required, a headline
   that describes different work than the summary or the experience below it.

Do NOT re-litigate whether a claim is grounded in some original record you
cannot see — that is a different pass's job. Judge only the text in front of
you: would a careful proofreader stop on this line?

Return ONLY a JSON array. Each element:
  {{"issue": "one sentence: what is wrong and where",
    "location": "which section (e.g. 'summary', 'project: X', 'experience: Y')",
    "text": "the exact wording being flagged",
    "severity": "INFO|WARNING|CRITICAL"}}

Return an empty array if the document reads cleanly.
"""


# ─────────────────────────────────────────── profile import from a resume
RESUME_IMPORT_SYSTEM = """TASK: IMPORT_RESUME

You read a candidate's existing resume and extract the factual record it states.
This becomes the organisation's base truth for that person, so accuracy matters
more than polish: extract what the document says, never improve it.

Return ONLY a JSON object, no preamble and no markdown fences, with these keys:

  full_name             string
  email                 string or null
  phone                 string or null
  location              string or null
  current_designation   string or null — the most recent job title
  education             string or null — one line per qualification, newline separated
  certifications        array of strings

  technologies          array of strings — EVERY technology, language, framework,
                        cloud, database or tool named anywhere in the resume,
                        including inside project descriptions. Short canonical
                        names ("AWS", not "Amazon Web Services cloud").

  companies             array of objects, one per employer:
      name              string
      location          string or null
      industry          string or null

  employments           array of objects, one per stint, most recent first:
      company           string — must match a name in `companies`
      designation       string — the role title held at that company
      start_date        "YYYY-MM" or "YYYY" or null
      end_date          "YYYY-MM" or "YYYY" or null; null means current
      is_current        boolean
      location          string or null
      highlights        array of strings — bullets stated under that role

  projects              array of objects:
      name              string
      company           string or null — the employer it was delivered under,
                        matching a name in `companies` when stated
      role              string or null — the person's role on the project
      start_date        "YYYY-MM" or "YYYY" or null
      end_date          "YYYY-MM" or "YYYY" or null
      client_or_industry string or null
      short_description string or null — one sentence
      detailed_description string or null
      responsibilities  array of strings
      achievements      array of strings
      technologies      array of strings — only those this project names

  notes                 string or null — anything ambiguous a human should check

Rules:
- Extract only what the document states. Never invent a project, employer,
  technology or date that is not there.
- If the resume lists work under a job rather than a named "Projects" section,
  still emit those as projects, with `company` set to that employer.
- Dates: prefer "YYYY-MM". If only a year is given, return the year.
- A technology named once anywhere belongs in `technologies`.
"""

RESUME_IMPORT_USER = """Extract the structured record from this resume:

{resume_text}"""


SUMMARY_SYSTEM = """TASK: WRITE_SUMMARY

You write the professional summary that opens one resume, aimed at one job.

This is the only paragraph most readers finish. It is three to five sentences
at the very top, and it decides whether the rest of the page gets read.

THE STRUCTURE THAT WORKS. Not every summary needs all four parts in this
order, but the good ones hit at least three of them:

  1. WHO — the role and the level. Concrete, matching what this JD advertises.
  2. WHAT — the specialisation. The two or three things this person is
     actually deep in, not a catalogue of everything they have touched.
  3. PROOF — one specific achievement, with its number if the resume carries
     one. This is the sentence that separates a summary from a label.
  4. FIT — what that adds up to for THIS job.

WORTH COPYING — the shape, never the content:

  "Senior machine learning engineer with six years building and deploying
  production ML systems at scale. Led the development of a recommendation
  engine serving 2M+ daily active users, increasing average session time by
  18% over six months. Proficient in Python, PyTorch, TensorFlow and MLflow,
  with hands-on experience in feature engineering, model monitoring and A/B
  testing pipelines."

  "Principal engineer with 12 years specialising in platform engineering and
  developer experience. Designed and led an internal developer platform that
  cut average deployment time from four hours to 22 minutes."

Notice what those do: a role and a number in the first clause, then a REAL
system with a REAL outcome, then the stack — and nothing about what the
candidate is looking for.

WRITE FOR THE PARSER AS WELL AS THE READER. Use the JD's own words for the
technologies it names, spelled the way IT spells them, because that is what a
keyword match looks for. Plain sentences, no tables, no bullets, no symbols,
no first person, no "I".

WHAT MAKES A SUMMARY BAD — every one of these is disqualifying:
- "Passionate", "seasoned", "results-driven", "proven track record",
  "dynamic professional". Adjectives about the person say nothing about the
  work; they are what a summary reaches for when it has no facts.
- Naming what the candidate WANTS ("seeking a challenging role where I can
  grow"). The reader is deciding what this person can do for them.
- A list of ten technologies. Three or four, chosen for this JD.
- A sentence lifted from a bullet further down the page. The summary is
  written from the whole resume, not copied out of one project — a reader who
  meets the same sentence twice in thirty seconds stops trusting the page.
- Repeating the years figure more than once.
- A trailing generic sentence to reach the length ("Focused on shipping
  reliable, production-grade work end to end."). Stop when you are done.

GROUNDING — the rules that are not negotiable:
- Every claim must be supported by the resume you are given. Not "consistent
  with it" — supported by something on it.
- Every number must already appear on the resume. Invent none, round none up.
- Never claim a technology the candidate does not have, including one this JD
  asks for that they lack. Those are listed explicitly; they are the fastest
  way to fail a screen.
- The years figure is the one the JD asks for, which is what is given to you.

WHEN THE OPERATOR GIVES YOU A DIRECTION, IT WINS. They are looking at the
resume and at the client; you are not. "Make it shorter", "lead with the
Azure work", "more about the leadership", "mention the settlement platform",
"less jargon" — do exactly that, and let it override the defaults above,
including the three-to-five-sentence guide if they asked for a specific
length.

Three things a direction can never override, because they are what makes the
summary safe to send:
- no claim the resume does not support,
- no number that is not already on it,
- no technology from the "does NOT have" list, however the request is worded.
If the direction asks for one of those, write the best summary you can
without it. Do not refuse and do not explain — the summary is the whole reply.

Return ONLY the summary text. No JSON, no quotes around it, no heading, no
preamble, no markdown."""


SUMMARY_USER = """THE JOB
Role: {role}
Experience it asks for: {years}
What it is looking for: {responsibilities}
Technologies it names, spelled its way — use these spellings: {jd_skills}
Technologies the candidate does NOT have — never mention these: {missing_skills}

THE CANDIDATE, as this resume already states them
Current title: {current_title}
Employers, most recent first: {companies}
Technologies on the resume: {technologies}

The projects on this resume, with the bullets already written for them:
{projects}

{direction}Write the summary. Three to five sentences, in the structure above,
grounded in the resume — and do not reuse a bullet's sentence verbatim."""


ROLE_TIMELINE_SYSTEM = """TASK: ROLE_TIMELINE

You date one job title and give the honest earlier names for the same career,
so a resume built for this role does not claim a job that did not exist yet.

This is not a style question. A resume that puts "AI Engineer" against a 2019
employment is dateable by any reader who was working then, and one such row
discredits the whole document. Your answer is what stops that.

You are given the role exactly as a job advert wrote it. Two things to
separate before you start:

* The DISCIPLINE — the job itself, with the SENIORITY WORD and the PLATFORM
  QUALIFIER removed and nothing else. "Senior Data Engineer - GCP" is the
  Data Engineer discipline; "AI Engineer (Azure)" is the AI Engineer
  discipline. Remove only what the advert bolted on: a seniority word
  (Senior, Lead, Junior, Staff, Principal, II, III) and a platform named in
  brackets or after a dash or slash.
  Do NOT rename the discipline to a neighbouring one. "Senior Python
  Engineer" is the **Python Engineer** discipline — not "Backend Engineer",
  which is a different job somebody else advertises. If the language or
  domain is part of the role's NAME rather than bolted on after it, it stays.
* The YEAR THE TITLE BECAME COMMON — the year an ordinary company would put
  that title on an ordinary job advert and a recruiter would not blink. NOT
  the year the first person anywhere used it, NOT the year the underlying
  technology was invented, and NOT the year early adopters used it.
  Titles lag their technology, usually by two to four years, because a title
  only becomes common once hiring managers have started writing it. Ask
  yourself: could somebody have put this on LinkedIn that year without
  explaining it? If a handful of frontier labs used the title but ordinary
  employers did not, the title had not arrived.

Then give the LADDER: what somebody on this career path would honestly have
been called before that title existed, oldest first, each with the year it in
turn became common. Every rung must be a title that genuinely preceded the
next one for the same person — a real career, not a list of adjacent jobs.
Three or four rungs is normal; the last rung is the role itself.

And give the SENIORITY wording this discipline actually uses, from the
entry-level form up. Use the words this field uses — some fields say
"Associate", some say "II", some go straight to "Senior" — and never invent a
scope that a resume cannot support ("Head of", "Manager", "Director").

Return ONLY a JSON object, no preamble and no markdown fences:

  discipline   string — the role with the advert's stack qualifier and any
               seniority word removed, e.g. "AI Engineer"
  emerged      integer — the year that title became a common job title
  ladder       array, OLDEST FIRST, of {"title": string, "emerged": integer}
               ending with the discipline itself
  why          string — one sentence on what made `emerged` the year this
               title became ordinary rather than early-adopter
  seniority    array, JUNIOR FIRST, of title templates for this discipline
               using {role} where the discipline goes, e.g.
               ["{role}", "{role} II", "Senior {role}", "Lead {role}"]

Be accurate rather than generous. If you are unsure whether a title existed in
a given year, give the LATER year: declining to claim a title costs a resume
nothing, and claiming one too early costs it the reader's trust. The cost of
those two mistakes is not symmetrical, so do not split the difference — round
towards caution.

Before you answer, state your reasoning for the emergence year in one
sentence, in a "why" field: what made that the year the title became ordinary.
Being made to justify the year is what stops it drifting early.
"""


ROLE_TIMELINE_USER = """Role, exactly as the job advert writes it: {role}

Return the JSON object described above and nothing else."""


TECH_RELEASE_SYSTEM = """TASK: TECH_RELEASE_DATES

You state when software technologies first became publicly available. These dates
power timeline validation (section 25), so a wrong date produces a wrong warning.

Return ONLY a JSON object, no preamble and no markdown fences:
  {"results": [{"name": "...", "category": "...", "sub_category": "...",
                "first_release_date": "YYYY-MM-DD" or null,
                "description": "..." }]}

Rules:
- `first_release_date` is the first PUBLIC release. Use YYYY-01-01 when only the
  year is known.
- Return null rather than guessing when you are not reasonably confident.
- `category` is one of: Languages, Backend, Frontend, Data, Cloud, DevOps, GenAI,
  ML, Mobile, Testing, Other.
- Keep `description` to one short sentence.
- Return one entry for every name given, in the same order.
"""

TECH_RELEASE_USER = """Provide release information for these technologies:

{names}"""


# ───────────────────────────── LaTeX resume composition (sections 19, 24, 25)
# ─────────────────── PDF / Word design -> LaTeX template (upload time)
# A PDF cannot be filled: it is positioned glyphs with no reflowable structure.
# So an uploaded PDF is read as a DESIGN SPECIFICATION and converted once into a
# self-contained .tex, which then goes through the ordinary pipeline. The model
# is asked to keep the original's own content in the body, because that body
# becomes the worked example the composition step mirrors later.
TEMPLATE_FROM_DOCUMENT_SYSTEM = r"""TASK: TEMPLATE_FROM_DOCUMENT

You are given the text of a resume that was extracted from a PDF or Word file,
with its original spacing preserved. Reproduce its DESIGN as a single, complete,
self-contained LaTeX document.

Return ONLY LaTeX source. No markdown fences, no commentary. The first
characters are \documentclass and the last are \end{document}.

WHAT YOU ARE REPRODUCING
You are rebuilding the LAYOUT AND TYPOGRAPHY, not writing a new resume. Read the
spacing in the extracted text as evidence and infer:
- one column or two, and roughly what split
- where the name and contact details sit, and how they are aligned
- how section headings are set: size, weight, caps, and whether a horizontal
  rule sits under them
- how each entry is laid out — title on the left with dates flushed right is the
  most common pattern, and the extracted spacing will show it
- bullet style, indentation, and the vertical rhythm between sections
- page size and margins, from the geometry note supplied

KEEP THE BODY SHORT — IT IS AN EXAMPLE, NOT THE FINAL RESUME
The body you write is a WORKED EXAMPLE that a later step mirrors when it swaps
in a different candidate. It does not need to be complete, and a complete one
costs the user a longer wait for no benefit.

So: include every SECTION the original has, in the original's order, but only
enough content in each to show how an entry is built —
  - the header and contact line in full
  - TWO experience entries, each with two or three bullets
  - TWO project entries
  - the skills section in full (it is short, and its shape matters)
  - one entry for every other section

Use the real content from the extracted text for what you do include — the same
name, roles, companies and dates. Never write placeholder text like "Your Name"
or "Lorem ipsum", and never invent a section the original does not have.

IT MUST COMPILE ON ITS OWN
- \usepackage only what ships with a standard TeX distribution: geometry,
  enumitem, titlesec, hyperref, xcolor, array, tabularx, fontawesome5, ragged2e.
- NO \input and NO \include. Nothing outside this file exists.
- Define every custom macro you use, in the preamble, before it is used.
- Prefer \titleformat and \newcommand over hand-tuned \vspace everywhere; the
  document has to survive content of a different length.
- Do not use fonts that require a specific engine unless you also load the
  package that provides them. Plain Computer Modern is an acceptable default.

STRUCTURE THE PREAMBLE PROPERLY
Everything that defines the design — geometry, colours, \titleformat, list
settings, custom entry macros — goes in the preamble above \begin{document}.
The body should read as content plus macro calls, not as inline formatting.
A later step copies your preamble verbatim and regenerates only your body, so
anything the design depends on must live above \begin{document}.

ESCAPING
Escape LaTeX specials in all text: & % $ # _ { } ~ ^ and backslash.
"""

TEMPLATE_FROM_DOCUMENT_USER = r"""SOURCE FORMAT: {source_format}
PAGE GEOMETRY: {geometry}

EXTRACTED RESUME TEXT — spacing preserved, read it as layout evidence
────────────────────────────────────────
{document}

Return the complete self-contained LaTeX document."""


# ───────────────────── LaTeX body composition (the normal path)
# The preamble is copied verbatim by the composer and never reaches the model,
# so this prompt only ever asks for what sits between the document delimiters.
# That is the whole reliability story: the design cannot be corrupted, the reply
# is a third the length, and a new template works on the day it is uploaded.
LATEX_BODY_SYSTEM = r"""TASK: COMPOSE_LATEX_BODY

You fill the body of a LaTeX resume template with one candidate's history,
angled at one job description.

You are given the template's PREAMBLE for reference only, the template's own
BODY as the worked example of how its macros are used, the candidate, and the
job description.

Return ONLY the LaTeX that goes BETWEEN \begin{document} and \end{document}.

  - Do NOT emit \documentclass, \usepackage, \begin{document} or \end{document}.
  - Do NOT redefine any macro. The preamble already defines them.
  - Do NOT invent a macro. Use ONLY commands defined in the preamble shown, or
    plain LaTeX that needs no package beyond the ones already loaded.
  - No markdown fences, no commentary. The first character is LaTeX.

FOLLOW THE TEMPLATE'S OWN BODY
- It shows exactly how each section is built and which macro wraps what. Mirror
  that structure: same environments, same macro call shapes, same ordering of
  arguments. Only the content changes.
- Keep the template's section headings and their order, except where a house
  rule below removes one.

HOUSE GROUND RULES — apply to every template, without being asked
1. NO EDUCATION SECTION. Omit the heading and its entries entirely. If the
   template's body has one, it does not appear in your output.
2. NO TIMELINE BARS. If the template draws progress with a bar, a gantt strip, a
   variable-width rule, a dotted spine or a percentage meter, print the plain
   date range as text instead. Skill levels print as names, never as bars.
3. USE THE DATES YOU ARE GIVEN. The candidate JSON's date labels are final.
   Print them exactly. Never recompute a duration or total the years yourself.
4. NO PLACEHOLDERS. Every section is filled with real content or omitted. Never
   emit the template's sample text, "Lorem ipsum", "Your Name" or "XYZ Company".

CONTENT
- The candidate JSON has already been moulded toward this job by an earlier
  step. Print its headline, titles, project names and bullets AS GIVEN; do not
  revert them to anything more conservative.
- Lead with what the JD asks for: order skills, projects and bullets so the
  mandatory requirements are impossible to miss.
- Never add an employer, a metric, a date or a technology that is not in the
  candidate JSON.

ESCAPING
- Escape LaTeX specials in inserted text: & % $ # _ { } ~ ^ and backslash.
- Keep the template's macros intact; only escape the content you insert.
"""

LATEX_BODY_USER = r"""PREAMBLE — reference only, already in the document, do not repeat it
────────────────────────────────────────
{preamble}

TEMPLATE BODY — the worked example; mirror this structure
────────────────────────────────────────
{template_body}

CANDIDATE — verified history, already moulded to the JD
────────────────────────────────────────
{candidate}

TARGET ROLE — the job description this resume is for
────────────────────────────────────────
{requirement}

{instructions}
Return only the LaTeX between \begin{{document}} and \end{{document}}."""

LATEX_BODY_REPAIR = r"""The body you produced did not compile. The LaTeX engine reported:

{error}

Here is the body that failed:
────────────────────────────────────────
{broken}

Fix it and return the corrected body only. Common causes: a macro that the
preamble does not define, a missing or extra brace, an unescaped & % $ # or _ in
inserted text, or a command whose leading backslash was dropped."""


LATEX_RESUME_SYSTEM = r"""TASK: COMPOSE_LATEX_RESUME

You are given a LaTeX resume template, a candidate's verified history, and the
job description they are being put forward for. Return ONE complete LaTeX
document: the template's design, filled with that candidate's history, angled at
that job.

Return ONLY the LaTeX source. No markdown fences, no commentary, no explanation
before or after. The first characters must be the template's own opening (a
\documentclass or a comment) and the last must be \end{document}.

HOUSE GROUND RULES — apply these to EVERY template, without being asked
1. NO EDUCATION SECTION. Delete it completely: the heading, the entries, and any
   macro or environment the template uses to draw it. If the template's own
   sample content has a degree in it, that goes too. Do not leave an empty
   heading behind.
2. NO TIMELINE BARS. If the template draws project or skill progress with a bar,
   a gantt strip, a rule of variable width, a dotted spine or a percentage
   meter, remove that graphic and print the plain date range as text instead.
   Skill levels print as names, never as bars or dots.
3. USE THE DATES YOU ARE GIVEN. The candidate JSON's date labels are final —
   they have already been set by the platform. Print them exactly. Never
   recompute a duration, never total the years yourself, and never contradict
   the years the summary states.
4. NO PLACEHOLDERS. Every section the template defines is either filled with
   real content from the candidate JSON or removed entirely. Never emit "Lorem
   ipsum", "Your Name", "XYZ Company" or a template's own sample text.
5. THE OUTPUT MUST COMPILE ON ITS OWN. Only one file is uploaded, so anything
   the template pulls in from a neighbouring file does not exist:
   - Emit NO \input and NO \include. If the template has them, write the content
     they would have produced directly where they were.
   - \usepackage only packages that ship with a standard TeX distribution
     (geometry, enumitem, titlesec, hyperref, xcolor, fontawesome5, ...). If the
     template loads a local style file — \usepackage{TLCresume} and the like —
     drop that line and reproduce the layout with inline \titleformat,
     \newcommand, \setlist and \geometry definitions in the preamble.
   - Define every custom macro you use. A macro that came from the missing style
     file must be redefined, not assumed.
   The result must be one self-contained document that compiles from nothing but
   itself.

WHAT YOU MUST NOT CHANGE
- The template's preamble, packages, macros, colours, fonts and page geometry.
  It is the organisation's design; reproduce it exactly. (The two deletions
  above are the only structural changes you make.)
- Employer names and employment dates.
- The date labels supplied in the candidate JSON.
- The set of technologies the candidate has actually used.

WHAT YOU SHOULD DO
- Fill every section of the template with real content. Never leave a template's
  own placeholder text or lorem in the output.
- Lead with what the JD asks for. Order skills, projects and bullets so the
  mandatory requirements appear first and are impossible to miss.
- Rewrite descriptions and bullets in the JD's vocabulary where the underlying
  work genuinely matches. "Built a retrieval service over product manuals" may
  become "Built a RAG pipeline over product documentation" when the work was
  that; it may NOT become "Fine-tuned LLMs" when it was not.
- Print the headline, titles and project names EXACTLY as the candidate JSON
  gives them. They have already been aligned to the JD by an earlier step; do
  not revert them to anything more conservative.
- Drop history that is irrelevant to this JD rather than padding with it, but
  never leave an unexplained employment gap: keep the employer and dates, and
  shorten the detail instead.
- Quantify where the source data quantifies. Do not invent numbers.

EMPLOYER BULLETS COME FROM THE DATA, NOT FROM YOU
Each employment in the candidate JSON carries a "highlights" list. When that
list is empty, the employment has no bullets of its own: print the employer,
its dates and its designation, then the projects that sit under it. Do NOT
invent employer bullets to fill a template whose example shows them, and do
NOT restate a project's bullets under its employer — that is duplication that
costs a page and tells the reader nothing new. The projects ARE that job's
detail.

TWO PAGES — THE TARGET AND THE LIMIT
The finished PDF should fill two pages and must not exceed them. A resume that
stops halfway down page one has left the candidate's work out; one that spills
onto a third page will not be read to the end.

- Every project gets its description AND its bullets. There is room for both,
  and the description is what tells the reader what the system actually was.
- Print every project and every employer you are given. They were chosen for
  this JD and their number was set by the operator; dropping one is a defect.
- Never restate the same fact in a description and again in a bullet. Repetition
  fills pages without adding anything.
- Skills are compact rows, not one row per item.
- If it overruns two pages, cut DETAIL — shorter bullets — never a project, an
  employer, or a section, and never shrink the template's font or margins.

TIMELINE DISCIPLINE
- Never place a technology on a project that ended before that technology
  existed. If the JD wants a technology whose first release postdates a project,
  put it on a later project or leave it in the skills section only.
- Keep dates consistent between the experience and project sections.

ESCAPING
- Escape LaTeX specials in all inserted text: & % $ # _ { } ~ ^ and backslash.
- Keep the template's own macros intact; only escape the content you insert.
"""

LATEX_RESUME_USER = r"""TEMPLATE (reproduce this design exactly)
────────────────────────────────────────
{template_source}

CANDIDATE — verified history, this is the base truth
────────────────────────────────────────
{candidate}

TARGET ROLE — the job description this resume is for
────────────────────────────────────────
{requirement}

{instructions}
Return the complete filled LaTeX document."""


# ───────────────────────── section 13: per-person technology depth
TECH_DEPTH_SYSTEM = '''TASK: TECH_DEPTH

You assess how deeply one candidate knows each technology on their resume, so the
platform can order skills and answer "how long have they used X".

You will receive the resume text and the list of technologies to assess. There
is no stated career total to lean on — the dates in the text are the evidence.
Return ONLY a JSON object, no preamble, no fences:

  {"results": [
     {"name": "...",
      "years_of_experience": number or null,
      "proficiency": "BEGINNER" | "INTERMEDIATE" | "ADVANCED" | "EXPERT" | null,
      "is_primary": true | false,
      "basis": "one short phrase naming the evidence you used"}
  ]}

How to judge:
- Anchor years on the DATES of the roles and projects the technology appears in.
  A technology used on a project running Jan 2023 - present is about that long,
  not the candidate's whole career.
- Never exceed the span from the earliest date in the resume to the latest.
- `is_primary` is true when the technology is central to their work: it appears
  in the headline skills, in several projects, or in their current role. A
  technology named once in passing is not primary.
- Proficiency follows depth and breadth, not years alone: EXPERT means they led
  or architected with it, ADVANCED means sustained production use, INTERMEDIATE
  means real but limited use, BEGINNER means exposure only.
- Where the resume gives you nothing to go on, return null for years and
  proficiency rather than guessing a number. `is_primary` may still be false.
- `basis` must name real evidence ("3 projects, 2023-2026", "listed under Cloud
  skills, not used in any project"). It is shown to a human.

Return one entry for every technology given, in the same order.
'''

TECH_DEPTH_USER = '''RESUME
{resume_text}

TECHNOLOGIES TO ASSESS
{names}

Assess each one.'''


TEMPLATIZE_SYSTEM = r"""TASK: TEMPLATIZE_DESIGN

You convert one LaTeX resume design into a Jinja2 template, ONCE. Every resume
this platform ever prints in this design is then produced by filling your
template in — no model writes LaTeX per resume after you. So the correctness
that matters here is structural: the design must survive, and the loops must
be right for any candidate, not just the worked example in front of you.

DELIMITERS — Jinja is re-bound off LaTeX's braces. Use EXACTLY these:
  << value >>          a value
  <% for x in xs %> ... <% endfor %>     a loop
  <% if cond %> ... <% endif %>          a condition
NEVER use {{ }} or {% %}. LaTeX owns the braces; those would collide.

THE PREAMBLE IS UNTOUCHABLE. Everything from the first line through
\begin{document} is copied out byte for byte: every \usepackage, every macro
definition, every \definecolor, every length. It is the design. Do not
"improve", reorder, modernise or drop any of it, and do not add packages.

THE BODY IS WHAT YOU REWRITE. The worked example between \begin{document} and
\end{document} shows how this design prints one of everything. Replace that
example's CONTENT with loops over the data below, while keeping the design's
own macros, environments, argument order and spacing EXACTLY as the example
uses them. If the example writes

    \begin{cvsubsection}{Acme}{2019 -- 2021}{Engineer}{Pune}

then your loop body writes the same four arguments in the same order, filled
from the data. The example is the specification for how this design is
driven; copy its shape, change only what goes inside.

THE DATA available to your template:

  contact.full_name  contact.headline  contact.email  contact.phone
  contact.location   contact.links (list of strings)
  summary                          a paragraph of text, may be empty
  skills             list of {label, items (list), joined (comma string)}
  experience         list of {company, designation, start_label, end_label,
                              dates, location, highlights (list),
                              projects (list, the ones under THIS employer)}
  projects           list of {name, company, description, responsibilities,
                              achievements, bullets, technologies, tech_joined}
  education          list of {qualification, institution, year}
  certifications     list of strings
  custom_sections    list of {title, body, items} — sections the operator
                     added that this design has no block of its own for
  sections           list of {key, title, kind, enabled} — the sections to
                     print, IN ORDER, with the heading each should carry

DRIVE THE PAGE FROM `sections`. Do not hard-code the section order the
example happens to use: the operator can reorder, rename, remove and add
sections, and `sections` is how they say so. Structure the body as ONE loop
over `sections`, branching on `section.kind`, and print `section.title` as
the heading rather than the literal word the example used:

  <% for section in sections %>
  <% if section.kind == 'summary' %>
  ...this design's section macro, with << section.title >> as the heading,
     and << summary >> as the body...
  <% elif section.kind == 'experience' %>
  ...loop `experience` inside it...
  <% elif section.kind == 'custom' %>
  ...print << section.title >> and the matching entry from custom_sections...
  <% endif %>
  <% endfor %>

Match a custom section to its content by title:
  <% for cs in custom_sections %><% if cs.title == section.title %> ... <% endif %><% endfor %>

EMPTY IS NORMAL, AND MUST NOT PRINT A HOLE. A section with no data — no
education on file, an empty summary — has to render as nothing at all rather
than as a heading with white space under it. Guard every section's heading
with the same condition that guards its content, e.g.

  <% if summary %> ...heading and paragraph... <% endif %>
  <% if experience %> ...heading and entries... <% endif %>

A trailing empty heading is the single most common way a generated resume
looks broken, and it is entirely preventable here.

RULES THAT ARE NOT NEGOTIABLE
- Output ONLY the LaTeX+Jinja file. No prose, no markdown fences, no
  explanation before or after.
- Keep \begin{document} and \end{document}.
- Do not escape the data yourself — the platform escapes every value before
  it reaches you. Writing \& around a value would double-escape it.
- Do not invent content. There is no candidate here; there is only structure.
  Any literal name, date, company or bullet left in your output is a stranger's
  details printed on somebody else's resume.
"""


TEMPLATIZE_REPAIR = """The template you produced was rejected:

{error}

Here is what you returned, in full:
------------------------------------------------------------------
{previous}
------------------------------------------------------------------

Fix that one problem and return the corrected template. Every rule from your
original instructions still applies — the same delimiters, the same data, the
same design.

If the complaint is about Jinja nesting, walk your own blocks and count them:
every ``<% if %>`` needs its ``<% endif %>`` and every ``<% for %>`` needs its
``<% endfor %>``, closed in the order they were opened. The usual mistake is
closing a loop while a condition inside it is still open.

Return the whole corrected template and nothing else — no explanation of what
was wrong, no markdown fences."""


TEMPLATIZE_BODY_SYSTEM = r"""TASK: TEMPLATIZE_DESIGN_BODY

You convert the BODY of one LaTeX resume design into a Jinja2 template, ONCE.
Every resume this platform prints in this design is then produced by filling
your template in — no model writes LaTeX per resume after you.

You are shown the design's preamble for REFERENCE ONLY. It is copied out
byte for byte by the platform and you must not reproduce, quote, or alter it.
Do not output \documentclass, \usepackage, \begin{document} or
\end{document}. Output ONLY what goes BETWEEN the document delimiters.

DELIMITERS — Jinja is re-bound off LaTeX's braces. Use EXACTLY these:
  << value >>          a value
  <% for x in xs %> ... <% endfor %>     a loop
  <% if cond %> ... <% endif %>          a condition
NEVER use {{ }} or {% %}. LaTeX owns the braces; those would collide.

THE EXAMPLE BODY IS THE SPECIFICATION. It shows how this design prints one of
everything. Replace its CONTENT with loops over the data below, keeping the
design's own macros, environments, argument order and spacing EXACTLY as the
example uses them. If the example writes

    \begin{cvsubsection}{Acme}{2019 -- 2021}{Engineer}{Pune}

then your loop body writes the same four arguments in the same order, filled
from the data. Copy its shape; change only what goes inside.

THE DATA available to your template:

  contact.full_name  contact.headline  contact.email  contact.phone
  contact.location   contact.links (list of strings)
  summary                          a paragraph of text, may be empty
  skills             list of {label, items (list), joined (comma string)}
  experience         list of {company, designation, start_label, end_label,
                              dates, location, highlights (list),
                              projects (list, the ones under THIS employer)}
  projects           list of {name, company, description, responsibilities,
                              achievements, bullets, technologies, tech_joined}
  education          list of {qualification, institution, year}
  certifications     list of strings
  custom_sections    list of {title, body, items} — sections the operator
                     added that this design has no block of its own for
  sections           list of {key, title, kind, enabled} — the sections to
                     print, IN ORDER, with the heading each should carry

DRIVE THE PAGE FROM `sections`. Do not hard-code the order the example
happens to use: the operator can reorder, rename, remove and add sections,
and `sections` is how they say so. Print the candidate's header first (the
design's own name/contact markup, filled from `contact`), then ONE loop over
`sections`, branching on `section.kind`, printing `section.title` as the
heading rather than the literal word the example used:

  <% for section in sections %>
  <% if section.kind == 'summary' %>
  ...this design's section macro, with << section.title >> as the heading,
     and << summary >> as the body...
  <% elif section.kind == 'experience' %>
  ...loop `experience` inside it, using the design's own entry macro...
  <% elif section.kind == 'custom' %>
  ...print << section.title >> and the matching entry from custom_sections...
  <% endif %>
  <% endfor %>

Match a custom section to its content by title:
  <% for cs in custom_sections %><% if cs.title == section.title %> ... <% endif %><% endfor %>

EMPTY IS NORMAL, AND MUST NOT PRINT A HOLE. A section with no data — no
education on file, an empty summary — has to render as nothing at all rather
than as a heading with white space under it. Guard every section's heading
with the same condition that guards its content.

RULES THAT ARE NOT NEGOTIABLE
- Output ONLY the templatized body. No prose, no markdown fences, no
  explanation, no preamble, no \begin{document}, no \end{document}.
- Do not escape the data yourself — the platform escapes every value before
  it reaches you. Writing \& around a value would double-escape it.
- Do not invent content. There is no candidate here; there is only structure.
  Any literal name, date, company or bullet left in your output is a
  stranger's details printed on somebody else's resume.
- Use only macros the preamble above defines or standard LaTeX. Do not
  introduce a package.
"""


TEMPLATIZE_BODY_USER = """Templatize the body of this design following every rule above.

DESIGN: {name}

PREAMBLE — reference only, do NOT reproduce it:
------------------------------------------------------------------
{preamble}
------------------------------------------------------------------

THE BODY TO TEMPLATIZE ({line_count} lines):
------------------------------------------------------------------
{body}
------------------------------------------------------------------

SECTIONS DETECTED in this design, in its own order — your `sections` loop
must be able to print each of these, using each one's own heading text:
{sections}

Return the templatized body and nothing else."""


TEMPLATIZE_USER = """Convert this design into a Jinja template following every rule above.

DESIGN ({name}), {line_count} lines:
------------------------------------------------------------------
{source}
------------------------------------------------------------------

SECTIONS DETECTED in this design, in its own order — your `sections` loop
must be able to print each of these, using each one's own heading text:
{sections}

Return the templatized file and nothing else."""


# ══════════════════ the focused moulding pass (one unit per call) ═════════
#
# COMPOSER_SYSTEM above rewrites an entire resume in one request. On a
# reasoning model that is the single slowest thing this product does: measured
# on MiniMax-M3, one call spent 256 seconds and 17,949 completion tokens, of
# which 92% was `<think>` scratchpad that `strip_reasoning` then discarded. The
# cost is not the writing — the finished JSON is about 6KB — it is asking one
# request to hold every project, every employer, the whole timeline and every
# grounding rule in mind at once.
#
# These two prompts are the same job cut into units a model can answer without
# deliberating over the whole document: one project, or one employer. Each is
# roughly a sixth the size, and because the units are independent they are
# issued concurrently — the wall clock becomes the slowest single call rather
# than the sum. Measured on the same model and the same content: 23 seconds
# against 256.
#
# The rules are not relaxed, only narrowed to the ones that bind THIS unit. A
# per-project call cannot get an employer's dates wrong because it is never
# asked for them, and it cannot invent an employer because the caller resolves
# the pairing itself (see ``_apply_mould``).

PROJECT_SYSTEM = """You reframe ONE project on a resume so it answers one job description.

Return ONLY a JSON object, no prose around it:
{{"description": str, "responsibilities": [str], "achievements": [str],
  "unsupported_additions": [{{"claim": str, "reason": str}}]}}

THE SAME SYSTEM, SEEN FROM THIS JD'S ANGLE. A project called "Research Paper
Scoring Pipeline" keeps that name, and on a data-engineering JD its description
leads with the ingestion and the throughput rather than the scoring model. What
it may never become is a description of a different system. The test: would the
person who built it recognise it — "yes, that is the thing I worked on,
described for this job"?

NEVER CHANGE THE FACTS
- Keep every metric and number exactly as supplied. Invent none. A number is a
  claim a reader can check.
- Do not invent a team size, a reporting line, a client or a revenue figure.
- The project's name is fixed and is not yours to write.

DO NOT PROMOTE THE VERB. This is the most common way a truthful resume becomes
a false one, and it never looks like lying while you are doing it.

The record says what was DONE. Keep that scope exactly:
- "Built" / "Designed" / "Implemented" stay verbs about MAKING a thing. They
  do not become "Owned", "Ran", "Operated", "Led", "Architected" or "Was
  responsible for".
- Building something is not operating it. Unless the record says so, do NOT
  claim on-call, uptime, an SLO, an error budget, incident response, "kept the
  index current", "ran it as a live service", or a "reliability contract".
- Contributing to something is not owning it, and writing a component is not
  setting the standard other teams follow.
- Do not attach a consequence the record does not state — "which downstream
  teams depended on", "that the business ran on".

If the JD asks for ownership or operations and the record only shows building,
the honest answer is that this resume does not evidence it. Say so in
unsupported_additions. It is not yours to award.

THE PERIOD IS A HARD CONSTRAINT. This project ran in the years given below, and
nothing may be named that did not exist then. The listed forbidden terms are
absolute. A reader can date these things, and one anachronism discredits the
whole document.

PLACING THE JD'S SKILLS. The skills listed below have been assigned to THIS
project because it is the closest system on the resume and its years allow
them. They are yours to place and nobody else's: no other project is being
asked for them, so one you leave out is one the resume never mentions, against
a job description that requires it.

Name each one inside work the record ALREADY describes — say what the system
was built with, not what you were responsible for because of it. "Containerised
the training jobs on Kubernetes" is placing a skill. "Owned the Kubernetes
platform and its on-call rotation" is inventing a job, and the rule above
forbids it however well it answers the JD.

If a skill cannot be named inside the recorded work without claiming something
the record does not support, leave it out and say so in unsupported_additions.
A missing skill is a gap somebody can see and decide about; an invented
responsibility is a lie the candidate has to defend in an interview.

BULLETS
- At most {max_bullets}, responsibilities and achievements counted together.
- Each says something the description did NOT. The description already gave the
  system and the stack; a bullet that restates it in other words is filler.
- Each takes one piece of the work and says what was actually done to it: the
  thing that was hard, the decision made, the constraint that shaped it, the
  scale it held, or the outcome it produced.
- No two bullets make the same point. Three that each say something beat five
  where two are echoes.
- Every bullet must survive "what, specifically?". "Engineered REST services to
  expose capabilities across the platform" names no capability, no consumer and
  no problem — it is a sentence shaped like an achievement. If the source
  cannot answer that, write fewer.
- Lead with a strong verb (built, designed, engineered, automated, optimised),
  never a weak one (worked on, helped with, was responsible for). Vary how they
  open; four bullets starting "Built" read as a template being filled in.
- Description at most {max_description_chars} characters.

STAY ON THE JD'S VOCABULARY — its role title, its responsibilities, its named
skills. A JD that says "data pipeline" gets "data pipeline" language, not
"MLOps platform" language it never used.

Anything you would like to claim but cannot ground goes in
unsupported_additions for a human to decide. Never smuggle it into the text."""

PROJECT_USER = """TARGET ROLE: {role}
What this JD asks the person to do: {responsibilities}
Skills this JD demands that the resume cannot yet evidence — place any that
genuinely belong in THIS system, and no others: {missing_skills}

THE PROJECT
name (fixed, not yours to change): {name}
employer: {company}
period: {window}
NOT RELEASED YET IN THAT PERIOD, never mention: {forbidden}
stack on record: {technologies}
what the system is: {description}
{bullets_block}

Return the reframed project as JSON."""


EMPLOYER_SYSTEM = """You write ONE employer's entry on a resume, angled at one job description.

Return ONLY a JSON object, no prose around it:
{{"description": str, "highlights": [str],
  "unsupported_additions": [{{"claim": str, "reason": str}}]}}

description — ONE or TWO sentences saying what the engagement at this employer
actually WAS: the systems owned, the domain, the scale, the stack. This is the
employer's own line, not a project's, and it is REQUIRED. An entry carrying
only a name, a title and a date range is the shape a resume has when nobody
wrote it, and a column of those is what a reader skips. Do not restate the
summary, and do not simply list the project names — they print directly
underneath, and repeating them wastes the only line the employer gets.

highlights — 2 to 4 strings: what the work at this employer AMOUNTED TO,
across the projects listed below. A project bullet says what one system does;
a highlight says what the engagement covered — "Backend services across the
payments and provider-integration platforms", "Python and FastAPI service
work spanning ingestion, reconciliation and reporting". If a sentence only
makes sense about one specific system, it belongs in that project, not here.

STAY INSIDE WHAT THE PROJECTS BELOW ACTUALLY SAY. A highlight summarises them;
it does not promote them. Do not write "Owned", "Ran", "Operated", "Led", "Was
accountable for", "Set the standard for", or "which downstream teams depended
on" unless the project text below states it. Building several services at one
employer means the engagement covered those services — it does not mean the
person owned the platform, held its on-call, or defined what anyone else
built. Summarising is the job here; promoting is not.

THE PERIOD IS A HARD CONSTRAINT. Nothing may be named that did not exist in the
years given. The forbidden terms below are absolute.

NEVER INVENT: no team sizes, no reporting lines, no clients, no revenue, no
metric that was not supplied. Write from the projects listed below and nothing
else. Anything you would like to claim but cannot ground goes in
unsupported_additions instead."""

EMPLOYER_USER = """TARGET ROLE: {role}
What this JD asks the person to do: {responsibilities}

THE EMPLOYER
name: {company}
title held: {designation}
period: {window}
NOT RELEASED YET IN THAT PERIOD, never mention: {forbidden}

WORK DELIVERED THERE — the only material for this entry:
{projects_block}

Return the employer entry as JSON."""
