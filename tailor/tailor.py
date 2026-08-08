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
import shutil
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
IDEAL_SKILLS: <describe the hiring manager's IDEAL candidate for this exact posting: the skills, tools, and experiences they dream of seeing, comma-separated, INCLUDING ones this candidate lacks>
MATCHES: <one line per requirement/ideal-skill the candidate can TRUTHFULLY support, format "requirement => strongest truthful evidence from resume/whitelist/bank, and where it should appear (which bullet/section)">
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
   - IMPACT FIRST, AND BOLD IT: every work/project bullet must contain a concrete outcome (metric, scale, or consequence) stated as impressively as the truth allows, and the impact phrase must be wrapped in \\textbf{{...}}. Exactly one bold phrase per bullet, covering just the outcome (e.g. \\textbf{{cutting p99 turn latency $\\sim$35\\%}}), never the whole bullet.
   - PROJECTS in the plan's PROJECT_ORDER. Rewrite project bullets toward the angle too, from the same truthful facts.
   - SKILLS: every JD-named technology from MATCHES appears, placed FIRST on its line; drop the least relevant items. Never add anything not on the base resume or whitelist.
   - COURSEWORK: reorder so the plan-relevant courses come first.
4. FILL the page: exactly 1 page with no big blank band at the bottom. If content runs short, add one more truthful bullet (bullet bank or plan MATCHES) to the most JD-relevant role or project rather than leaving whitespace.
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

For each of the JD's top requirements: does the resume visibly address it (cite the line), weakly address it, or ignore it? Also check: does every bullet state a concrete outcome with the impact phrase bolded (\\textbf), and is the page well used (no more than ~15% trailing whitespace implied by sparse content)? Then answer:
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
    # NB max_tokens must be generous: the model's internal reasoning counts
    # toward it, and a starved budget truncated plans to ~190 chars (seen live).
    return _api([{"role": "user", "content": PLAN_PROMPT.format(
        company=company, title=title, jd=jd[:6000], tex=BASE_TEX,
        bullet_bank=_load_bullet_bank(),
        skills_whitelist=_load_skills_whitelist())}], max_tokens=6000).strip()


def call_claude(company: str, title: str, jd: str, plan: str = "") -> str:
    # 20k: reasoning tokens count toward max_tokens on this model; 8k truncated
    # the LaTeX mid-document (observed live: 0.73 ratio, no \end{document}).
    return _strip_fences(_api([{"role": "user", "content": PROMPT.format(
        company=company, title=title, jd=jd[:6000], tex=BASE_TEX, plan=plan or "(none)",
        bullet_bank=_load_bullet_bank(), quality_rules=_load_quality_rules(),
        skills_whitelist=_load_skills_whitelist())}], max_tokens=20000))


def critique(company: str, title: str, jd: str, tex: str) -> tuple[bool, str]:
    """Returns (is_strong, critique_text)."""
    text = _api([{"role": "user", "content": CRITIQUE_PROMPT.format(
        company=company, title=title, jd=jd[:6000], tex=tex)}], max_tokens=4000)
    strong = bool(re.search(r"VERDICT:\s*STRONG", text))
    return strong, text.strip()


def revise_with_critique(company: str, title: str, jd: str, tex: str, crit: str, plan: str) -> str:
    return _strip_fences(_api([{"role": "user", "content": (
        "Revise this LaTeX resume per the recruiter critique below. Keep every rule from before: "
        "no invented experience, fixed employer order (Framewise Health, Freya, Sotatek), one page, "
        "$\\rightarrow$ not ->, $\\sim$ not ~, must compile.\n\nTAILORING PLAN:\n" + plan +
        "\n\nCRITIQUE:\n" + crit + "\n\nJOB POSTING:\nCompany: " + company + "\nTitle: " + title +
        "\n" + jd[:4000] + "\n\nCURRENT LATEX:\n" + tex +
        "\n\nReturn ONLY the complete revised LaTeX source, no commentary, no fences.")}],
        max_tokens=20000))


FORBIDDEN_DRIFT = ["\\newcommand", "\\documentclass"]  # sanity: these must match base count

