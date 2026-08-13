"""Form Q&A engine: extract form controls, have Claude map them to profile answers,
fill them. Questions Claude can't answer from profile facts -> returned as 'manual'.
"""
from __future__ import annotations

import json
import os
import re
import sys
import datetime
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PROFILE = yaml.safe_load((ROOT / "profile" / "profile.yaml").read_text())


def _load_application_answers() -> dict:
    """Load user-approved sensitive answers outside the source tree when configured."""
    configured = os.environ.get("JOBHUNT_APPLICATION_ANSWERS_FILE", "").strip()
    candidates = [Path(configured)] if configured else []
    candidates.append(ROOT / "profile" / "application_answers.yaml")
    for path in candidates:
        try:
            if path.is_file():
                value = yaml.safe_load(path.read_text()) or {}
                if isinstance(value, dict):
                    return value
        except OSError:
            continue
    return {}


APPLICATION_ANSWERS = _load_application_answers()


def _company_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _company_matches(company: object, question: str, company_context: str = "") -> bool:
    key = _company_key(company)
    question_key = _company_key(question)
    context_key = _company_key(company_context)
    return bool(key and (
        key in question_key
        or (context_key and (key in context_key or context_key in key))
    ))


def relevant_application_answers(controls: list[dict], approved: dict | None = None,
                                 company_context: str = "") -> dict:
    """Expose only answer-bank sections relevant to controls on this form."""
    source = APPLICATION_ANSWERS if approved is None else approved
    question = " ".join(_control_question_text(control).lower() for control in controls)
    result: dict = {"version": source.get("version", 1)}
    identity = source.get("identity") or {}
    selected_identity = {}
    if re.search(r"\b(date of birth|dob|birth date|birthday|age)\b", question):
        selected_identity["date_of_birth"] = identity.get("date_of_birth")
    if re.search(r"\bpronouns?\b", question):
        selected_identity["pronouns"] = identity.get("pronouns")
    if re.search(r"\b(disability|disabled|impairment|medical condition|health condition)\b", question):
        selected_identity["disability"] = identity.get("disability")
    if selected_identity:
        result["identity"] = selected_identity
    if re.search(r"\b(graduation|graduate)\s+(?:date|month|year)|\b(high school|secondary school)\b", question):
        education = source.get("education") or {}
        profile_education = PROFILE.get("education") or {}
        selected_education = {}
        if re.search(r"\b(graduation|graduate)\s+(?:date|month|year)\b", question):
            selected_education.update({
                "expected_graduation_month": education.get("expected_graduation_month")
                    or profile_education.get("grad_month"),
                "expected_graduation_year": education.get("expected_graduation_year")
                    or profile_education.get("grad_year"),
                "exact_graduation_date": education.get("exact_graduation_date"),
            })
        if re.search(r"\b(high school|secondary school)\b", question):
            selected_education["high_school"] = education.get("high_school")
        result["education"] = selected_education
    preferences = source.get("preferences") or {}
    selected_preferences = {}
    for key, pattern in {
        "hybrid": r"\b(hybrid|onsite|in[- ]?office|days? (?:a|per) week|work schedule)\b",
        "relocation_assistance_required": r"\b(relocat(?:e|ion)|local to the area)\b",
        "travel": r"\btravel\b",
        "future_contact": r"\b(future contact|marketing|talent community)\b",
        "compensation_policy": r"\b(compensation|salary|pay|hourly rate|bonus|equity)\b",
    }.items():
        if re.search(pattern, question):
            selected_preferences[key] = preferences.get(key)
    if selected_preferences:
        result["preferences"] = selected_preferences
    if re.search(r"\b(offer|deadline)\b", question):
        result["current_offers"] = source.get("current_offers") or []
    if re.search(r"\b(start|end|availability|available|internship dates|season)\b", question):
        result["availability"] = source.get("availability") or {}
    legal = source.get("legal") or {}
    if re.search(r"\b(non[- ]?compete|conflict|clearance|public trust)\b", question):
        result["legal"] = legal
    companies = {}
    for company, facts in (source.get("company_facts") or {}).items():
        if _company_matches(company, question, company_context):
            companies[company] = facts
    if companies:
        result["company_facts"] = companies
    return result


def _grounding() -> str:
    """Truthful long-form material: STAR story bank + approved bullet bank.
    Used ONLY as source facts for essay-style answers; never fabricated beyond."""
    out = []
    for p in (ROOT / "docs" / "interview_stories.md", ROOT / "resume" / "bullet_bank.md"):
        if p.exists():
            out.append(p.read_text())
    return "\n\n".join(out)[:24000]


MODEL = "claude-sonnet-5"
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

