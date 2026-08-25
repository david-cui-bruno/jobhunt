# Approved Application Answers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist David’s newly approved application facts and make the deterministic QA policy answer only the intended matching questions.

**Architecture:** Extend the existing private `profile/application_answers.yaml` schema and the deterministic render/approval branches in `apply/qa.py`. Company-specific answers remain scoped through `_company_matches`; generic legal, event, language, transcript-consent, offer, and date facts get explicit relevance and exact-value checks.

**Tech Stack:** Python 3.9, PyYAML, pytest, existing `apply/qa.py` policy engine

**Spec:** `docs/superpowers/specs/2026-08-25-jobhunt-approved-answers-compensation-design.md`

## Global Constraints

- Never send candidate PII or approved answer-bank contents to a new external service.
- A missing structured fact remains manual.
- Company-specific facts must not leak across normalized company contexts.
- Transcript authorization does not invent or locate a transcript file and does not authorize unrelated document uploads.
- “Mid-May” is normalized to `05/15/2027` only for application start-date questions.
- Preserve the existing unverified Soren deadline note. Do not invent a contractual day.
- Do not release queues, click Submit, or alter submission-confirmation semantics.
- Use TDD for every policy change and commit each independently reviewed task.

## File Structure

- Modify `profile/application_answers.yaml`: canonical user-approved facts.
- Modify `apply/qa.py`: relevance gating, deterministic rendering, and exact approval checks.
- Modify `tests/test_qa_policy.py`: source-of-truth, scoping, rendering, and fail-closed regressions.

---

### Task 1: Persist the canonical approved facts

**Files:**
- Modify: `profile/application_answers.yaml`
- Test: `tests/test_qa_policy.py`

**Interfaces:**
- Consumes: `_load_application_answers() -> dict` from `apply/qa.py`.
- Produces: structured fields under `availability`, `current_offers`, `company_facts`, `legal`, `professional`, `documents`, and `events`.

- [ ] **Step 1: Write the failing canonical-source test**

Add a test that reads the loaded answer bank and asserts the exact approved facts:

```python
def test_canonical_answer_bank_contains_august_25_approved_facts():
    approved = qa.APPLICATION_ANSWERS
    assert approved["legal"]["security_clearance"] == "None"
    assert approved["legal"]["us_dod_employment_after_2008_01_28"] is False
    assert approved["legal"]["self_or_family_or_business_partner_government_employment"] is False
    assert approved["company_facts"]["Point72"]["prior_application"] is False
    assert approved["company_facts"]["Akuna Capital"]["prior_application"] is False
    assert approved["company_facts"]["Availity"]["household_employment"] is False
    assert approved["company_facts"]["American Fidelity"]["household_employment"] is False
    assert approved["events"]["neurips_2026"]["attending"] is False
    assert approved["professional"]["english_proficiency"] == "Fluent"
    transcript = approved["documents"]["unofficial_transcript"]
    assert transcript == {"available": True, "application_upload_authorized": True}
    assert approved["availability"]["default_start_date"] == "05/15/2027"
    assert approved["availability"]["summer_2027"]["start"] == "05/15/2027"
    assert approved["current_offers"][0]["company"] == "Soren"
    assert approved["current_offers"][0]["description"] == "Founding Engineer"
```

- [ ] **Step 2: Run the test to verify RED**

Run:

```bash
python3 -m pytest tests/test_qa_policy.py::test_canonical_answer_bank_contains_august_25_approved_facts -q
```

Expected: FAIL on the first absent or stale structured value.

- [ ] **Step 3: Add the exact values to the answer bank**

Update the YAML without deleting earlier approved facts:

