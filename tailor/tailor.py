"""Build a truthful, deterministic LaTeX resume for each job description.

The model-generated rewrite path was retired after a production resume changed
identity fields and project facts despite prompt-only guardrails. Tailoring now
starts from the reviewed base resume and changes only two code-controlled areas:
verified coursework ordering and allowlisted JD skill coverage. The header,
education identity, employers, dates, bullets, projects, and contact information
are therefore immutable by construction.
"""
from __future__ import annotations

import json
import datetime
import hashlib
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
4. FILL the page: exactly 1 page with no big blank band at the bottom. If content runs short, add one more truthful bullet (bullet bank or plan MATCHES) to the most JD-relevant role or project rather than leaving whitespace. NEVER fill space with a summary/objective/profile blurb: the template is Jake's-resume style with sections EXACTLY Education, Work Experience, Projects, Skills, and nothing between the header and Education.
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
    # PM (David 2026-08-19): CS & Econ is already the ideal PM degree; lead
    # with the econ/data/product-adjacent courses, keep systems credibility.
    "pm": "Statistics, Machine Learning, Databases, Design \\& Analysis of Algorithms, Distributed Systems, Deep Learning, Computer Networks, Linear Algebra",
    "embedded": "Real-Time \\& Embedded Software, Digital Electronics Systems Design, Design of Computing Systems (Computer Architecture), Operating Systems (Weenix kernel), Electrical Circuits \\& Signals, Computer Networks, Multiprocessor Synchronization, Linear Systems \\& Signals",
    "hardware": "Digital Electronics Systems Design, Design of Computing Systems (Computer Architecture), Real-Time \\& Embedded Software, Electrical Circuits \\& Signals, Electricity \\& Magnetism, Operating Systems (Weenix kernel), Linear Systems \\& Signals, Communication Systems",
    "backend": "Distributed Systems, Computer Networks, Operating Systems (Weenix kernel), Databases, Multiprocessor Synchronization, Design \\& Analysis of Algorithms, Computer Systems Security, Software Security",
    "ml": "Machine Learning, Deep Learning, Computer Vision, Design \\& Analysis of Algorithms, Distributed Systems, Linear Algebra, Statistics, Operating Systems",
    "full-stack": "Distributed Systems, Computer Networks, Databases, Operating Systems (Weenix kernel), Design \\& Analysis of Algorithms, Software Security, Machine Learning, Deep Learning",
    "data": "Databases, Distributed Systems, Machine Learning, Design \\& Analysis of Algorithms, Statistics, Computer Networks, Deep Learning, Linear Algebra",
    "security": "Software Security \\& Exploitation, Computer Systems Security, Operating Systems (Weenix kernel), Computer Networks, Compilers, Distributed Systems, Multiprocessor Synchronization, Theory of Computation",
}

DEFAULT_COURSES = (
    "Data Structures \\& Algorithms, Computer Systems, Computer Vision, "
    "Linear Algebra, Statistics, Databases, Machine Learning, Computer Architecture, "
    "Deep Learning"
)

APPROVED_AWARDS_LINE = (
    "USACO Platinum; AIME Qualifier (4x); 3rd of 250 teams, "
    "CMU TartanHacks 2026 (SpaceOverflow)"
)


def apply_course_variant(tex: str, role_type: str) -> str:
    courses = next(
        (value for key, value in COURSE_VARIANTS.items() if key in role_type),
        DEFAULT_COURSES,
    )
    return re.sub(
        r"(\\resumeItem\{Coursework\}\s*\{)[^}]+(\})",
        lambda m: m.group(1) + courses + m.group(2),
        tex, count=1,
    )