EXTRACT_JS = """
() => {
  const controls = [];
  const seen = new Set();
  const labelFor = (el) => {
    let t = el.labels?.[0]?.innerText || el.getAttribute('aria-label') || '';
    if (!t) {
      // Lever cards: question text in .application-label above the field
      const q = el.closest('.application-question, li[class*=question]');
      t = q?.querySelector('.application-label, .text, label')?.innerText || '';
    }
    if (!t) {
      const wrap = el.closest('div[class*=question], fieldset, .field, [role=group]');
      t = wrap?.querySelector('label, legend, .label')?.innerText || '';
    }
    if (!t) {
      // Ashby: question text lives on the _fieldEntry wrapper (e.g. date pickers)
      const fe = el.closest('[class*=_fieldEntry], [class*=fieldEntry]');
      t = fe?.querySelector('label, [class*=_label], [class*=question-title]')?.innerText || '';
      if (!t && fe) t = (fe.innerText || '').split('\\n')[0] || '';
    }
    if (!t) t = el.placeholder || '';
    return t.replace(/\\s+/g, ' ').trim().slice(0, 200);
  };
  const groupInfo = (el) => {
    // checkbox/radio group: same name; group question label = wrapper's first label-ish text
    const boxes = [...document.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`)];
    // Ashby: question title label lives on the fieldEntry wrapper
    const fe = el.closest('[class*=_fieldEntry]');
    let q = fe?.querySelector('[class*=question-title], label[class*=_label]')?.innerText || '';
    const wrap = el.closest('fieldset, [role=group], div[class*=question], div[class*=checkbox]')
      || boxes[0]?.parentElement?.parentElement;
    if (!q) q = wrap?.querySelector('legend, .label, label:not([for])')?.innerText || '';
    if (!q) {
      // walk up until a div whose first text node looks like a question
      let n = boxes[0]?.parentElement;
      for (let i = 0; i < 4 && n; i++, n = n.parentElement) {
        const t = [...n.childNodes].find(x => x.nodeType === 3 && x.textContent.trim())?.textContent
          || n.querySelector(':scope > label, :scope > div > label')?.innerText;
        if (t && t.trim().length > 10) { q = t; break; }
      }
    }
    const opts = boxes.map(b => b.labels?.[0]?.innerText?.trim() || b.value).filter(Boolean);
    return { q: (q || '').replace(/\\s+/g, ' ').trim().slice(0, 250), opts };
  };
  document.querySelectorAll('input, select, textarea, [role=combobox]').forEach(el => {
    if (el.type === 'hidden' || el.type === 'file') return;
    const isYesNo = el.type === 'checkbox' && el.closest('[class*=yesno]');
    if (el.offsetParent === null && !isYesNo) return;
    const isGroup = (el.type === 'checkbox' || el.type === 'radio') && el.name;
    const key = isGroup ? el.name : (el.id || el.name || labelFor(el));
    if (!key || seen.has(key)) return;
    seen.add(key);
    let options = [];
    let label = labelFor(el);
    if (el.tagName === 'SELECT') {
      options = [...el.options].map(o => o.text.trim()).filter(Boolean).slice(0, 60);
    }
    if (isGroup) {
      const g = groupInfo(el);
      label = g.q || label;
      options = g.opts.slice(0, 60);
      // Ashby yes/no widget: hidden checkbox with Yes/No buttons
      if (el.closest('[class*=yesno]')) options = ['Yes', 'No'];
    }
    let chosen = '';
    const shell = (el.parentElement || el).closest('.select-shell, .select__container, [class*=select-shell]');
    const cv = shell?.querySelector('.select__single-value, [class*=singleValue], [class*=single-value]');
    if (cv) chosen = cv.innerText.trim().slice(0, 100);
    controls.push({
      id: isGroup ? '' : (el.id || ''), name: el.name || '', tag: el.tagName.toLowerCase(),
      type: isGroup ? 'group-' + el.type : (el.type || el.getAttribute('role') || ''),
      cls: el.className || '',
      chosen,
      label,
      required: el.required || el.getAttribute('aria-required') === 'true',
      value: (el.value && el.type !== 'checkbox' && el.type !== 'radio') ? el.value.slice(0, 100) : '',
      options,
    });
  });
  return controls;
}
"""

ANSWER_PROMPT = """You fill job application forms for this candidate. Profile (source of truth):

{profile}

APPROVED APPLICATION ANSWERS AND POLICIES (source of truth; omit a personal answer if absent):
{application_answers}

Additional standing instructions:
- Compensation expectation questions: follow the approved compensation policy. Prefer an employer-published range or no-preference option. Never invent a numeric amount.
- Outstanding offers/deadlines: report only the approved current offers and deadlines. Never default to No.
- Graduation: June 2028 for every role. When a form requires an exact day, use the user-approved estimate 06/01/2028. Never change the date based on role type.
- High school/secondary school: use only the approved answer-bank value. Omit the answer when it is absent; never infer a school or region.
- Willing to relocate: Yes, without employer relocation assistance. Open to any listed office location; prefer SF then NYC if ranked. If preferred cities are not offered, choose any offered US city over non-US.
- If a select's options are provided, your answer MUST be copied verbatim from the options list (character for character). Pick the option most consistent with the profile.
- How did you hear about us: "Company website" or closest option.
- Signature blocks: "Name"/"Signature" = the candidate's full legal name; "Date" = today's date {today} (use the format the field implies, default MM/DD/YYYY).
- Internship availability dates: start "05/25/2027", end "08/20/2027" (Summer 2027). For Fall 2026 roles: start "09/08/2026", end "12/18/2026". Infer season from the job title/context.
- Required privacy-policy and accurate-information attestations: Yes/agree.
- Optional future contact, recruiting marketing, and talent-community enrollment: No/decline.
- Previous employment at this company / referrals: use only a matching company-specific approved answer; otherwise omit.
- Non-compete and conflicts: use only an explicit approved answer. Work authorization remains governed by the profile.
- Internship history: yes, completed software engineering internships (see resume); none at a hedge fund/prop firm unless resume says otherwise.

Form controls (JSON): {controls}

STORY BANK (truthful long-form source material):
{stories}

Return a JSON array, one entry per control you can answer: {{"id_or_name": ..., "answer": ...}}.
- For selects/comboboxes, answer must EXACTLY match one of the provided options (or be a close prefix for autocomplete widgets).
- Short factual free-text questions (visa status, startup experience, availability, grad year, "list your...", one-line whys) SHOULD be answered from the profile/work history, in 1-3 sentences.
- Essay-style questions ("hardest technical challenge", "why us", "tell us about a project"):
  answer them (FULL AUTO, David ratified 2026-08-08) in 80-150 words using ONLY the
  STORY BANK below. Pick the most relevant story, adapt tone to the company, first person,
  plain text, no markdown. NEVER invent projects, employers, metrics, or credentials that
  are not in the story bank/profile. If nothing in the story bank honestly fits, OMIT it.
- Attention-check / logic-puzzle questions (e.g. riddles, "should I walk or drive to the
  car wash", "what is 2+2", "type the word banana"): these are bot checks. Answer them
  briefly and correctly with common sense (1-2 sentences max). They need no personal facts.
- OMIT anything the profile and story bank genuinely cannot justify.
- Dates: month names and 4-digit years as separate controls demand.
- Date-picker text inputs (placeholder like "Pick date...", labels like "when can you
  start"): ALWAYS answer with the internship start date in MM/DD/YYYY (do not omit).
Return ONLY the JSON array."""


