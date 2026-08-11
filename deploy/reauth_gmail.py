"""Reauthorize Gmail using a local browser and save the token under jobhunt/secrets.

Run on the VPS behind an SSH tunnel so Google redirects to the forwarded local
port. The client secret is intentionally not part of the repository.
"""
from __future__ import annotations

import os
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

ROOT = Path(os.environ.get("JOBHUNT_ROOT", Path(__file__).resolve().parents[1]))
CLIENT = ROOT / "secrets" / "gmail_client.json"
TOKEN = ROOT / "secrets" / "gmail_token.json"
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]


def main() -> None:
    if not CLIENT.exists():
        raise SystemExit(f"Missing OAuth client file: {CLIENT}")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT), SCOPES)
    creds = flow.run_local_server(
        port=8765,
        open_browser=False,
        authorization_prompt_message="AUTH_URL: {url}",
    )
    TOKEN.write_text(creds.to_json())
    TOKEN.chmod(0o600)
    print(f"TOKEN_SAVED: {TOKEN}")


if __name__ == "__main__":
    main()