```yaml
availability:
  default_start_date: 05/15/2027
  summer_2027:
    pursue: true
    start: 05/15/2027
    end: 08/20/2027
current_offers:
  - company: Soren
    description: Founding Engineer
    deadline: 09/15/2026
    deadline_month: September 2026
    deadline_note: Exact contractual day is not yet verified; never invent one.
company_facts:
  Point72:
    prior_application: false
  Akuna Capital:
    prior_application: false
    prior_interview: false
    prior_interview_or_application: false
  Availity:
    household_employment: false
  American Fidelity:
    household_employment: false
legal:
  security_clearance: None
  us_dod_employment_after_2008_01_28: false
  self_or_family_or_business_partner_government_employment: false
professional:
  english_proficiency: Fluent
  publications: []
  references: Available upon request.
documents:
  unofficial_transcript:
    available: true
    application_upload_authorized: true
events:
  neurips_2026:
    attending: false
```

Change the existing `generic_internship_start_date` long-form answer to `05/15/2027`.

- [ ] **Step 4: Run the canonical-source test to verify GREEN**

Run the focused test from Step 2.

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add profile/application_answers.yaml tests/test_qa_policy.py
git commit -m "Record approved application facts"
```

---

### Task 2: Scope the new answer-bank sections to matching controls

**Files:**
- Modify: `apply/qa.py:98-199`
- Test: `tests/test_qa_policy.py`

**Interfaces:**
- Consumes: `relevant_application_answers(controls, approved, company_context) -> dict`.
- Produces: least-privilege answer subsets for `legal`, `professional`, `documents`, `events`, `availability`, and `company_facts`.

- [ ] **Step 1: Write failing least-privilege tests**

Add table-driven tests with a complete approved dictionary:

```python
@pytest.mark.parametrize((label, section, key), [
    ("What level is your US government security clearance?", "legal", "security_clearance"),
    ("Were you a US Department of Defense employee on or after January 28, 2008?", "legal", "us_dod_employment_after_2008_01_28"),
    ("What is your English proficiency?", "professional", "english_proficiency"),
    ("May we upload your unofficial transcript?", "documents", "unofficial_transcript"),
    ("Will you attend NeurIPS 2026?", "events", "neurips_2026"),
    ("When can you start?", "availability", "default_start_date"),
])
def test_relevant_application_answers_exposes_only_matching_new_fact(label, section, key):
    result = qa.relevant_application_answers(
        [{"id": "q", "label": label}], APPROVED_AUG_25,
    )
    assert key in result[section]
```

Add negative assertions:

```python
def test_new_sensitive_sections_are_not_exposed_to_unrelated_questions():
    result = qa.relevant_application_answers(
        [{"id": "q", "label": "Why do you want this role?"}], APPROVED_AUG_25,
    )
    assert "legal" not in result
    assert "professional" not in result
    assert "documents" not in result
    assert "events" not in result
```

Add company isolation:

```python
def test_household_and_prior_application_facts_do_not_cross_company_contexts():
    availity = qa.relevant_application_answers(
        [{"id": "q", "label": "Do you have relatives employed by Availity?"}],
        APPROVED_AUG_25,
        company_context="Availity",
    )
    assert set(availity["company_facts"]) == {"Availity"}
    unrelated = qa.relevant_application_answers(
        [{"id": "q", "label": "Do you have relatives employed here?"}],
        APPROVED_AUG_25,
        company_context="Another Company",
    )
    assert "company_facts" not in unrelated
```

- [ ] **Step 2: Run the new relevance tests to verify RED**

Run:

```bash
python3 -m pytest tests/test_qa_policy.py -k "matching_new_fact or new_sensitive_sections or household_and_prior" -q
```

Expected: FAIL for missing `documents`, `events`, English, and government/DoD relevance branches.

- [ ] **Step 3: Implement minimal relevance patterns**

In `relevant_application_answers`:

```python
if re.search(r"\b(english proficiency|proficiency in english|fluent in english)\b", question):
    result["professional"] = {
        "english_proficiency": (source.get("professional") or {}).get("english_proficiency")
    }
if re.search(r"\b(unofficial )?transcript\b", question):
    result["documents"] = {
        "unofficial_transcript": (source.get("documents") or {}).get("unofficial_transcript") or {}
    }
if re.search(r"\bneurips\s*2026\b", question):
    result["events"] = {
        "neurips_2026": (source.get("events") or {}).get("neurips_2026") or {}
    }