BLOCKED_QUESTION_PATTERNS = [
    r"\b(date of birth|dob|birth date|birthday|age)\b",
    r"\b(18 or older|at least 18|(?:graduation|graduate) (?:date|month|year))\b",
    r"\b(compensation|salary|pay (?:range|rate)|hourly rate|base pay|bonus|equity|expected (?:pay|salary)|desired (?:pay|salary))\b",
    r"\b(offer deadline|exploding offer|outstanding offer|competing offer|pending offer|deadline to accept)\b",
    r"\b(used|use|customer of|experience with|familiar with|proficient in|have you tried)\b.*\b(our|this|the)\b.*\b(product|platform|app|service|software|tool)\b",
    r"\b(referral|referred|refer you|know anyone|previously employed|prior employment|worked (?:at|for)|former(?:\s+\w+){0,4}\s+(?:employee|contingent worker)|current employee)\b",
    r"\b(previously interviewed|interviewed (?:at|with|for)|applied (?:to|with)|prior application|previous application)\b",
    r"\b(FINRA|SIE|securities industry essentials|professional licen[sc]e|certification|certified|plan to take the exam)\b",
    r"\b(member of your household|household member|family member|relative)\b.*\b(employed|worked|employee)\b",
    r"\b(non[- ]?compete|conflict of interest|conflicts?|restrictive covenant|moonlighting|outside employment)\b",
    r"\b(security clearance|clearance level|secret clearance|top secret|ts/sci|public trust)\b",
    r"\b(exact|specific)\b.*\b(schedule|hours|availability|travel)\b|\b(work schedule|travel schedule|travel percentage|% travel|days per week|hours per week|available hours)\b",
    r"\b(disability|disabled|impairment|medical condition|health condition|accommodation history)\b",
    r"\b(preferred pronouns?|pronouns?)\b",
    r"\bhave you (?:ever )?used\b.*\bbefore\b",
    r"\b(days? (?:a|per) week|in[- ]?office|onsite schedule|hybrid schedule|willing to (?:come|work|join).*(?:office|onsite))\b",
    r"\b(local to the area|relocation assistance)\b",
    r"\b(high school|secondary school)\b",
    r"\b(future contact|marketing (?:email|communications?|consent)|talent community)\b",
    r"\b(what (?:are you|do you) (?:reading|watching|listening)|favorite (?:book|movie|podcast|show|song|artist|media)|last (?:book|movie|show|podcast)|reading list|media (?:you consume|consumption))\b",
]


def _control_question_text(control: dict) -> str:
    return " ".join(str(control.get(k) or "") for k in ("label", "id", "name", "placeholder")).strip()


def _answer_boolean(value: object) -> bool | None:
    text = str(value or "").strip().lower()
    if re.search(r"\b(no|false|never|none|not disabled|do not|don't|have not|haven't)\b", text):
        return False
    if re.search(r"\b(yes|true|willing|i have|currently have)\b", text):
        return True
    return None


def _date_parts(value: object) -> tuple[int, int | None, int] | None:
    text = str(value or "").strip()
    full = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b", text)
    if full:
        return int(full.group(1)), int(full.group(2)), int(full.group(3))
    numeric = re.search(r"\b(\d{1,2})[/-](\d{4})\b", text)
    if numeric:
        return int(numeric.group(1)), None, int(numeric.group(2))
    months = {
        name.lower(): index for index, name in enumerate(
            ("January", "February", "March", "April", "May", "June", "July",
             "August", "September", "October", "November", "December"),
            start=1,
        )
    }
    named = re.search(r"\b(" + "|".join(months) + r")\s+(\d{4})\b", text, re.I)
    if named:
        return months[named.group(1).lower()], None, int(named.group(2))
    return None


_MISSING = object()


def _company_fact(control: dict, field: str, answers: dict) -> object:
    question = _control_question_text(control).lower()
    company_context = str(control.get("company_context") or "")
    for company, facts in (answers.get("company_facts") or {}).items():
        if (_company_matches(company, question, company_context)
                and isinstance(facts, dict) and facts.get(field) is not None):
            return facts[field]
    return _MISSING


def _render_boolean(expected: bool, options: list[str]) -> str | None:
    desired = "Yes" if expected else "No"
    if not options:
        return desired
    exact = _best_option(desired, options)
    if exact:
        return exact
    return next((option for option in options if _answer_boolean(option) is expected), None)


def _first_matching_option(candidates: list[str], options: list[str]) -> str | None:
    """Return the first candidate with a credible option match.

    Candidate ordering is meaningful. It lets factual profile data prefer the
    most precise truthful label without ever falling back to an arbitrary first
    option.
    """
    for candidate in candidates:
        match = _best_option(candidate, options)
        if match:
            return match
    return None


def _approved_high_school_answer(approved: dict, options: list[str]) -> str | None:
    """Render the approved school fact for either text or geographic pickers.

    Greenhouse normally asks for the school name, but HRT labels a continent
    picker "Where did you attend high school/secondary school?".  A Texas school
    truthfully maps to North America.  Never choose a region unless the approved
    school value itself contains an explicit US/Texas location marker.
    """
    school = str((approved.get("education") or {}).get("high_school") or "").strip()
    if not school:
        return None
    if not options:
        return school
    direct = _best_option(school, options)
    if direct:
        return direct
    if re.search(r"\b(texas|tx|united states|usa|u\.s\.)\b", school, re.I):
        return _first_matching_option(["United States", "USA", "North America"], options)
    return None