# Per-role education variants (David, 2026-08-07): Brown's open curriculum lets
# him declare/switch concentration freely, so embedded/hardware roles present
# the EE+CS direction he may declare; everything else keeps the current line.
# Applied deterministically after generation (never left to the model).
EDU_BASE_RE = r"B\.S\. in Computer Science \\& Economics"
EDU_VARIANTS = {
    "embedded": "B.S. in Electrical Engineering \\& Computer Science",
    "hardware": "B.S. in Electrical Engineering \\& Computer Science",
}


# Per-role coursework pools drawn ONLY from resume/courses.md (verified with
# David 2026-08-07; he has taken every course listed there, ENGN 2912 excluded).
# The variant line replaces the base Coursework list wholesale so the model
# can't drop real courses or invent new ones; ordering = screening relevance.
COURSE_VARIANTS = {
    "embedded": "Real-Time \\& Embedded Software, Digital Electronics Systems Design, Design of Computing Systems (Computer Architecture), Operating Systems (Weenix kernel), Electrical Circuits \\& Signals, Computer Networks, Multiprocessor Synchronization, Linear Systems \\& Signals",
    "hardware": "Digital Electronics Systems Design, Design of Computing Systems (Computer Architecture), Real-Time \\& Embedded Software, Electrical Circuits \\& Signals, Electricity \\& Magnetism, Operating Systems (Weenix kernel), Linear Systems \\& Signals, Communication Systems",
    "backend": "Distributed Systems, Computer Networks, Operating Systems (Weenix kernel), Databases, Multiprocessor Synchronization, Design \\& Analysis of Algorithms, Computer Systems Security, Software Security",
    "ml": "Machine Learning, Deep Learning, Computer Vision, Design \\& Analysis of Algorithms, Distributed Systems, Linear Algebra, Statistics, Operating Systems",
    "full-stack": "Distributed Systems, Computer Networks, Databases, Operating Systems (Weenix kernel), Design \\& Analysis of Algorithms, Software Security, Machine Learning, Deep Learning",
    "data": "Databases, Distributed Systems, Machine Learning, Design \\& Analysis of Algorithms, Statistics, Computer Networks, Deep Learning, Linear Algebra",
    "security": "Software Security \\& Exploitation, Computer Systems Security, Operating Systems (Weenix kernel), Computer Networks, Compilers, Distributed Systems, Multiprocessor Synchronization, Theory of Computation",
}


def apply_course_variant(tex: str, role_type: str) -> str:
    for key, courses in COURSE_VARIANTS.items():
        if key in role_type:
            return re.sub(
                r"(\\resumeItem\{Coursework\}\s*\{)[^}]+(\})",
                lambda m: m.group(1) + courses + m.group(2),
                tex, count=1)
    return tex


# Grad date is role-dependent (David 2026-08-08): internship applications say
# May 2028 (returning to school after), full-time say May 2027. Base tex has 2027.
GRAD_INTERN = "May 2028"
GRAD_FULLTIME = "May 2027"


def apply_grad_date(tex: str, title: str) -> str:
    is_intern = bool(re.search(r"\bintern|co[- ]?op\b", title, re.I))
    target = GRAD_INTERN if is_intern else GRAD_FULLTIME
    return re.sub(r"Aug 2024 -- May 202[0-9]", f"Aug 2024 -- {target}", tex, count=1)


def parse_role_type(plan: str) -> str:
    m = re.search(r"ROLE_TYPE:\s*([^\n]+)", plan)
    return m.group(1).strip().lower() if m else ""


def apply_education_variant(tex: str, role_type: str) -> str:
    for key, repl in EDU_VARIANTS.items():
        if key in role_type:
            return re.sub(EDU_BASE_RE, lambda _m: repl, tex, count=1)
    return tex

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