```

Expand the legal relevance regex only with exact government/DoD families:

```python
r"\bdepartment of defense|\bDoD\b|government[- ]entity employment|government agency employment|government employment"
```

Keep existing `_company_matches` handling unchanged for Point72, Akuna, Availity, and American Fidelity.

- [ ] **Step 4: Run relevance tests and the existing company-scoping suite**

```bash
python3 -m pytest tests/test_qa_policy.py -k "relevant_application_answers or company_context or matching_new_fact or household_and_prior" -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apply/qa.py tests/test_qa_policy.py
git commit -m "Scope newly approved application facts"
```

---

### Task 3: Render and verify the approved answers deterministically

**Files:**
- Modify: `apply/qa.py:1086-1587`
- Modify: `apply/qa.py:1598-2068`
- Test: `tests/test_qa_policy.py`

**Interfaces:**
- Consumes: `explicit_approved_answers(...) -> list[dict]` and `answer_requires_manual(...) -> bool`.
- Produces: exact rendered values whose approval verifier accepts, while alternative values remain blocked.

- [ ] **Step 1: Write failing rendering and verifier tests**

Use exact controls and options:

```python
def test_august_25_facts_render_exactly_and_wrong_values_remain_manual():
    controls = [
        {"id": "clear", "label": "What level is your US government security clearance?", "options": ["None", "Public Trust", "Secret"]},
        {"id": "dod", "label": "Were you a US Department of Defense employee on or after January 28, 2008?", "options": ["Yes", "No"]},
        {"id": "gov", "label": "Have you or your immediate family or business partners worked for a government entity?", "options": ["Yes", "No"]},
        {"id": "english", "label": "English proficiency", "options": ["Basic", "Conversational", "Fluent", "Native"]},
        {"id": "neurips", "label": "Will you attend NeurIPS 2026?", "options": ["Yes", "No"]},
        {"id": "transcript", "label": "May we upload your unofficial transcript?", "options": ["Yes", "No"]},
        {"id": "start", "label": "When can you start?", "options": []},
    ]
    answers = qa.explicit_approved_answers(controls, approved_answers=APPROVED_AUG_25)
    assert {a["id_or_name"]: a["answer"] for a in answers} == {
        "clear": "None", "dod": "No", "gov": "No", "english": "Fluent",
        "neurips": "No", "transcript": "Yes", "start": "05/15/2027",
    }
    assert qa.answer_requires_manual(controls[0], "Secret", approved_answers=APPROVED_AUG_25)
    assert qa.answer_requires_manual(controls[1], "Yes", approved_answers=APPROVED_AUG_25)
```

Add company-context cases for Point72, Akuna, Availity, and American Fidelity, plus a similarly named unrelated employer. Assert the approved “No” is rendered only in the intended context.

- [ ] **Step 2: Run focused tests to verify RED**

```bash
python3 -m pytest tests/test_qa_policy.py -k "august_25_facts_render or point72 or availity or american_fidelity" -q
```

Expected: FAIL for missing deterministic branches and over-broad clearance approval.

- [ ] **Step 3: Add exact deterministic branches**

At the top of `explicit_approved_answers`, bind:

```python
legal = approved.get("legal") or {}
professional = approved.get("professional") or {}
documents = approved.get("documents") or {}
events = approved.get("events") or {}
availability = approved.get("availability") or {}
```

Add branches before generic schedule/document text handling:

```python
elif re.search(r"\b(security clearance|clearance level|public trust|secret clearance|top secret|ts/sci)\b", question):
    expected = str(legal.get("security_clearance") or "").strip()
    answer = _first_matching_option([expected, "No clearance", "None"], options) if options else expected or None
elif re.search(r"\bdepartment of defense|\bdod\b", question) and re.search(r"\bemploy", question):
    expected = legal.get("us_dod_employment_after_2008_01_28")
    if isinstance(expected, bool):
        answer = _render_boolean(expected, options)