def explicit_approved_answers(controls: list[dict], key_field: str = "id_or_name",
                              company_context: str = "",
                              approved_answers: dict | None = None) -> list[dict]:
    """Render direct approved facts without asking a language model.

    Sensitive yes/no and date facts should not become unavailable, or mutable,
    merely because the model API is down. Unsupported questions are omitted.
    """
    approved = APPLICATION_ANSWERS if approved_answers is None else approved_answers
    identity = approved.get("identity") or {}
    education = approved.get("education") or {}
    preferences = approved.get("preferences") or {}
    offers = [offer for offer in (approved.get("current_offers") or [])
              if isinstance(offer, dict)]
    rendered = []
    for original in controls:
        control = dict(original, company_context=company_context)
        question = _control_question_text(control).lower()
        options = [str(option) for option in control.get("options") or []]
        answer: object | None = None
        if re.search(r"\b(18 or older|at least (?:age )?18)\b", question):
            birth = _date_parts(identity.get("date_of_birth"))
            if birth:
                today = datetime.date.today()
                age = today.year - birth[2] - ((today.month, today.day) < (birth[0], birth[1] or 1))
                answer = _render_boolean(age >= 18, options)
        elif re.search(r"\b(date of birth|dob|birth date|birthday)\b", question):
            answer = identity.get("date_of_birth")
        elif re.search(r"\b(preferred pronouns?|pronouns?)\b", question):
            answer = identity.get("pronouns")
        elif re.search(r"\b(disability|disabled|impairment|medical condition|health condition|accommodation history)\b", question):
            disability = identity.get("disability") or {}
            expected = disability.get("history") if "history" in question else disability.get("current")
            if isinstance(expected, bool):
                answer = _render_boolean(expected, options)
        elif re.search(r"\b(graduation|graduate)\s+(?:date|month|year)\b", question):
            profile_education = PROFILE.get("education") or {}
            month = str(education.get("expected_graduation_month")
                        or profile_education.get("grad_month") or "")
            year = str(education.get("expected_graduation_year")
                       or profile_education.get("grad_year") or "")
            exact = education.get("exact_graduation_date")
            if control.get("hasDay"):
                answer = exact
            elif "year" in question and "date" not in question:
                answer = year
            elif "month" in question and "date" not in question:
                answer = month
            elif control.get("kind") == "date" and month and year:
                parsed = _date_parts(f"{month} {year}")
                answer = f"{parsed[0]:02d}/{year}" if parsed else None
            elif month and year:
                answer = f"{month} {year}"
        elif re.search(r"\b(high school|secondary school)\b", question):
            answer = _approved_high_school_answer(approved, options)
        elif re.search(r"\bhighest level of education\b", question):
            # David is currently pursuing a BS. Prefer an in-progress/some-college
            # label over a completed Bachelor's claim when the form offers one.
            if options:
                answer = _first_matching_option([
                    "Some college",
                    "College - currently enrolled",
                    "Currently pursuing a Bachelor's degree",
                ], options)
            else:
                answer = "Currently pursuing a Bachelor of Science"
        elif re.search(r"\b(current )?(major|area of study|field of study)\b", question):
            major = str((PROFILE.get("education") or {}).get("major") or "")
            candidates = [major]
            candidates.extend(part.strip() for part in re.split(r"\s*&\s*|\s+and\s+", major)
                              if part.strip())
            answer = _first_matching_option(candidates, options) if options else major
        elif re.search(r"\b(legally )?authorized to work\b", question):
            expected = (PROFILE.get("work_authorization") or {}).get("authorized_us")
            if isinstance(expected, bool):
                answer = _render_boolean(expected, options)
        elif re.search(r"\b(require|need).*(sponsorship|visa)|\bsponsorship\b.*\b(require|need)\b", question):
            expected = (PROFILE.get("work_authorization") or {}).get("needs_sponsorship")
            if isinstance(expected, bool):
                answer = _render_boolean(expected, options)
        elif re.search(r"\b(offer deadline|exploding offer|deadline to accept)\b", question):
            if offers:
                answer = offers[0].get("deadline") or offers[0].get("deadline_month")
                if control.get("hasDay") and not offers[0].get("deadline"):
                    answer = None
        elif re.search(r"\b(outstanding offer|competing offer|pending offer)\b", question):
            answer = _render_boolean(bool(offers), options)
            if offers and not options:
                companies = ", ".join(str(offer.get("company")) for offer in offers if offer.get("company"))
                answer = f"Yes, {companies}" if companies else "Yes"
        elif re.search(r"\bhave you (?:ever )?used\b.*\bbefore\b|\b(used|use|customer of|experience with|familiar with|have you tried)\b.*\b(our|this|the)\b.*\b(product|platform|app|service|software|tool)\b", question):
            expected = _company_fact(control, "used_product", approved)
            if isinstance(expected, bool):
                answer = _render_boolean(expected, options)
        elif re.search(r"\b(previously employed|prior employment|worked (?:at|for)|former(?:\s+\w+){0,4}\s+(?:employee|contingent worker))\b", question):
            expected = _company_fact(control, "prior_employment", approved)
            if isinstance(expected, bool):
                answer = _render_boolean(expected, options)
        elif re.search(r"\b(referral|referred|refer you|know anyone|current employee)\b", question):
            expected = _company_fact(control, "referral", approved)
            if isinstance(expected, bool):
                answer = _render_boolean(expected, options)
        elif re.search(r"\b(previously interviewed|interviewed (?:at|with|for)|applied (?:to|with)|prior application|previous application)\b", question):
            expected = _company_fact(control, "prior_interview_or_application", approved)
            if isinstance(expected, bool):
                answer = _render_boolean(expected, options)
        elif re.search(r"\b(finra|sie|securities industry essentials|professional licen[sc]e|certification|certified|plan to take the exam)\b", question):
            expected = _company_fact(control, "licenses_or_exams", approved)
            if isinstance(expected, bool):
                answer = _render_boolean(expected, options)
        elif re.search(r"\b(member of your household|household member|family member|relative)\b", question):
            expected = _company_fact(control, "household_employment", approved)
            if isinstance(expected, bool):
                answer = _render_boolean(expected, options)
        elif re.search(r"\b(schedule|hours|days? (?:a|per) week|in[- ]?office|onsite|hybrid)\b", question):
            expected = preferences.get("hybrid")
            if isinstance(expected, bool):
                answer = _render_boolean(expected, options)
        elif re.search(r"\blocal to the area\b", question):
            # Workday's "No" choices encode whether relocation assistance is
            # required. Prefer a company-specific approved label, then the
            # user's general relocation policy.
            expected = _company_fact(control, "local_to_advertised_area_answer", approved)
            if isinstance(expected, str):
                answer = _best_option(expected, options) if options else expected
            elif (preferences.get("relocation_assistance_required") is False
                  and (PROFILE.get("preferences") or {}).get("relocate_ok")):
                candidates = [
                    "No - I am willing to relocate & I do not require relocation assistance.",
                    "No, I am willing to relocate and do not require relocation assistance",
                    "No, willing to relocate without relocation assistance",
                ]
                answer = _first_matching_option(candidates, options) if options else candidates[0]
        elif re.search(r"\brelocation assistance\b", question):
            expected = preferences.get("relocation_assistance_required")
            if isinstance(expected, bool):
                answer = _render_boolean(expected, options)
        elif re.search(r"\b(future contact|marketing (?:email|communications?|consent)|talent community)\b", question):
            # Optional recruiting marketing is not required to apply.  Default
            # to the privacy-preserving choice unless David explicitly opts in.
            expected = preferences.get("future_contact")
            if not isinstance(expected, bool):
                expected = False
            answer = _render_boolean(expected, options)
        elif (re.search(r"\bwhich location\(s\).*open to working\b", question)
              and control.get("kind") == "checkgroup"
              and (PROFILE.get("preferences") or {}).get("relocate_ok")):
            # The profile explicitly says open to any listed office. Preserve a
            # list so Workday can select every checkbox instead of stringifying
            # it and accidentally choosing only one.
            answer = options or None
        if answer is not None:
            if options and not isinstance(answer, list):
                answer = _best_option(str(answer), options) or answer
            key = (control.get(key_field) if key_field != "id_or_name" else
                   control.get("id") or control.get("name") or control.get("label"))
            if key:
                rendered.append({key_field: key, "answer": answer})
    return rendered