def validate(tex: str, why: list | None = None) -> bool:
    def fail(reason: str) -> bool:
        if why is not None:
            why.append(reason)
        return False
    if "\\begin{document}" not in tex or "\\end{document}" not in tex:
        return fail("missing document env")
    for tok in FORBIDDEN_DRIFT:
        if tex.count(tok) != BASE_TEX.count(tok):
            return fail(f"drift: {tok} count {tex.count(tok)} != {BASE_TEX.count(tok)}")
    # length guard: within 25% of original
    if not 0.75 < len(tex) / len(BASE_TEX) < 1.25:
        return fail(f"length ratio {len(tex)/len(BASE_TEX):.2f} outside 0.75..1.25")
    if not employers_in_order(tex):
        return fail("employer order violated")
    # no text-mode arrows / raw angle brackets left (math mode is fine)
    if re.search(r"(?<![$\\{-])->", tex):
        return fail("raw -> present")
    # education = degree + ONE Coursework bullet, nothing else (David 2026-08-08)
    edu = tex[tex.find("EDUCATION"):tex.find("EXPERIENCE")]
    n_bullets = len(re.findall(r"\\resumeItem(?:NH)?\{", edu))
    if n_bullets > 1:
        return fail("extra bullet in Education (only the Coursework line is allowed)")
    return True


PDFLATEX = shutil.which("pdflatex") or "/Library/TeX/texbin/pdflatex"


def compile_pdf(tex: str, out_pdf: Path) -> bool:
    """Compile; also records the page count from pdflatex's log into
    LAST_PAGE_COUNT (object streams make counting pages from PDF bytes
    unreliable, but the log line 'Output written on resume.pdf (N pages' is
    authoritative)."""
    global LAST_PAGE_COUNT
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "resume.tex"
        src.write_text(tex)
        for _ in range(2):  # two passes for refs
            p = subprocess.run(
                [PDFLATEX, "-interaction=nonstopmode", "-halt-on-error", "resume.tex"],
                cwd=td, capture_output=True, timeout=120,
            )
        pdf = Path(td) / "resume.pdf"
        if p.returncode != 0 or not pdf.exists():
            return False
        m = re.search(rb"Output written on resume\.pdf \((\d+) page", p.stdout or b"")
        if not m:
            log = (Path(td) / "resume.log")
            if log.exists():
                m = re.search(rb"Output written on resume\.pdf \((\d+) page", log.read_bytes())
        LAST_PAGE_COUNT = int(m.group(1)) if m else 0
        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        out_pdf.write_bytes(pdf.read_bytes())
        return True


LAST_PAGE_COUNT = 0


def measure_fill(pdf: Path) -> float:
    """Fraction of the page height actually used (0..1): renders page 1 and
    finds the lowest row with ink. Pure stdlib: pdftoppm -> PGM bytes."""
    try:
        with tempfile.TemporaryDirectory() as td:
            subprocess.run(["pdftoppm", "-gray", "-r", "50", "-f", "1", "-l", "1",
                            str(pdf), f"{td}/pg"], capture_output=True, timeout=60)
            pgms = sorted(Path(td).glob("pg*.pgm"))
            if not pgms:
                return 1.0
            data = pgms[0].read_bytes()
            # P5 header: magic, width height, maxval, then raw bytes
            parts = data.split(b"\n", 3)
            w, h = (int(x) for x in parts[1].split())
            raw = parts[3][-(w * h):]
            last_ink = 0
            for row in range(h):
                seg = raw[row * w:(row + 1) * w]
                if any(b < 128 for b in seg):
                    last_ink = row
            return last_ink / h
    except Exception:
        return 1.0  # measurement failure must never block a resume


EXPAND_PROMPT = """This LaTeX resume leaves too much blank space: content ends at {fill_pct}% of the page. Make it fill the page (while staying EXACTLY 1 page):
- Add 1-2 more truthful bullets to the most JD-relevant roles/projects, drawn ONLY from the approved bullet bank or the tailoring plan's MATCHES evidence below. Never invent facts.
- Or expand the most JD-relevant existing bullets with truthful specifics already present in the source material.
Keep employer order Framewise Health, Freya, Sotatek. Keep the bold-impact convention (\\textbf on each bullet's outcome phrase). Keep $\\rightarrow$/$\\sim$ hygiene.

TAILORING PLAN:
{plan}

{bullet_bank}

JOB POSTING (for relevance):
{jd}

LATEX:
{tex}

Return ONLY the complete LaTeX source, no commentary, no fences."""


def expand_to_fill(tex: str, jd: str, plan: str, fill: float) -> str:
    return _strip_fences(_api([{"role": "user", "content": EXPAND_PROMPT.format(
        fill_pct=int(fill * 100), plan=plan[:3000], jd=jd[:3000], tex=tex,
        bullet_bank=_load_bullet_bank())}], max_tokens=20000))


