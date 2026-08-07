"""Tailor: rewords the base LaTeX resume against a job description using Claude.

Strict rules enforced by prompt + validation:
  - Reword/reorder ONLY. No new employers, projects, metrics, or skills.
  - Same structure and roughly same length (must still fit one page).
  - Output is full LaTeX, compiled to PDF with pdflatex; compile failure -> retry once, else fall back to base resume.
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

PROMPT = """You are tailoring a LaTeX resume to a specific job posting.

RULES:
1. Never invent employers, titles, dates, degrees, or credentials. Bullets must stay grounded in the resume's existing facts and the approved bullet bank (if provided below).
2. Keep the same LaTeX custom commands and personal info. Output MUST compile.
3. AGGRESSIVE TAILORING ENCOURAGED:
   - WORK EXPERIENCE ORDER IS FIXED reverse-chronological: Framewise Health, then Freya, then Sotatek. NEVER reorder employers. Tailor a role by rewriting its bullets, not by moving the role.
   - Rewrite work-experience bullets to foreground whatever in that role is closest to THIS job: mirror the JD's vocabulary and emphases, expand relevant bullets, compress or swap out irrelevant ones (using the APPROVED BULLET BANK when its bullets fit better).
   - Reorder PROJECTS so the most relevant to this job comes first. Reorder bullets within a role/project by relevance.
   - SKILLS MUST BE VISIBLY TAILORED to this JD. Step 1: list (to yourself) the concrete technologies the JD names. Step 2: for each, if it appears on the base resume OR in the CLAIMABLE SKILLS WHITELIST below, it MUST appear in the Skills section, placed FIRST on its line. Step 3: drop the least relevant existing items to make room. A reader comparing Skills to the JD must immediately see the overlap. NEVER claim anything not on the base resume or whitelist (no RTOS/CAN/QNX/Kubernetes/etc. unless whitelisted).
   - COURSEWORK MUST BE REWEIGHTED: put the 2-3 courses closest to this JD first (embedded/systems role: Computer Systems, Computer Architecture first; ML role: Machine Learning, Deep Learning first). You may append a truthful whitelist-backed phrasing like "(C)" markers only if natural.
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


def call_claude(company: str, title: str, jd: str) -> str:
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 8000,
        "messages": [{"role": "user", "content": PROMPT.format(
            company=company, title=title, jd=jd[:6000], tex=BASE_TEX,
            bullet_bank=_load_bullet_bank(), quality_rules=_load_quality_rules(),
            skills_whitelist=_load_skills_whitelist())}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.load(r)
    text = "".join(b.get("text", "") for b in resp["content"] if b.get("type") == "text")
    text = re.sub(r"^```(?:latex|tex)?\s*|\s*```$", "", text.strip())
    return text


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
    """Returns path to tailored PDF, or None on failure (caller falls back to base)."""
    safe = re.sub(r"[^A-Za-z0-9]+", "_", f"{company}_{title}")[:80]
    out_pdf = OUT_DIR / f"{safe}.pdf"
    out_tex = OUT_DIR / f"{safe}.tex"
    for attempt in range(2):
        try:
            tex = call_claude(company, title, jd)
        except Exception:
            continue
        tex = sanitize(tex)
        tex = enforce_coverage(tex, jd)
        if not validate(tex):
            continue
        if compile_pdf(tex, out_pdf):
            out_tex.write_text(tex)
            return out_pdf
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