def _company_answer_is_approved(control: dict, field: str, answers: dict,
                                answer: object) -> bool:
    expected = _company_fact(control, field, answers)
    if expected is _MISSING:
        return False
    if isinstance(expected, bool):
        return _answer_boolean(answer) is expected
    return str(expected).strip().lower() in str(answer or "").strip().lower()


def _blocked_answer_is_approved(control: dict, answer: object, approved: dict) -> bool:
    """Whether a normally-manual category has an explicit user-approved source."""
    question = _control_question_text(control).lower()
    identity = approved.get("identity") or {}
    preferences = approved.get("preferences") or {}
    legal = approved.get("legal") or {}
    offers = approved.get("current_offers") or []
    answer_text = str(answer or "")
    if re.search(r"\b(18 or older|at least (?:age )?18)\b", question):
        birth = _date_parts(identity.get("date_of_birth"))
        actual = _answer_boolean(answer_text)
        if not birth or actual is None:
            return False
        today = datetime.date.today()
        age = today.year - birth[2] - ((today.month, today.day) < (birth[0], birth[1] or 1))
        return actual is (age >= 18)
    if re.search(r"\b(date of birth|dob|birth date|birthday|age)\b", question):
        expected = identity.get("date_of_birth")
        return bool(expected and _date_parts(expected) == _date_parts(answer_text))
    if re.search(r"\b(graduation|graduate)\s+(?:date|month|year)\b", question):
        education = approved.get("education") or {}
        if control.get("hasDay") or re.search(r"\d{1,2}/\d{1,2}/\d{4}", answer_text):
            exact = education.get("exact_graduation_date")
            return bool(exact and _date_parts(exact) == _date_parts(answer_text))
        profile_education = PROFILE.get("education") or {}
        expected_month = education.get("expected_graduation_month") or profile_education.get("grad_month")
        expected_year = str(education.get("expected_graduation_year") or profile_education.get("grad_year") or "")
        supplied = _date_parts(answer_text)
        if supplied:
            month, day, year = supplied
            expected = _date_parts(f"{expected_month} {expected_year}")
            return bool(day is None and expected and (month, year) == (expected[0], expected[2]))
        if re.fullmatch(r"\s*\d{4}\s*", answer_text):
            return answer_text.strip() == expected_year
        return str(expected_month or "").lower() == answer_text.strip().lower()
    if re.search(r"\b(high school|secondary school)\b", question):
        expected = _approved_high_school_answer(
            approved,
            [str(option) for option in control.get("options") or []],
        )
        return bool(expected and expected.lower() == answer_text.strip().lower())
    if re.search(r"\b(preferred pronouns?|pronouns?)\b", question):
        expected = str(identity.get("pronouns") or "").strip().lower()
        return bool(expected and expected in answer_text.strip().lower())
    if re.search(r"\b(disability|disabled|impairment|medical condition|health condition|accommodation history)\b", question):
        disability = identity.get("disability") or {}
        expected = disability.get("history") if "history" in question else disability.get("current")
        return expected is not None and _answer_boolean(answer_text) is expected
    if re.search(r"\b(compensation|salary|pay (?:range|rate)|hourly rate|base pay|bonus|equity|expected (?:pay|salary)|desired (?:pay|salary))\b", question):
        if not preferences.get("compensation_policy"):
            return False
        numeric = bool(re.search(r"\$|\b\d+(?:\.\d+)?\b", answer_text))
        offered_options = [str(option) for option in control.get("options") or []]
        return not numeric or answer_text in offered_options
    if re.search(r"\b(offer deadline|exploding offer|deadline to accept)\b", question):
        for offer in offers:
            if not isinstance(offer, dict):
                continue
            exact = offer.get("deadline")
            month = offer.get("deadline_month")
            if control.get("hasDay") or re.search(r"\d{1,2}/\d{1,2}/\d{4}", answer_text):
                if exact and _date_parts(exact) == _date_parts(answer_text):
                    return True
            elif (exact or month) and _date_parts(exact or month) == _date_parts(answer_text):
                return True
        return False
    if re.search(r"\b(outstanding offer|competing offer|pending offer)\b", question):
        parsed = _answer_boolean(answer_text)
        if parsed is not None:
            return parsed is bool(offers)
        return bool(offers) and any(
            str(offer.get("company") or "").lower() in answer_text.lower()
            for offer in offers if isinstance(offer, dict)
        )
    if re.search(r"\bhave you (?:ever )?used\b.*\bbefore\b|\b(used|use|customer of|experience with|familiar with|have you tried)\b.*\b(our|this|the)\b.*\b(product|platform|app|service|software|tool)\b", question):
        return _company_answer_is_approved(control, "used_product", approved, answer)
    if re.search(r"\b(previously employed|prior employment|worked (?:at|for)|former(?:\s+\w+){0,4}\s+(?:employee|contingent worker))\b", question):
        return _company_answer_is_approved(control, "prior_employment", approved, answer)
    if re.search(r"\b(referral|referred|refer you|know anyone|current employee)\b", question):
        return _company_answer_is_approved(control, "referral", approved, answer)
    if re.search(r"\b(previously interviewed|interviewed (?:at|with|for)|applied (?:to|with)|prior application|previous application)\b", question):
        return _company_answer_is_approved(control, "prior_interview_or_application", approved, answer)
    if re.search(r"\b(finra|sie|securities industry essentials|professional licen[sc]e|certification|certified|plan to take the exam)\b", question):
        return _company_answer_is_approved(control, "licenses_or_exams", approved, answer)
    if re.search(r"\b(member of your household|household member|family member|relative)\b", question):
        return _company_answer_is_approved(control, "household_employment", approved, answer)
    if re.search(r"\b(non[- ]?compete|conflict of interest|conflicts?|restrictive covenant|moonlighting|outside employment)\b", question):
        return legal.get("non_compete_or_conflict") is not None
    if re.search(r"\b(security clearance|clearance level|secret clearance|top secret|ts/sci|public trust)\b", question):
        return legal.get("security_clearance") is not None
    if re.search(r"\btravel\b", question):
        return preferences.get("travel") is not None
    if re.search(r"\b(schedule|hours|days? (?:a|per) week|in[- ]?office|onsite|hybrid)\b", question):
        expected = preferences.get("hybrid")
        return expected is not None and _answer_boolean(answer_text) is expected
    if re.search(r"\blocal to the area\b", question):
        expected = _company_fact(control, "local_to_advertised_area_answer", approved)
        if isinstance(expected, str):
            return expected.strip().lower() == answer_text.strip().lower()
        return bool(
            preferences.get("relocation_assistance_required") is False
            and (PROFILE.get("preferences") or {}).get("relocate_ok")
            and re.search(r"\bno\b", answer_text, re.I)
            and re.search(r"\brelocat(?:e|ion)\b", answer_text, re.I)
            and re.search(r"\b(?:do not|don't|without)\b.*\bassist", answer_text, re.I)
        )
    if re.search(r"\brelocation assistance\b", question):
        expected = preferences.get("relocation_assistance_required")
        return isinstance(expected, bool) and _answer_boolean(answer_text) is expected
    if re.search(r"\b(future contact|marketing (?:email|communications?|consent)|talent community)\b", question):
        expected = preferences.get("future_contact")
        if not isinstance(expected, bool):
            expected = False
        return _answer_boolean(answer_text) is expected
    return False