elif re.search(r"\bgovernment (?:entity|agency|employment)\b", question) and re.search(r"\b(family|business partner|worked|employ)", question):
    expected = legal.get("self_or_family_or_business_partner_government_employment")
    if isinstance(expected, bool):
        answer = _render_boolean(expected, options)
elif re.search(r"\b(english proficiency|proficiency in english|fluent in english)\b", question):
    expected = str(professional.get("english_proficiency") or "").strip()
    answer = _first_matching_option([expected], options) if options else expected or None
elif re.search(r"\bneurips\s*2026\b", question):
    expected = ((events.get("neurips_2026") or {}).get("attending"))
    if isinstance(expected, bool):
        answer = _render_boolean(expected, options)
elif re.search(r"\b(unofficial )?transcript\b", question) and re.search(r"\b(may|consent|authorize|upload)\b", question):
    transcript = documents.get("unofficial_transcript") or {}
    expected = transcript.get("available") is True and transcript.get("application_upload_authorized") is True
    answer = _render_boolean(expected, options)
elif re.search(r"\b(?:when can you start|desired start date|available to start)\b", question):
    answer = availability.get("default_start_date")
```

Mirror these question families in `_blocked_answer_is_approved` and compare against the exact expected value. Replace the current clearance check, which approves any answer when a clearance fact exists, with an exact option/text comparison.

- [ ] **Step 4: Run focused policy tests**

```bash
python3 -m pytest tests/test_qa_policy.py -q
```

Expected: all policy tests PASS.

- [ ] **Step 5: Commit**

```bash
git add apply/qa.py tests/test_qa_policy.py
git commit -m "Answer approved application facts deterministically"
```

---

### Task 4: Exercise the production answer-resolution interface

**Files:**
- Modify only if a discovered regression requires it: `apply/qa.py`, `tests/test_qa_policy.py`

**Interfaces:**
- Consumes: the actual loaded `qa.APPLICATION_ANSWERS`, `explicit_approved_answers`, and `filter_manual_answers`.
- Produces: acceptance evidence that supplied facts remove intended manual blockers without approving unrelated answers.

- [ ] **Step 1: Run a production-policy probe**

```bash
python3 - <<'PY'
from apply import qa
controls = [
    {"id": "clear", "label": "What level is your US government security clearance? *", "options": ["None", "Public Trust", "Secret"]},
    {"id": "point72", "label": "Have you previously applied to Point72? *", "options": ["Yes", "No"]},
    {"id": "availity", "label": "Are you a relative of an Availity employee? *", "options": ["Yes", "No"]},
    {"id": "english", "label": "What is your English proficiency level? *", "options": ["Basic", "Fluent", "Native"]},
    {"id": "start", "label": "What is your desired start date? *", "options": []},
]
answers = qa.explicit_approved_answers(controls, company_context="Point72")
allowed, blocked = qa.filter_manual_answers(controls, answers, company_context="Point72")
print({"answers": answers, "allowed": allowed, "blocked": blocked})
assert {x["id_or_name"] for x in blocked} == {"availity"}
assert {x["id_or_name"] for x in allowed} == {"clear", "point72", "english", "start"}
PY
```

The Availity control is intentionally blocked under Point72 context. Run a second call with `company_context="Availity"` and only that control; assert it returns approved “No.”

- [ ] **Step 2: Verify transcript action remains fail closed without a file**

Search the configured repo-local/document paths for a transcript PDF. If no configured path exists, assert the answer bank authorizes upload consent but no adapter attempts a file upload. Record this as a remaining artifact dependency, not a user-answer blocker.

- [ ] **Step 3: Run the full regression suite and source checks**

```bash
python3 -m pytest -q
git diff --check
git status --short --branch
```

Expected: full suite PASS; only expected live/generated files remain dirty.

- [ ] **Step 4: Commit any acceptance-driven correction**

If Step 1 or 2 required a code correction:

```bash
git add apply/qa.py tests/test_qa_policy.py
git commit -m "Harden approved answer resolution"
```

If no correction was needed, do not create an empty commit.
