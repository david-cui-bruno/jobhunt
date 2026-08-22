#!/usr/bin/env python3
"""Create a secure runtime env file from existing jobhunt launchd plists."""

from __future__ import annotations

import argparse
import os
import plistlib
import tempfile
from pathlib import Path

from runtime_secrets import ALLOWED_SECRET_KEYS

ALLOWED_NON_SECRET_ENV_KEYS = frozenset({"PATH", "PYTHONUNBUFFERED"})


def _jobhunt_plists(launch_agents: Path) -> list[Path]:
    return sorted(Path(launch_agents).glob("com.jobhunt.*.plist"))


def _read_plist(path: Path) -> dict[str, object]:
    with path.open("rb") as handle:
        payload = plistlib.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"invalid plist payload in {path.name}")
    return payload


def _validate_environment_key(key: str) -> None:
    if key in ALLOWED_SECRET_KEYS or key in ALLOWED_NON_SECRET_ENV_KEYS or key.startswith("JOBHUNT_"):
        return
    raise ValueError(f"unknown environment key {key!r}")


def _atomic_write_mode_600(output: Path, content: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    fd = -1
    temp_name = ""
    try:
        fd, temp_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, output)
        os.chmod(output, 0o600)
    except Exception:
        if fd >= 0:
            os.close(fd)
        if temp_name:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
        raise


def migrate(launch_agents: Path, output: Path) -> None:
    """Collect identical allowlisted launchd secrets and write *output* securely."""
    launch_agents = Path(launch_agents)
    output = Path(output)
    collected: dict[str, str] = {}

    for plist_path in _jobhunt_plists(launch_agents):
        payload = _read_plist(plist_path)
        environment = payload.get("EnvironmentVariables", {})
        if environment is None:
            continue
        if not isinstance(environment, dict):
            raise ValueError(f"invalid EnvironmentVariables in {plist_path.name}")

        for key, value in environment.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError(f"invalid environment entry in {plist_path.name}")
            _validate_environment_key(key)
            if key not in ALLOWED_SECRET_KEYS:
                continue
            existing = collected.get(key)
            if existing is not None and existing != value:
                raise ValueError(f"conflicting value for {key}")
            collected[key] = value

    lines = [f"{key}={collected[key]}\n" for key in sorted(collected)]
    _atomic_write_mode_600(output, "".join(lines))
    if collected:
        print("Migrated keys: " + ", ".join(sorted(collected)))
    else:
        print("Migrated keys: none")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate jobhunt launchd secrets to a runtime env file")
    parser.add_argument("--launch-agents", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    migrate(args.launch_agents, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