def answer_requires_manual(control: dict, answer: object, profile_text: str | None = None,
                           approved_answers: dict | None = None) -> bool:
    """Pure fail-closed policy for questions needing explicit user facts/preferences.

    Returns True when a model answer must be dropped so optional fields remain blank
    and required fields naturally surface for manual completion.
    """
    question = _control_question_text(control).lower()
    if not question:
        return False
    approved = APPLICATION_ANSWERS if approved_answers is None else approved_answers
    if _blocked_answer_is_approved(control, answer, approved):
        return False
    if re.search(r"\b(disability|disabled|impairment|medical condition|health condition|accommodation history)\b", question):
        if profile_text and re.search(r"\b(disability|disabled|impairment|medical condition|health condition|accommodation)\b", profile_text, re.I):
            return False
    return any(re.search(p, question, re.I) for p in BLOCKED_QUESTION_PATTERNS)


def filter_manual_answers(controls: list[dict], answers: list[dict], profile_text: str | None = None,
                          key_field: str = "id_or_name", approved_answers: dict | None = None) -> tuple[list[dict], list[dict]]:
    """Return (allowed, blocked) answers using only inputs, with no side effects."""
    by_key = {}
    for c in controls:
        for k in (c.get("id"), c.get("name"), c.get("label"), c.get("faid")):
            if k:
                by_key.setdefault(k, c)
    allowed, blocked = [], []
    for a in answers:
        answer_key = a.get(key_field)
        c = by_key.get(answer_key, {"label": answer_key or ""})
        (blocked if answer_requires_manual(c, a.get("answer"), profile_text, approved_answers) else allowed).append(a)
    return allowed, blocked


