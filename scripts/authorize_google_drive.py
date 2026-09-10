"""One-time local Google Drive OAuth authorization script for Alakazam.

WHY THIS EXISTS: the Noctrowl export pipeline (export/drive_client.py)
needs a real Google OAuth token to upload CSV files to Drive under
Alakazam's own export folder. Alakazam uses its OWN, separate Google Cloud
OAuth client — it does NOT reuse Project-Noctrowl's existing OAuth setup
(see CLAUDE.md's "Milestone 4 decisions, confirmed 2026-09-10"). This
script is adapted line-for-line from Noctrowl's own
``scripts/authorize_google_drive.py`` (same proven flow), pointed at
Alakazam's own env vars and token path so the two projects' credentials
never mix.

RUN THIS YOURSELF, ONCE, LOCALLY:
  - On your own machine, logged in to whichever Google account should own
    the Alakazam export folder — not on a server, not by an agent. It
    needs a real browser and a real human to click "Allow".
  - Safe to re-run any time you need to regenerate the token (lost,
    revoked, or you just want a fresh grant) — it always runs the
    interactive consent flow again and overwrites the existing token file.

BEFORE YOU CAN RUN THIS, you need real credentials that only you can
create (this script cannot do this part for you):
  1. Create a Google Cloud project (or reuse one you control) — this must
     be a project of your own; do not reuse Noctrowl's.
  2. In that project, enable the Google Drive API (APIs & Services >
     Library > Google Drive API > Enable). Drive API scope only — no
     Sheets API, matching this project's stated restraint.
  3. Create an OAuth 2.0 Client ID: APIs & Services > Credentials > Create
     Credentials > OAuth client ID > Application type: Desktop app.
  4. Download the resulting client-secret JSON file and save it at the
     path in the ALAKAZAM_GOOGLE_OAUTH_CLIENT_SECRET env var (see
     .env.example for the default path).

WHAT THIS SCRIPT DOES:
  1. Reads the OAuth "Desktop app" client-secret JSON from step 4 above.
  2. Opens your default browser to Google's sign-in/consent screen
     (InstalledAppFlow.run_local_server) requesting the Drive scope
     (https://www.googleapis.com/auth/drive) — log in and click Allow.
     Forces Google's consent screen every run (prompt="consent") so a
     refresh token is always issued, even on a re-run.
  3. Saves the resulting credentials (including a refresh token) as JSON
     to the ALAKAZAM_GOOGLE_OAUTH_TOKEN_PATH env var's path (see
     .env.example). export/drive_client.py reads from this same path
     afterward and refreshes the access token automatically when it
     expires — you should not need to re-run this script under normal use.

USAGE:
    python3 scripts/authorize_google_drive.py
"""
from __future__ import annotations

import os
import sys

# Allow this script to be run directly (e.g. `python3 scripts/authorize_google_drive.py`
# from the project root). When invoked that way, Python sets sys.path[0] to this
# file's own directory (scripts/), not the project root, so the top-level `export`
# package (which lives at the project root) wouldn't otherwise be importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SCOPES = ["https://www.googleapis.com/auth/drive"]

DEFAULT_CLIENT_SECRET_PATH = "./secrets/alakazam-google-oauth-client-secret.json"
DEFAULT_TOKEN_PATH = "./secrets/alakazam-google-oauth-token.json"


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass  # python-dotenv is a project dependency, but don't hard-fail if it's missing here

    client_secret_path = os.environ.get(
        "ALAKAZAM_GOOGLE_OAUTH_CLIENT_SECRET", DEFAULT_CLIENT_SECRET_PATH
    )
    token_path = os.environ.get("ALAKAZAM_GOOGLE_OAUTH_TOKEN_PATH", DEFAULT_TOKEN_PATH)

    if not os.path.isfile(client_secret_path):
        print(
            f"ERROR: OAuth client-secret file not found at {client_secret_path!r}.\n\n"
            "Get it from Google Cloud Console, using a project/OAuth client that is\n"
            "Alakazam's own — do NOT reuse Project-Noctrowl's existing OAuth client:\n"
            "  1. Create (or select) a Google Cloud project.\n"
            "  2. Enable the Google Drive API (Drive API scope only).\n"
            "  3. APIs & Services > Credentials > Create Credentials > OAuth client ID\n"
            "     > Application type: Desktop app > Create > Download JSON.\n\n"
            f"Save the downloaded file at {client_secret_path!r}, or set "
            "ALAKAZAM_GOOGLE_OAUTH_CLIENT_SECRET in your .env to point at wherever you "
            "saved it.",
            file=sys.stderr,
        )
        return 1

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from oauthlib.oauth2 import OAuth2Error
    except ImportError:
        print(
            "ERROR: google-auth-oauthlib is not installed. Run:\n"
            "  pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 1

    # export.drive_client is this same project's Drive layer — reused here
    # only for its atomic/restricted-permissions token-file writer
    # (save_oauth_token), so the initial save and the later refresh-save
    # (see export/drive_client.py::_load_oauth_credentials) go through the
    # exact same, already-tested write path rather than two versions that
    # could drift.
    from export.drive_client import save_oauth_token

    print("=" * 70)
    print("Alakazam Google Drive authorization")
    print("=" * 70)
    print()
    print(f"Client secret: {client_secret_path}")
    print(f"Token will be saved to: {token_path}")
    print()
    print("Your browser will now open. Sign in as the Google account that should own")
    print("the Alakazam export folder, and click Allow when asked to grant this app")
    print("access to your Google Drive.")
    print()

    flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, SCOPES)
    try:
        # prompt="consent" forces Google to issue a refresh token on every
        # run, not just the very first time this client ever got consent —
        # without it, a re-run to regenerate a lost token could silently
        # come back with no refresh token at all, which would contradict
        # this script's own "safe to re-run any time" claim above.
        credentials = flow.run_local_server(port=0, prompt="consent")
    except OAuth2Error as exc:
        print(
            f"ERROR: Google did not complete the authorization: {exc}\n\n"
            "This usually means access was denied or the consent screen was closed\n"
            "before finishing. Re-run this script and click Allow when prompted.",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - port conflicts, browser launch failure, timeout, etc.
        print(
            f"ERROR: The authorization flow did not complete: {exc}\n\n"
            "Re-run this script and complete the browser sign-in/consent steps promptly.",
            file=sys.stderr,
        )
        return 1

    # Atomic + created-already-restricted (0600) — see
    # export/drive_client.py::save_oauth_token for why a plain
    # open(path, "w") + chmod afterward isn't good enough for a file
    # holding a live refresh token.
    save_oauth_token(token_path, credentials.to_json())

    print()
    print("SUCCESS.")
    print()
    print("Granted scope:")
    for scope in SCOPES:
        print(f"  - {scope}")
    print()
    print(f"Token (including a refresh token) saved to: {token_path}")
    print()
    print("This file lets the export pipeline read AND write your Google Drive under")
    print("your own account's storage quota — it's already gitignored, but treat it")
    print("like a password (don't paste it into chat, don't commit it, don't share it).")
    print()
    print("Next step: make sure ALAKAZAM_GOOGLE_OAUTH_TOKEN_PATH is set in your .env to")
    print("the path above (it already matches the default if you didn't override it),")
    print("then scripts/run_export.py will use this token automatically — no further")
    print("action needed unless the token is later lost or revoked, in which case just")
    print("re-run this script.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
