from pathlib import Path

from apply.artifacts import safe_screenshot


class FakePage:
    def __init__(self) -> None:
        self.paths: list[str] = []

    def screenshot(self, *, path: str, full_page: bool, timeout: int) -> None:
        assert full_page is True
        assert timeout == 1000
        self.paths.append(path)
        Path(path).write_bytes(b"png")


class FailingPage:
    def screenshot(self, **kwargs) -> None:
        raise RuntimeError("browser closed")


def test_safe_screenshot_returns_local_path_and_sanitizes_stage(tmp_path):
    page = FakePage()
    path = safe_screenshot(page, "Acme role", "filled\n../../bad", root=tmp_path)
    assert path is not None
    assert Path(path).parent == tmp_path
    assert ".." not in Path(path).name
    assert page.paths == [path]


def test_safe_screenshot_is_best_effort(tmp_path):
    assert safe_screenshot(FailingPage(), "Acme", "filled", root=tmp_path) is None