def log_skill_gaps(posting_id: str, company: str, plan: str) -> None:
    """Append this JD's unmet ideals to a ledger so recurring gaps become the
    roadmap for which portfolio projects to build next (David 2026-08-08:
    'if I can't hit those ideals, make a project so that I can')."""
    try:
        gaps = re.search(r"GAPS:\s*(.+?)(?:\n[A-Z_]+:|\Z)", plan, re.S)
        ideals = re.search(r"IDEAL_SKILLS:\s*(.+?)(?:\n[A-Z_]+:|\Z)", plan, re.S)
        with (OUT_DIR.parent / "skill_gaps.log").open("a") as f:
            f.write(f"--- {posting_id} | {company}\n")
            if ideals:
                f.write(f"IDEAL: {' '.join(ideals.group(1).split())}\n")
            if gaps:
                f.write(f"GAPS: {' '.join(gaps.group(1).split())}\n")
    except Exception:
        pass


SHRINK_PROMPT = """This LaTeX resume compiles to {pages} pages; it MUST fit exactly 1 page.
Cut the weakest content for this job until it fits: drop the least relevant project entirely, trim bullets to at most 2 lines, compress the coursework line to the 5-6 most relevant courses. Do NOT touch employers, dates, or personal info. Keep employer order Framewise Health, Freya, Sotatek. Keep $\\rightarrow$/$\\sim$ hygiene.

JOB POSTING (for relevance judgment):
{jd}

LATEX:
{tex}

Return ONLY the complete LaTeX source, no commentary, no fences."""


def shrink_to_one_page(tex: str, jd: str, pages: int) -> str:
    return _strip_fences(_api([{"role": "user", "content": SHRINK_PROMPT.format(
        pages=pages, jd=jd[:3000], tex=tex)}], max_tokens=20000))


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
    if not API_KEY:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set: refusing to run (would silently "
            "fall back to the base resume for every posting)")
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
        role = parse_role_type(plan)
        tex = apply_education_variant(tex, role)
        tex = apply_course_variant(tex, role)
        tex = apply_grad_date(tex, title)
        tex = enforce_coverage(tex, jd)
        why: list = []
        if not validate(tex, why):
            print(f"[tailor] validate failed: {'; '.join(why)}", file=sys.stderr)
            return None
        if not compile_pdf(tex, out_pdf):
            print("[tailor] pdflatex failed", file=sys.stderr)
            return None
        # hard one-page gate with up to 2 shrink passes
        for _ in range(2):
            pages = LAST_PAGE_COUNT
            if pages <= 1:
                break
            print(f"[tailor] {pages} pages; shrinking", file=sys.stderr)
            try:
                smaller = sanitize(shrink_to_one_page(tex, jd, pages))
            except Exception:
                return None
            swhy: list = []
            if not validate(smaller, swhy):
                print(f"[tailor] shrink validate failed: {'; '.join(swhy)}", file=sys.stderr)
                return None
            if not compile_pdf(smaller, out_pdf):
                return None
            tex = smaller
        if LAST_PAGE_COUNT > 1:
            print("[tailor] still >1 page after shrinks", file=sys.stderr)
            return None
        # whitespace gate: if content ends high on the page, expand with
        # truthful bullets (measured, not guessed; 0.88 leaves normal margins)
        if jd.strip():
            for _ in range(2):
                fill = measure_fill(out_pdf)
                if fill >= 0.88:
                    break
                print(f"[tailor] page only {fill:.0%} full; expanding", file=sys.stderr)
                try:
                    bigger = sanitize(expand_to_fill(tex, jd, plan, fill))
                except Exception:
                    break
                ewhy: list = []
                if not validate(enforce_coverage(bigger, jd), ewhy):
                    print(f"[tailor] expand validate failed: {'; '.join(ewhy)}", file=sys.stderr)
                    break
                if not compile_pdf(bigger, out_pdf) or LAST_PAGE_COUNT > 1:
                    compile_pdf(tex, out_pdf)  # restore the good one
                    break
                tex = bigger
        out_tex.write_text(tex)
        log_skill_gaps(posting_id, company, plan)
        return out_pdf

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
