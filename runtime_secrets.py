#!/usr/bin/env python3
"""Load launchd runtime secrets from an owner-only env file and exec a command."""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

ALLOWED_SECRET_KEYS = frozenset({"ANTHROPIC_API_KEY"})
DEFAULT_RUNTIME_ENV = Path.home() / ".config" / "jobhunt" / "runtime.env"


def _validate_runtime_env_path(path: Path) -> os.stat_result:
    if path.is_symlink():
        raise PermissionError("runtime env file must not be a symlink")

    info = os.stat(path)
    if info.st_uid != os.getuid():
        raise PermissionError("runtime env file must be owned by the current user")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise PermissionError("runtime env file must have mode 0600")
    if not stat.S_ISREG(info.st_mode):
        raise PermissionError("runtime env file must be a regular file")
    return info


def load_runtime_env(path: Path) -> dict[str, str]:
    """Read allowlisted runtime secrets from *path* after strict file checks."""
    runtime_path = Path(path).expanduser()
    _validate_runtime_env_path(runtime_path)

    loaded: dict[str, str] = {}
    with runtime_path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.rstrip("\n")
            if line.endswith("\r"):
                line = line[:-1]
            if not line:
                continue
            if "=" not in line:
                raise ValueError(f"malformed runtime env line {line_number}")
            key, value = line.split("=", 1)
            if not key or key.strip() != key or value == "" or "\x00" in key or "\x00" in value:
                raise ValueError(f"malformed runtime env line {line_number}")
            if key not in ALLOWED_SECRET_KEYS:
                raise ValueError(f"unknown runtime env key on line {line_number}")
            loaded[key] = value
    return loaded


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load jobhunt runtime secrets and exec a command")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_RUNTIME_ENV)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("expected: runtime_secrets.py [--env-file PATH] -- <command> [args...]")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    secrets = load_runtime_env(args.env_file)
    merged_env = os.environ.copy()
    merged_env.update(secrets)
    os.execvpe(args.command[0], args.command, merged_env)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
