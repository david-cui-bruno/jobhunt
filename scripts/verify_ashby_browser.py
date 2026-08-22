#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apply.ashby_browser import PROFILE_DIR, ChromeTarget, persistent_ashby_context, resolve_chrome


class VisibilityEvidenceError(RuntimeError):
    """Visibility evidence could not be queried safely."""


def _sync_playwright():
    from playwright.sync_api import sync_playwright

    return sync_playwright()


def _visibility_for_process(process_name: str) -> dict[str, Any]:
    # Import is deliberately local so --check-only never invokes System Events or
    # the macOS hiding helper. This query is only for dedicated about:blank smoke.
    from apply.hide_macos_browser import visible_windows_for_process

    try:
        windows = visible_windows_for_process(process_name)
    except Exception as exc:
        raise VisibilityEvidenceError(str(exc)) from exc
    return {"process_name": process_name, "visible": bool(windows), "window_count": len(windows)}


def _target_payload(target: ChromeTarget) -> dict[str, Any]:
    return {
        "dedicated": target.dedicated,
        "executable_path": str(target.executable),
        "process_name": target.process_name,
    }


def _blocker(message: str, *, mode: str, reason: str | None = None) -> dict[str, Any]:
    blocker = {"code": "fail_closed", "message": message}
    if reason is not None:
        blocker["reason"] = reason
    return {"mode": mode, "ok": False, "blocker": blocker}


def _run_about_blank(target: ChromeTarget) -> dict[str, Any]:
    if not target.dedicated:
        raise RuntimeError("Ashby about:blank verification requires a dedicated Chrome for Testing target.")

    with _sync_playwright() as pw:
        with persistent_ashby_context(pw) as context:
            page = context.pages[0] if getattr(context, "pages", None) else context.new_page()
            page.goto("about:blank")
            user_agent = page.evaluate("navigator.userAgent")
            webdriver = page.evaluate("navigator.webdriver")
            plugin_count = page.evaluate("navigator.plugins.length")
            try:
                visibility = _visibility_for_process(target.process_name)
            except Exception as exc:
                if isinstance(exc, VisibilityEvidenceError):
                    raise
                raise VisibilityEvidenceError(str(exc)) from exc

    return {
        "mode": "about-blank",
        "ok": True,
        "executable_path": str(target.executable),
        "process_name": target.process_name,
        "profile_path": str(PROFILE_DIR),
        "user_agent": user_agent,
        "navigator_webdriver": webdriver,
        "plugin_count": plugin_count,
        "process_visibility": visibility,
    }


def _emit_value(key: str, value: Any) -> None:
    if isinstance(value, dict):
        for nested_key in sorted(value):
            _emit_value(f"{key}.{nested_key}", value[nested_key])
        return
    print(f"{key}: {value}")


def _emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, sort_keys=True))
        return
    if payload.get("ok"):
        print(f"{payload['mode']}: ok")
        for key, value in payload.items():
            if key not in {"mode", "ok"}:
                _emit_value(key, value)
    else:
        blocker = payload["blocker"]
        print(f"{payload['mode']}: blocked ({blocker['code']}): {blocker['message']}")


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify Ashby dedicated browser readiness without visiting an ATS.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check-only", action="store_true", help="Resolve and report the target without launching a browser.")
    mode.add_argument("--about-blank", action="store_true", help="Run a dedicated Chrome for Testing about:blank smoke.")
    parser.add_argument("--json", action="store_true", help="Emit one machine-readable JSON object and no extra stdout.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parse(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    mode = "check-only" if args.check_only else "about-blank"
    try:
        target = resolve_chrome()
        if args.check_only:
            payload = {"mode": mode, "ok": True, "target": _target_payload(target)}
        else:
            if not target.dedicated:
                raise RuntimeError("Ashby about:blank verification requires a dedicated Chrome for Testing target.")
            payload = _run_about_blank(target)
    except Exception as exc:
        reason = "query_error" if isinstance(exc, VisibilityEvidenceError) else None
        payload = _blocker(str(exc), mode=mode, reason=reason)
        _emit(payload, as_json=args.json)
        return 1

    _emit(payload, as_json=args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
