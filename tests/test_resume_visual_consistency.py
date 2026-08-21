from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
RESUME = ROOT / "resume" / "resume.tex"


def test_projects_do_not_mix_inline_bold_claims() -> None:
    source = RESUME.read_text()
    projects = source.split("\\section*{Projects}", 1)[1].split("\\section{Skills}", 1)[0]

    assert "\\textbf{" not in projects
