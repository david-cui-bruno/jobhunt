"""Form Q&A engine: extract form controls, have Claude map them to profile answers,
fill them. Questions Claude can't answer from profile facts -> returned as 'manual'.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PROFILE = yaml.safe_load((ROOT / "profile" / "profile.yaml").read_text())
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
    return t.replace(/\\s+/g, ' ').trim().slice(0, 200);
  };
  const groupInfo = (el) => {
    // checkbox/radio group: same name; group question label = wrapper's first label-ish text
    const boxes = [...document.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`)];
    const wrap = el.closest('fieldset, [role=group], div[class*=question], div[class*=checkbox]')
      || boxes[0]?.parentElement?.parentElement;
    let q = wrap?.querySelector('legend, .label, label:not([for])')?.innerText || '';
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
    if (el.offsetParent === null) return;
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

Additional standing instructions:
- Compensation expectation questions: answer "Open / market rate" or pick the no-preference option; if a number is required, use market-rate intern comp for the role's industry.
- Outstanding offers/deadlines: No.
- Willing to relocate: Yes. Open to any listed office location; prefer SF then NYC if ranked. If preferred cities are not offered, choose any offered US city over non-US.
- If a select's options are provided, your answer MUST be copied verbatim from the options list (character for character). Pick the option most consistent with the profile.
- How did you hear about us: "Company website" or closest option.
- Signature blocks: "Name"/"Signature" = the candidate's full legal name; "Date" = today's date {today} (use the format the field implies, default MM/DD/YYYY).
- Consent/acknowledgment checkboxes (privacy policy, accurate-info attestations, future contact): Yes/agree.
- Previous employment at this company / referral: No.
- Non-compete / can you work legally: consistent with profile (US citizen, no sponsorship needed).
- Internship history: yes, completed software engineering internships (see resume); none at a hedge fund/prop firm unless resume says otherwise.

Form controls (JSON): {controls}

Return a JSON array, one entry per control you can answer: {{"id_or_name": ..., "answer": ...}}.
- For selects/comboboxes, answer must EXACTLY match one of the provided options (or be a close prefix for autocomplete widgets).
- Only answer what the profile + instructions justify. For anything genuinely personal or unanswerable (essays, "why us", salary numbers you can't infer), OMIT it.
- Dates: month names and 4-digit years as separate controls demand.
Return ONLY the JSON array."""


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


def get_answers(controls: list[dict]) -> list[dict]:
    import datetime
    unanswered = [c for c in controls if not c["value"]]
    if not unanswered:
        return []
    today = datetime.date.today().strftime("%m/%d/%Y")
    body = json.dumps({
        "model": MODEL, "max_tokens": 4000,
        "messages": [{"role": "user", "content": ANSWER_PROMPT.format(
            profile=yaml.dump(PROFILE), controls=json.dumps(unanswered)[:20000],
            today=today)}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.load(r)
    text = "".join(b.get("text", "") for b in resp["content"] if b.get("type") == "text")
    m = re.search(r"\[.*\]", text, re.S)
    return json.loads(m.group(0)) if m else []


def _best_option(ans: str, options: list[str]) -> str | None:
    """Pick the menu option best matching the desired answer, else None."""
    if not options:
        return None
    al = ans.lower().strip()
    for o in options:
        if o.lower().strip() == al:
            return o
    for o in options:
        ol = o.lower().strip()
        if al in ol or ol in al:
            return o
    # token overlap fallback (conservative: >=0.6 to avoid e.g. 'Aalborg University'
    # matching 'Brown University' on the shared token)
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
    for c in controls:
        if c["id"]: by_key[c["id"]] = c
        if c["name"]: by_key.setdefault(c["name"], c)
    filled, failed = [], []
    for a in answers:
        c = by_key.get(a["id_or_name"])
        if not c:
            failed.append(a["id_or_name"])
            continue
        sel = f"[id=\"{c['id']}\"]" if c["id"] else f"[name=\"{c['name']}\"]"
        ans = str(a["answer"])
        ok = False
        try:
            page.keyboard.press("Escape")  # dismiss any menu left open by a previous control
            el = page.locator(sel).first
            el.scroll_into_view_if_needed()
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
                ok = got > 0
            elif c["type"] in ("checkbox", "radio"):
                if ans.lower() in ("yes", "true", "1", "on"):
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
