from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
RESUME = ROOT / "resume" / "resume.tex"


def test_framewise_description_omits_team_size() -> None:
    source = RESUME.read_text()
    framewise = source.split("Framewise Health", 1)[1].split("\\resumeSubheading", 1)[0]

    assert "2-person" not in framewise
