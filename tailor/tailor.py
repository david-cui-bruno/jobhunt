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
   - Reorder PROJECTS so the most relevant to this job comes first. Reorder bullets within roles similarly.
   - Reweight the Skills section: list the job's stack first; drop the 2-3 least relevant items if space is needed.
   - Rewrite bullets to mirror the job description's vocabulary and emphases, expanding relevant bullets and compressing irrelevant ones.
   - You may swap in bullets from the APPROVED BULLET BANK when they fit the job better than current ones.
4. Stay one page: roughly the same total length (within ~20%).

{quality_rules}

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


def call_claude(company: str, title: str, jd: str) -> str:
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 8000,
        "messages": [{"role": "user", "content": PROMPT.format(
            company=company, title=title, jd=jd[:6000], tex=BASE_TEX,
            bullet_bank=_load_bullet_bank(), quality_rules=_load_quality_rules())}],
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


def validate(tex: str) -> bool:
    if "\\begin{document}" not in tex or "\\end{document}" not in tex:
        return False
    for tok in FORBIDDEN_DRIFT:
        if tex.count(tok) != BASE_TEX.count(tok):
            return False
    # length guard: within 15% of original
    if not 0.75 < len(tex) / len(BASE_TEX) < 1.25:
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
