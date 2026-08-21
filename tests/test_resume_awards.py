from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
RESUME = ROOT / "resume" / "resume.tex"


def test_primary_resume_lists_exact_awards_and_tartanhacks_rank() -> None:
    source = RESUME.read_text()

    assert r"\resumeItem{Awards}" in source
    assert "USACO Platinum" in source
    assert "AIME Qualifier (4x)" in source
    assert "3rd of 250 teams, CMU TartanHacks 2026 (SpaceOverflow)" in source
    assert "placed 3rd of 250 teams at CMU TartanHacks 2026" in source
    assert "Top 5 at TartanHacks" not in source