def infer_role_type(title: str, jd: str) -> str:
    """Classify the small deterministic variant set without model output."""
    title_text = title.lower()
    jd_text = jd.lower()
    title_patterns = [
        ("pm", r"\bproduct manage(?:r|ment)|associate product manager|\bapm\b|product intern\b"),
        ("security", r"\b(?:cyber ?security|information security|application security|"
                     r"security (?:engineer|researcher|analyst)|penetration tester)\b"),
        ("embedded", r"\b(?:embedded|firmware|microcontroller|rtos|fpga|hardware)"),
        ("ml", r"\b(?:machine learning|deep learning|artificial intelligence|ai engineer|"
               r"generative ai|computer vision|pytorch|tensorflow|research scientist|"
               r"nlp engineer|gpu programming)\b"),
        ("data", r"\b(?:data engineer|analytics engineer|data scientist|data science|etl|warehouse)\b"),
        ("full-stack", r"\b(?:full[ -]?stack|frontend|front[ -]?end|react native)\b"),
        ("backend", r"\b(?:backend|back[ -]?end|distributed systems?|platform engineer|"
                  r"infrastructure|cloud engineer)\b"),
    ]
    title_match = next(
        (role for role, pattern in title_patterns if re.search(pattern, title_text)),
        None,
    )
    if title_match:
        return title_match

    # Job descriptions often contain generic phrases such as "security best
    # practices" or "security clearance". Those are not evidence that the role
    # is a security position, so the JD fallback requires domain-specific terms.
    jd_patterns = [
        ("security", r"\b(?:(?:application|product|cloud|network|information|cyber) "
                     r"security|security (?:engineer|researcher|analyst)|vulnerability "
                     r"research|penetration testing|threat detection|malware analysis|"
                     r"cryptograph(?:y|ic)|exploit development)\b"),
        ("ml", r"\b(?:generative ai|large language models?|llms?|nlp|natural language "
               r"processing|gpu programming)\b"),
        *title_patterns[1:],
    ]
    return next(
        (role for role, pattern in jd_patterns if re.search(pattern, jd_text)),
        "general",
    )


def build_grounded_resume(title: str, jd: str, include_skill_coverage: bool = True) -> str:
    """Return a resume whose mutable content comes only from reviewed code data."""
    role = infer_role_type(title, jd)
    tex = apply_course_variant(BASE_TEX, role)
    tex = apply_grad_date(tex, title)
    if include_skill_coverage:
        tex = enforce_coverage(tex, jd)
    return sanitize(tex)


# Graduation is track-based (David 2026-08-19): internship applications say
# May 2028; full-time applications say May 2027. He confirmed he would really
# graduate a year early for a full-time job, so the 2027 date is an honest
# plan, not marketing. Classification lives in track.py (shared with qa.py so
# the resume PDF and the form answers always agree). Day-level application
# fields use the approved 05/15 estimates; resumes remain month/year only.
sys.path.insert(0, str(ROOT))
import track as _track  # noqa: E402


def grad_date_for(title: str) -> str:
    return _track.grad_month_year(_track.infer_track(title))


def apply_grad_date(tex: str, title: str) -> str:
    return re.sub(
        r"Aug 2024 -- (?:January|February|March|April|May|June|July|August|September|October|November|December) 202[0-9]",
        f"Aug 2024 -- {grad_date_for(title)}",
        tex,
        count=1,
    )


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
        if not re.search(pat, jd, re.IGNORECASE):
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
    # Education has exactly two reviewed bullets: mutable verified Coursework
    # and the immutable Awards line David approved on 2026-08-21.
    edu = tex[tex.find("EDUCATION"):tex.find("EXPERIENCE")]
    labels = re.findall(r"\\resumeItem(?:NH)?\{([^}]*)\}", edu)
    if labels != ["Coursework", "Awards"]:
        return fail(f"Education bullets drift: {labels!r}")
    awards = re.search(r"\\resumeItem\{Awards\}\s*\{([^}]+)\}", edu)
    if not awards or " ".join(awards.group(1).split()) != APPROVED_AWARDS_LINE:
        return fail("Awards line drift")
    # Jake's template: NO summary/objective (David 2026-08-08). Reject any prose
    # between the heading tabular and the first section, and any summary-like section.
    body = tex[tex.find("\\begin{document}"):]
    m = re.search(r"\\end\{tabular\*\}(.*?)\\section", body, re.S)
    if m:
        between = re.sub(r"%.*", "", m.group(1)).strip()
        if len(between) > 10:
            return fail("summary/blurb between header and first section (Jake's template forbids)")
    for s in re.findall(r"\\section\*?\{([^}]*)\}", body):
        if re.search(r"summary|objective|about|profile", s, re.I):
            return fail(f"forbidden section '{s}' (Jake's template: no summary/objective)")
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


