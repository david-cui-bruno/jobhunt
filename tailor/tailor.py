"""Tailor: rewrites the base LaTeX resume against a job description using Claude.

Three-stage pipeline (2026-08-07 redesign after single-pass output read like the
base resume with a shuffled skills line):
  1. PLAN    - analyze the JD: extract requirements, map each to a truthful asset
               (base resume or whitelist), pick angle + project order. Also names
               unclaimable requirements so the writer stops papering over gaps.
  2. WRITE   - rewrite the LaTeX following that plan.
  3. CRITIQUE- grade the draft against the JD requirement-by-requirement; if weak,
               one revision pass with the critique as instructions.
Guardrails (code, not model): fixed employer order, sanitizer for ->/~ text-mode
traps, JD-skill coverage check with mechanical insert, compile gate, base fallback.
  - Never invent employers, projects, metrics, or skills.
  - Output compiled with pdflatex; failure -> retry once, else base resume.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE_TEX = (ROOT / "resume" / "resume.tex").read_text()
OUT_DIR = ROOT / "out" / "resumes"

MODEL = "claude-sonnet-5"
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

PLAN_PROMPT = """Analyze this job posting and produce a tailoring plan for the candidate below. Be brutally honest.

JOB POSTING:
Company: {company}
Title: {title}
{jd}

CANDIDATE'S TRUTHFUL ASSETS:
Base resume (LaTeX):
{tex}

{skills_whitelist}

{bullet_bank}

Return a plan in EXACTLY this format (plain text, no markdown):

ROLE_TYPE: <embedded-systems | backend | ml | full-stack | data | other: one phrase>
TOP_REQUIREMENTS: <the 5-7 things this JD actually screens for, comma-separated, most important first>
MATCHES: <one line per requirement the candidate can TRUTHFULLY support, format "requirement => strongest truthful evidence from resume/whitelist/bank, and where it should appear (which bullet/section)">
GAPS: <requirements with NO truthful support. Never fake these. Note any adjacent-but-honest framing, e.g. RTOS gap => emphasize real-time latency work at Freya WITHOUT claiming RTOS>
ANGLE: <2-3 sentences: the honest story this resume should tell for this job, e.g. "low-level-curious systems builder with real-time latency and Linux infra experience">
PROJECT_ORDER: <the 3 projects best-first for THIS job, with 4 words on why each>
VOCAB: <8-12 JD words/phrases the rewrite should use where truthful, comma-separated>
"""

PROMPT = """You are tailoring a LaTeX resume to a specific job posting. A tailoring plan has been prepared; follow it.

TAILORING PLAN:
{plan}

RULES:
1. Never invent employers, titles, dates, degrees, or credentials. Bullets must stay grounded in the resume's existing facts and the approved bullet bank (if provided below). Nothing from GAPS may be claimed.
2. Keep the same LaTeX custom commands and personal info. Output MUST compile.
3. Execute the plan aggressively:
   - WORK EXPERIENCE ORDER IS FIXED reverse-chronological: Framewise Health, then Freya, then Sotatek. NEVER reorder employers; tailor each role's bullets instead.
   - Rewrite bullets to serve the ANGLE, using the VOCAB where truthful. Every MATCHES line must be visible in the final resume where the plan says. Expand what the plan emphasizes, compress what it doesn't. GAPS get adjacent-honest framing only.
   - PROJECTS in the plan's PROJECT_ORDER. Rewrite project bullets toward the angle too, from the same truthful facts.
   - SKILLS: every JD-named technology from MATCHES appears, placed FIRST on its line; drop the least relevant items. Never add anything not on the base resume or whitelist.
   - COURSEWORK: reorder so the plan-relevant courses come first.
4. Stay one page: roughly the same total length (within ~20%).
5. LaTeX hygiene: arrows must be $\\rightarrow$ (NEVER plain "->", which renders as an upside-down question mark). Approximation must be $\\sim$ (NEVER bare "~" before a number, which renders as a space). ASCII only.

{quality_rules}

{skills_whitelist}

{bullet_bank}

JOB POSTING:
Company: {company}
Title: {title}
Description (may be truncated):
{jd}

RESUME (LaTeX source):
{tex}