def log_answer_decisions(controls: list[dict], allowed: list[dict], blocked: list[dict],
                         context: dict | None = None, key_field: str = "id_or_name") -> None:
    """Append attributable allowed/blocked decisions for later application review."""
    label_by_key = {}
    for c in controls:
        for k in (c.get("id"), c.get("name"), c.get("faid"), c.get("label")):
            if k:
                label_by_key.setdefault(k, c.get("label", ""))
    try:
        (ROOT / "out").mkdir(parents=True, exist_ok=True)
        with open(ROOT / "out" / "qa_answers.log", "a") as f:
            for decision, batch in (("allowed", allowed), ("blocked_manual", blocked)):
                for answer in batch:
                    answer_key = answer.get(key_field)
                    rec = {
                        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                        "decision": decision,
                        "question": label_by_key.get(answer_key, answer_key),
                        "answer": answer.get("answer"),
                    }
                    if re.search(r"\b(date of birth|dob|birth date|birthday|disability|medical condition)\b", str(rec["question"]), re.I):
                        rec["answer"] = "[redacted approved sensitive answer]"
                    if context:
                        rec.update({k: v for k, v in context.items() if v})
                    f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def harvest_select_options(page, controls: list[dict]) -> None:
    """React-select options only exist in the DOM while the menu is open.
    Open each combobox briefly to capture its options so Claude can answer exactly."""
    for c in controls:
        if not _is_react_select(c) or c["options"] or c.get("chosen") or c["value"]:
            continue
        sel = f"[id='{c['id']}']" if c["id"] else f"[name='{c['name']}']"
        try:
            # close any stray open menu first: click empty page corner
            page.mouse.click(5, 5)
            page.wait_for_timeout(250)
            el = page.locator(sel).first
            el.scroll_into_view_if_needed()
            el.click(timeout=2500)
            page.wait_for_timeout(700)
            # only read options if THIS control's menu is the open one
            expanded = el.evaluate("el => el.getAttribute('aria-expanded') === 'true'")
            if expanded:
                opts = el.evaluate("""
                    el => {
                        // react-select renders the menu inside the control's container
                        const shell = (el.parentElement || el).closest('.select__container, .select-shell, [class*=select]');
                        const menu = shell?.querySelector('.select__menu') || document.querySelector('.select__menu');
                        return menu ? [...menu.querySelectorAll('.select__option, [role=option]')].map(o => o.innerText.trim()) : [];
                    }
                """)
                c["options"] = [o for o in opts if o and "no options" not in o.lower()][:60]
            page.mouse.click(5, 5)
            page.wait_for_timeout(200)
        except Exception:
            page.mouse.click(5, 5)


def get_answers(controls: list[dict], context: dict | None = None) -> list[dict]:
    import datetime
    unanswered = [c for c in controls if not c["value"]]
    if not unanswered:
        return []
    context = context or {}
    company_context = str(context.get("company") or context.get("slug") or "")
    policy_controls = [dict(control, company_context=company_context) for control in unanswered]
    explicit = explicit_approved_answers(
        unanswered,
        company_context=company_context,
    )
    explicit_keys = {answer["id_or_name"] for answer in explicit}
    model_controls = [
        control for control in unanswered
        if (control.get("id") or control.get("name") or control.get("label")) not in explicit_keys
    ]
    model_answers = []
    if model_controls:
        model_answers = _model_answers(model_controls, company_context)
    answers, blocked = filter_manual_answers(
        policy_controls,
        explicit + model_answers,
    )
    # FULL-AUTO audit trail: every deterministic and model answer is logged.
    log_answer_decisions(policy_controls, answers, blocked, context=context)
    return answers