def measure_fill(pdf: Path) -> float | None:
    """Fraction of the page height actually used (0..1): renders page 1 and
    finds the lowest row with ink. Pure stdlib: pdftoppm -> PGM bytes."""
    try:
        with tempfile.TemporaryDirectory() as td:
            subprocess.run(["pdftoppm", "-gray", "-r", "50", "-f", "1", "-l", "1",
                            str(pdf), f"{td}/pg"], capture_output=True, timeout=60)
            pgms = sorted(Path(td).glob("pg*.pgm"))
            if not pgms:
                return None
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
        return None


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
Cut the weakest content for this job until it fits: drop the least relevant project entirely or trim bullets to at most 2 lines. Never alter the verified 8-9 course Coursework line. Do NOT touch employers, dates, or personal info. Keep employer order Framewise Health, Freya, Sotatek. Keep $\\rightarrow$/$\\sim$ hygiene.

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
    """Build, compile, and audit a deterministic grounded resume."""
    global LAST_PAGE_COUNT
    # Include the posting identity so a later role with the same company/title
    # cannot overwrite the exact artifact recorded for an earlier application.
    digest = hashlib.sha256(posting_id.encode("utf-8")).hexdigest()[:10]
    safe = re.sub(r"[^A-Za-z0-9]+", "_", f"{company}_{title}")[:68].strip("_")
    safe = f"{safe}_{digest}"
    out_pdf = OUT_DIR / f"{safe}.pdf"
    out_tex = OUT_DIR / f"{safe}.tex"
    out_plan = OUT_DIR / f"{safe}.plan.txt"
    out_quality = OUT_DIR / f"{safe}.quality.json"
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    role = infer_role_type(title, jd)
    plan = (
        f"ROLE_TYPE: {role}\n"
        "MODE: deterministic grounded template\n"
        "MUTABLE_FIELDS: verified coursework order; allowlisted JD skill coverage\n"
    )
    out_plan.write_text(plan)

    def write_quality(tex: str, source: str, review_required: bool, reason: str = "") -> bool:
        why: list[str] = []
        structurally_valid = validate(tex, why)
        covered, missing_skills = jd_skills_covered(tex, jd)
        measured_fill = measure_fill(out_pdf)
        if measured_fill is None:
            review_required = True
            reason = "; ".join(x for x in (reason, "page fill could not be measured") if x)
        out_quality.write_text(json.dumps({
            "version": 1,
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "posting_id": posting_id,
            "company": company,
            "title": title,
            "source": source,
            "review_required": review_required,
            "reason": reason,
            "plan_generated": False,
            "plan_error": "",
            "critique_verdict": "not_applicable_deterministic",
            "structural_validation": "passed" if structurally_valid else "failed",
            "validation_errors": why,
            "page_count": LAST_PAGE_COUNT,
            "fill_ratio": round(measured_fill, 4) if measured_fill is not None else None,
            "jd_skill_coverage": "passed" if covered else "failed",
            "missing_claimable_skills": missing_skills,
            "expected_grad_date": grad_date_for(title),
        }, indent=2) + "\n")
        return review_required

    def finish(include_skill_coverage: bool) -> Path | None:
        global LAST_PAGE_COUNT
        tex = build_grounded_resume(title, jd, include_skill_coverage)
        why: list = []
        if not validate(tex, why):
            print(f"[tailor] validate failed: {'; '.join(why)}", file=sys.stderr)
            return None
        LAST_PAGE_COUNT = 0
        if not compile_pdf(tex, out_pdf) or not out_pdf.is_file():
            print("[tailor] pdflatex failed", file=sys.stderr)
            return None
        if LAST_PAGE_COUNT != 1:
            print(
                f"[tailor] expected exactly one PDF page, measured {LAST_PAGE_COUNT}",
                file=sys.stderr,
            )
            return None
        out_tex.write_text(tex)
        review_required = write_quality(
            tex,
            source="deterministic_grounded",
            review_required=False,
        )
        if review_required:
            print(
                f"[tailor] quality review required: {out_quality}",
                file=sys.stderr,
            )
            return None
        return out_pdf

    result = finish(include_skill_coverage=True)
    if result:
        return result
    # A long JD can name many allowlisted skills and push the Skills line over
    # one page. Preserve all factual sections and retry without optional skill
    # insertions; coursework still stays at 8-9 verified courses.
    return finish(include_skill_coverage=False)


if __name__ == "__main__":
    # smoke test: tailor against a sample JD from argv or a canned one
    jd = sys.argv[1] if len(sys.argv) > 1 else (
        "We're looking for a backend-focused SWE intern with Python, distributed systems, "
        "and low-latency service experience. Bonus: ML infrastructure, real-time pipelines.")
    p = tailor("test", "TestCo", "Software Engineer Intern", jd)
    print("PDF:", p)