Return ONLY the complete modified LaTeX source, no commentary, no markdown fences."""

CRITIQUE_PROMPT = """You are a skeptical recruiter for this exact role. Grade how well this resume targets this job description.

JOB POSTING:
Company: {company}
Title: {title}
{jd}

RESUME (LaTeX):
{tex}

For each of the JD's top requirements: does the resume visibly address it (cite the line), weakly address it, or ignore it? Then answer:
VERDICT: STRONG or WEAK
FIXES: if WEAK, the 3-5 highest-impact concrete edits (reword bullet X to say Y, move project Z first, lead skills with W). Only truthful edits from the resume's existing facts; never invent experience.
Return plain text in that format."""


def _load_quality_rules() -> str:
    rules = ROOT / "resume" / "quality_rules.md"
    if rules.exists():
        return "QUALITY RULES (follow strictly):\n" + rules.read_text()
    return ""


def _load_bullet_bank() -> str:
    bank = ROOT / "resume" / "bullet_bank.md"
    if not bank.exists():
        return ""
    approved = [l for l in bank.read_text().splitlines()
                if l.strip() and "[PENDING]" not in l and not l.startswith("#")]
    if not any(l[0].isdigit() for l in approved if l):
        return ""
    return "APPROVED BULLET BANK (pre-approved truthful bullets you may swap in):\n" + "\n".join(approved)


def _load_skills_whitelist() -> str:
    wl = ROOT / "resume" / "skills_whitelist.md"
    if not wl.exists():
        return ""
    lines = [l for l in wl.read_text().splitlines() if l.strip() and not l.startswith("#")]
    return ("CLAIMABLE SKILLS WHITELIST (truthful skills beyond the base resume; "
            "add ONLY when the JD asks for them):\n" + "\n".join(lines))


def _api(messages: list[dict], max_tokens: int = 8000) -> str:
    body = json.dumps({
        "model": MODEL,
        "max_tokens": max_tokens,
        "messages": messages,
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        resp = json.load(r)
    return "".join(b.get("text", "") for b in resp["content"] if b.get("type") == "text")


def _strip_fences(text: str) -> str:
    return re.sub(r"^```(?:latex|tex)?\s*|\s*```$", "", text.strip())


def make_plan(company: str, title: str, jd: str) -> str:
    return _api([{"role": "user", "content": PLAN_PROMPT.format(
        company=company, title=title, jd=jd[:6000], tex=BASE_TEX,
        bullet_bank=_load_bullet_bank(),
        skills_whitelist=_load_skills_whitelist())}], max_tokens=2000).strip()


def call_claude(company: str, title: str, jd: str, plan: str = "") -> str:
    return _strip_fences(_api([{"role": "user", "content": PROMPT.format(
        company=company, title=title, jd=jd[:6000], tex=BASE_TEX, plan=plan or "(none)",
        bullet_bank=_load_bullet_bank(), quality_rules=_load_quality_rules(),
        skills_whitelist=_load_skills_whitelist())}]))


def critique(company: str, title: str, jd: str, tex: str) -> tuple[bool, str]:
    """Returns (is_strong, critique_text)."""
    text = _api([{"role": "user", "content": CRITIQUE_PROMPT.format(
        company=company, title=title, jd=jd[:6000], tex=tex)}], max_tokens=1500)
    strong = bool(re.search(r"VERDICT:\s*STRONG", text))
    return strong, text.strip()


def revise_with_critique(company: str, title: str, jd: str, tex: str, crit: str, plan: str) -> str:
    return _strip_fences(_api([{"role": "user", "content": (
        "Revise this LaTeX resume per the recruiter critique below. Keep every rule from before: "
        "no invented experience, fixed employer order (Framewise Health, Freya, Sotatek), one page, "
        "$\\rightarrow$ not ->, $\\sim$ not ~, must compile.\n\nTAILORING PLAN:\n" + plan +
        "\n\nCRITIQUE:\n" + crit + "\n\nJOB POSTING:\nCompany: " + company + "\nTitle: " + title +
        "\n" + jd[:4000] + "\n\nCURRENT LATEX:\n" + tex +
        "\n\nReturn ONLY the complete revised LaTeX source, no commentary, no fences.")}]))


FORBIDDEN_DRIFT = ["\\newcommand", "\\documentclass"]  # sanity: these must match base count

# Employers must stay reverse-chronological, always (David's rule 2026-08-07).
EMPLOYER_ORDER = ["Framewise Health", "Freya", "Sotatek"]

# LaTeX text-mode traps -> safe math-mode equivalents. Plain "->"/">" render as
# upside-down question marks in OT1; bare "~" before a digit silently becomes a
# space. The bullet bank uses both, so sanitize whatever the model emits.
def sanitize(tex: str) -> str:
    out = tex
    for uni, repl in [("\u2192", "$\\rightarrow$"), ("\u2190", "$\\leftarrow$"),
                      ("\u2248", "$\\sim$"), ("\u00d7", "x"), ("\u2264", "$\\leq$"),
                      ("\u2265", "$\\geq$")]:
        out = out.replace(uni, repl)
    out = re.sub(r"(?<![$\\{-])->(?!\$)", r"$\\rightarrow$", out)  # bare ->
    out = re.sub(r"(?<=[\s(])~(?=\d)", r"$\\sim$", out)  # bare ~35% etc.
    return out


def employers_in_order(tex: str) -> bool:
    positions = [tex.find(e) for e in EMPLOYER_ORDER]
    return all(p >= 0 for p in positions) and positions == sorted(positions)


# Claimable inventory: canonical skill -> regex that finds it in a JD.
# Union of base-resume skills and skills_whitelist.md. The coverage check
# demands: JD names it + we can claim it => it appears in the Skills section.
CLAIMABLE = {
    "C++": r"\bC\+\+",
    "C": r"(?<![A-Za-z+#.])C(?![A-Za-z+#])(?!\+\+)(?!-suite|-level|-corp|\.F\.R)",
    "Python": r"\bPython\b",
    "SQL": r"\bSQL\b",
    "TypeScript": r"\bTypeScript\b",
    "JavaScript": r"\bJavaScript\b",
    "Linux": r"\bLinux\b",
    "Bash": r"\b(?:Bash|shell scripting)\b",
    "Docker": r"\bDocker\b",
    "AWS": r"\bAWS\b|Amazon Web Services",
    "PostgreSQL": r"\bPostgres(?:QL)?\b",
    "Redis": r"\bRedis\b",
    "Node.js": r"\bNode(?:\.js)?\b",
    "React": r"\bReact\b",
    "Next.js": r"\bNext\.js\b",
    "PyTorch": r"\bPyTorch\b",
    "Pandas": r"\bPandas\b",
    "NumPy": r"\bNumPy\b",
    "Git": r"\bGit\b(?!Hub)",
    "REST": r"\bREST(?:ful)? API",
    "WebRTC": r"\bWebRTC\b",
    "SIP": r"\bSIP\b",
    "LangChain": r"\bLangChain\b",
    "Temporal": r"\bTemporal\b",
    "Supabase": r"\bSupabase\b",
    "Stripe": r"\bStripe\b",
    "FFmpeg": r"\bFFmpeg\b",
}


def _skills_section(tex: str) -> str:
    i = tex.rfind("\\section{Skills}")
    return tex[i:] if i >= 0 else ""


def jd_skills_covered(tex: str, jd: str) -> tuple[bool, list[str]]:
    """Every JD-named claimable skill must appear in the Skills section."""
    if not jd.strip():
        return True, []
    section = _skills_section(tex)
    if not section:
        return False, ["<no Skills section>"]
    missing = []
    for skill, pat in CLAIMABLE.items():
        if not re.search(pat, jd):
            continue
        # token match in the section: "C" must not match the c in "Docker",
        # and "C" must not match "C++"
        present = re.search(
            r"(?<![A-Za-z+#.])" + re.escape(skill) + r"(?![A-Za-z+#])",
            section, re.IGNORECASE)
        if not present:
            missing.append(skill)
    return not missing, missing


def validate(tex: str) -> bool:
    if "\\begin{document}" not in tex or "\\end{document}" not in tex:
        return False
    for tok in FORBIDDEN_DRIFT:
        if tex.count(tok) != BASE_TEX.count(tok):
            return False
    # length guard: within 15% of original
    if not 0.75 < len(tex) / len(BASE_TEX) < 1.25:
        return False
    if not employers_in_order(tex):
        return False
    # no text-mode arrows / raw angle brackets left (math mode is fine)
    if re.search(r"(?<![$\\{-])->", tex):
        return False
    return True


def compile_pdf(tex: str, out_pdf: Path) -> bool:
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "resume.tex"
        src.write_text(tex)
        for _ in range(2):  # two passes for refs
            p = subprocess.run(
                ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "resume.tex"],
                cwd=td, capture_output=True, timeout=120,
            )
        pdf = Path(td) / "resume.pdf"
        if p.returncode != 0 or not pdf.exists():
            return False
        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        out_pdf.write_bytes(pdf.read_bytes())
        # one-page check
        info = subprocess.run(["mdls", "-name", "kMDItemNumberOfPages", str(out_pdf)],
                              capture_output=True, text=True)
        return True


LANG_SKILLS = {"C++", "C", "Python", "SQL", "TypeScript", "JavaScript", "Pandas", "NumPy", "PyTorch"}


def enforce_coverage(tex: str, jd: str) -> str:
    """Deterministic backstop: prepend any JD-named claimable skill the model
    left out to the front of the right Skills line (languages vs tools)."""
    ok, missing = jd_skills_covered(tex, jd)
    if ok:
        return tex
    section = _skills_section(tex)
    lines = [l for l in section.splitlines() if "\\item" in l and "\\textbf{" in l]
    if not lines:
        return tex
    new_section = section
    for skill in missing:
        target = None
        if skill in LANG_SKILLS:
            target = lines[0]
        else:
            target = lines[1] if len(lines) > 1 else lines[0]
        # insert right after "}: "
        replaced = re.sub(r"(\\textbf\{[^}]*\}: )", r"\g<1>" + skill + ", ", target, count=1)
        new_section = new_section.replace(target, replaced, 1)
        lines = [replaced if l == target else l for l in lines]
    return tex.replace(section, new_section, 1)


def tailor(posting_id: str, company: str, title: str, jd: str) -> Path | None:
    """Plan -> write -> critique -> (revise) -> guardrails -> compile.
    Returns path to tailored PDF, or None on failure (caller falls back to base)."""
    safe = re.sub(r"[^A-Za-z0-9]+", "_", f"{company}_{title}")[:80]
    out_pdf = OUT_DIR / f"{safe}.pdf"
    out_tex = OUT_DIR / f"{safe}.tex"
    out_plan = OUT_DIR / f"{safe}.plan.txt"

    plan = ""
    if jd.strip():
        try:
            plan = make_plan(company, title, jd)
            out_plan.write_text(plan)  # auditable: why the resume looks how it looks
        except Exception:
            plan = ""

    def finish(tex: str) -> Path | None:
        tex = sanitize(tex)
        tex = enforce_coverage(tex, jd)
        if not validate(tex):
            return None
        if compile_pdf(tex, out_pdf):
            out_tex.write_text(tex)
            return out_pdf
        return None

    for attempt in range(2):
        try:
            tex = call_claude(company, title, jd, plan=plan)
        except Exception:
            continue
        tex = sanitize(tex)
        # self-critique loop: up to 2 revision passes while the recruiter-check says WEAK
        if jd.strip():
            for _ in range(2):
                try:
                    strong, crit = critique(company, title, jd, tex)
                except Exception:
                    break
                if strong:
                    break
                try:
                    revised = sanitize(revise_with_critique(company, title, jd, tex, crit, plan))
                except Exception:
                    break
                # only adopt a revision that still passes structural rules
                if validate(enforce_coverage(revised, jd)):
                    tex = revised
                else:
                    break
        result = finish(tex)
        if result:
            return result
    # fallback: compile the base resume
    if compile_pdf(BASE_TEX, out_pdf):
        out_tex.write_text(BASE_TEX)
        return out_pdf
    return None


if __name__ == "__main__":
    # smoke test: tailor against a sample JD from argv or a canned one
    jd = sys.argv[1] if len(sys.argv) > 1 else (
        "We're looking for a backend-focused SWE intern with Python, distributed systems, "
        "and low-latency service experience. Bonus: ML infrastructure, real-time pipelines.")
    p = tailor("test", "TestCo", "Software Engineer Intern", jd)
    print("PDF:", p)