def _model_answers(controls: list[dict], company_context: str = "") -> list[dict]:
    """Ask the model for remaining answers, returning no guesses on API failure."""
    today = datetime.date.today().strftime("%m/%d/%Y")
    body = json.dumps({
        "model": MODEL, "max_tokens": 20000,  # reasoning tokens count against this;
        # 4000 truncated mid-array on 25-control forms (Zipline 2026-08-09)
        "messages": [{"role": "user", "content": ANSWER_PROMPT.format(
            profile=yaml.dump(PROFILE), controls=json.dumps(controls)[:20000],
            application_answers=yaml.safe_dump(
                relevant_application_answers(controls, company_context=company_context)
            ),
            stories=_grounding(), today=today)}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.load(r)
    except Exception as exc:
        print(f"[qa] answer model unavailable: {type(exc).__name__}", file=sys.stderr)
        return []
    text = "".join(b.get("text", "") for b in resp["content"] if b.get("type") == "text")
    m = re.search(r"\[.*\]", text, re.S)
    if m:
        answers = json.loads(m.group(0))
    else:
        # truncated output: salvage every complete object so a long form
        # degrades to partial fills instead of silently zero (fail-open)
        objs = re.findall(r'\{"id_or_name":.*?\}', text, re.S)
        answers = []
        for o in objs:
            try:
                answers.append(json.loads(o))
            except Exception:
                continue
        if answers:
            print(f"[qa] output truncated; salvaged {len(answers)} answers", file=sys.stderr)
    return answers


def _best_option(ans: str, options: list[str]) -> str | None:
    """Pick the menu option best matching the desired answer, else None.
    Word-boundary aware: 'Male' must NOT match 'Female'."""
    if not options:
        return None
    al = ans.lower().strip()
    # 1) exact
    for o in options:
        if o.lower().strip() == al:
            return o
    # 2) whole-phrase containment at word boundaries only
    def phrase_in(needle: str, hay: str) -> bool:
        return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", hay) is not None
    for o in options:
        ol = o.lower().strip()
        if phrase_in(al, ol) or phrase_in(ol, al):
            return o
    # 3) token overlap (conservative >=0.6; tokens are whole words so male!=female)
    atoks = set(re.findall(r"[a-z0-9]+", al))
    best, score = None, 0.0
    for o in options:
        otoks = set(re.findall(r"[a-z0-9]+", o.lower()))
        if not otoks:
            continue
        s = len(atoks & otoks) / len(atoks | otoks)
        if s > score:
            best, score = o, s
    return best if score >= 0.6 else None


def _is_react_select(c: dict) -> bool:
    return c["type"] == "combobox" or "select__input" in (c.get("cls") or "")


def fill_answers(page, controls: list[dict], answers: list[dict]) -> tuple[list[str], list[str]]:
    """Apply answers with post-fill verification. Returns (filled_labels, failed_labels)."""
    by_key = {}
    by_label = {}
    for c in controls:
        if c["id"]: by_key[c["id"]] = c
        if c["name"]: by_key.setdefault(c["name"], c)
        if c["label"]: by_label.setdefault(c["label"].lower().strip(), c)
    filled, failed = [], []
    for a in answers:
        c = by_key.get(a["id_or_name"])
        if not c:
            # ids/names can regenerate between page loads (Ashby); fall back to label match
            lab = str(a.get("label") or a["id_or_name"]).lower().strip()
            c = by_label.get(lab)
        if not c:
            failed.append(a["id_or_name"])
            continue
        if c["id"]:
            sel = f"[id=\"{c['id']}\"]"
        elif c["name"]:
            sel = f"[name=\"{c['name']}\"]"
        else:
            # no id/name (Ashby date pickers): find the input inside the field
            # wrapper whose text contains the question label
            lab = (c["label"] or "").replace('"', '\\"')[:80]
            sel = f"[class*=fieldEntry]:has-text(\"{lab}\") input, [class*=_fieldEntry]:has-text(\"{lab}\") input"
        ans = str(a["answer"])
        ok = False
        try:
            page.keyboard.press("Escape")  # dismiss any menu left open by a previous control
            el = page.locator(sel).first
            try:
                el.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass  # hidden inputs (Ashby yes/no) can't scroll; JS click path handles them
            if c["tag"] == "select":
                try:
                    el.select_option(label=ans)
                    ok = bool(el.evaluate("el => el.value"))
                except Exception:
                    ok = False
            elif c["type"] in ("group-checkbox", "group-radio"):
                # tick option(s) whose label matches; scope strictly to this input group
                wanted = a["answer"] if isinstance(a["answer"], list) else [a["answer"]]
                got = 0
                boxes = page.locator(f"input[name=\"{c['name']}\"]")
                nb = boxes.count()
                for w in wanted:
                    wl = str(w).lower()
                    for i in range(nb):
                        box = boxes.nth(i)
                        lab_txt = box.evaluate("el => el.labels?.[0]?.innerText || el.value || ''").strip()
                        if lab_txt and (wl in lab_txt.lower() or lab_txt.lower() in wl):
                            try:
                                box.check(timeout=2000)
                                got += 1
                            except Exception:
                                try:
                                    box.evaluate("el => el.labels?.[0]?.click()")
                                    got += 1
                                except Exception:
                                    pass
                            break
                if not got:
                    # Ashby-style: hidden checkbox + sibling Yes/No buttons
                    got = int(bool(boxes.first.evaluate("""
                        (el, ans) => {
                            const wrap = el.closest('[class*=yesno], [class*=_container]');
                            const btns = wrap ? [...wrap.querySelectorAll('button')] : [];
                            if (!btns.length) return false;
                            const want = ['yes','true','1','on'].includes(ans.toLowerCase()) ? 'yes' : 'no';
                            const btn = btns.find(b => b.innerText.trim().toLowerCase() === want);
                            if (btn) { btn.click(); return true; }
                            return false;
                        }
                    """, str(wanted[0]))) if nb else 0)
                ok = got > 0
            elif c["type"] in ("checkbox", "radio"):
                # Ashby-style yes/no: hidden checkbox with sibling Yes/No buttons
                handled = el.evaluate("""
                    (el, ans) => {
                        const wrap = el.closest('[class*=yesno], [class*=_container]');
                        if (!wrap) return false;
                        const btns = [...wrap.querySelectorAll('button')];
                        if (!btns.length) return false;
                        const want = ['yes','true','1','on'].includes(ans.toLowerCase()) ? 'yes' : 'no';
                        const btn = btns.find(b => b.innerText.trim().toLowerCase() === want);
                        if (btn) { btn.click(); return true; }
                        return false;
                    }
                """, ans)
                if not handled and ans.lower() in ("yes", "true", "1", "on"):
                    el.check()
                ok = True
            elif _is_react_select(c):
                VERIFY = """
                    el => {
                        if ((el.value || '').trim()) return true;
                        const shell = (el.parentElement || el).closest('.select-shell, .select__container, [class*=select-shell]');
                        const chosen = shell?.querySelector(
                            '.select__single-value, .select__multi-value, [class*=singleValue], [class*=single-value], [class*=multi-value]');
                        return !!(chosen && chosen.innerText.trim());
                    }
                """
                known = c.get("options") or []
                target = _best_option(ans, known) if known else None
                for attempt in range(2):
                    el = page.locator(sel).first
                    el.scroll_into_view_if_needed()
                    el.click(timeout=3000)
                    page.wait_for_timeout(600)
                    if known and len(known) <= 25 and target:
                        # short fixed menu: click the known option directly, no typing
                        # (non-searchable selects break on keystrokes)
                        opt = page.locator(
                            f".select__option:text-is(\"{target}\"), [role=option]:text-is(\"{target}\")").first
                        if not opt.count():
                            opt = page.locator(
                                f".select__option:has-text(\"{target[:40]}\"), [role=option]:has-text(\"{target[:40]}\")").first
                        if opt.count():
                            try:
                                opt.scroll_into_view_if_needed()
                                opt.click(timeout=3000)
                            except Exception:
                                pass
                    else:
                        # long/async list: type to filter, then click best visible match
                        page.keyboard.type(ans[:12], delay=25)
                        page.wait_for_timeout(1600)
                        vis = [o.strip() for o in page.locator(".select__option, [role=option]").all_inner_texts()]
                        vis = [o for o in vis if o and "no options" not in o.lower()]
                        pick = _best_option(ans, vis) or (vis[0] if len(vis) == 1 else None)
                        if pick:
                            opt = page.locator(
                                f".select__option:text-is(\"{pick}\"), [role=option]:text-is(\"{pick}\")").first
                            if opt.count():
                                try:
                                    opt.click(timeout=3000)
                                except Exception:
                                    page.keyboard.press("Enter")
                            else:
                                page.keyboard.press("Enter")
                        else:
                            page.keyboard.press("Enter")
                    page.wait_for_timeout(400)
                    ok = bool(el.evaluate(VERIFY))
                    if ok:
                        break
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(300)
            else:  # plain input / textarea
                el.fill(ans)
                ok = bool(el.evaluate("el => (el.value || '').trim().length > 0"))
            (filled if ok else failed).append(c["label"] or a["id_or_name"])
            page.wait_for_timeout(200)
        except Exception:
            failed.append(c["label"] or a["id_or_name"])
    page.keyboard.press("Escape")
    return filled, failed


def css_escape(s: str) -> str:
    return re.sub(r"([^a-zA-Z0-9_-])", r"\\\1", s)
