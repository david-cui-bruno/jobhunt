from __future__ import annotations

import re
from pathlib import Path


def safe_screenshot(page, slug: str, stage: str, *, root: Path) -> str | None:
    root.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{slug}_{stage}").strip("._")
    while ".." in safe:
        safe = safe.replace("..", ".")
    path = root / f"{safe[:160]}.png"
    try:
        page.screenshot(path=str(path), full_page=True, timeout=1000)
        return str(path)
    except Exception:
        return None
