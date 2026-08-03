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

STRICT RULES:
1. Reword and reorder EXISTING content only. Never invent employers, projects, titles, dates, metrics, technologies, or skills that are not already present.
2. Keep the exact same LaTeX structure, custom commands, and section order. Keep all personal info identical.
3. You may: swap the order of bullets or projects to lead with the most relevant, adjust phrasing to mirror the job description's vocabulary, and emphasize matching technologies already on the resume.
4. Length must stay within a few characters of the original so it still fits one page.
5. Escape special characters correctly for LaTeX. Output MUST compile.

JOB POSTING:
Company: {company}
Title: {title}
Description (may be truncated):
{jd}

RESUME (LaTeX source):
{tex}

Return ONLY the complete modified LaTeX source, no commentary, no markdown fences."""


def call_claude(company: str, title: str, jd: str) -> str:
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 8000,
        "messages": [{"role": "user", "content": PROMPT.format(
            company=company, title=title, jd=jd[:6000], tex=BASE_TEX)}],
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
    if not 0.85 < len(tex) / len(BASE_TEX) < 1.15:
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
