"""Thin Google Drive API v3 wrapper for the Noctrowl export pipeline.

Alakazam gets its OWN, separate Google Cloud OAuth client/token — this
module never reuses Project-Noctrowl's existing OAuth setup
(``indogamingshop@gmail.com``) or its service account. See CLAUDE.md's
"Milestone 4 decisions, confirmed 2026-09-10".

Deliberately narrower than Noctrowl's own ``ingestion/drive_client.py``:
OAuth-only (no service-account fallback — Alakazam is a fresh build with
no legacy service-account history to carry, unlike Noctrowl's), and only
the two operations this milestone actually needs: find-or-create the
export root folder, and upload a CSV file into it. The OAuth
loading/refresh/token-file-writing logic below is adapted line-for-line
from Noctrowl's proven implementation (same shape, same atomicity/
permission guarantees on the token file) — see the module docstring there
for the full reasoning on each design choice.

Scope: ``https://www.googleapis.com/auth/drive`` (read+write) — Drive API
only, no Sheets API scope, matching Noctrowl's own restraint.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

OAUTH_SCOPES = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


class DriveCredentialError(Exception):
    """Raised when Alakazam's OAuth credentials/token are missing,
    unreadable, or can't be refreshed — never silently proceeds without
    real credentials, and never fabricates a folder/file ID.
    """


@dataclass
class DriveFile:
    id: str
    name: str
    mime_type: str


def save_oauth_token(token_path: str, content: str) -> None:
    """Writes an OAuth token file atomically and already-permission-
    restricted (0600) from the moment it's created — see Noctrowl's
    ``ingestion/drive_client.py::save_oauth_token`` for the full reasoning
    (create-then-chmod leaves a permission window; a direct write risks a
    truncated file on a crash mid-write). Identical implementation, kept
    independent per-project rather than shared, since these are two
    separate codebases with their own credential files.
    """
    token_dir = os.path.dirname(token_path) or "."
    os.makedirs(token_dir, exist_ok=True)
    tmp_path = token_path + ".tmp"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    os.replace(tmp_path, token_path)


def _load_oauth_credentials():
    """Loads Alakazam's own OAuth user credentials from
    ``ALAKAZAM_GOOGLE_OAUTH_TOKEN_PATH``, refreshing (and re-persisting) an
    expired access token automatically. Raises DriveCredentialError rather
    than silently proceeding without real credentials.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials as UserCredentials

    token_path = os.environ.get("ALAKAZAM_GOOGLE_OAUTH_TOKEN_PATH")
    if not token_path:
        raise DriveCredentialError(
            "ALAKAZAM_GOOGLE_OAUTH_TOKEN_PATH is not set. Run "
            "scripts/authorize_google_drive.py once to generate it, then set "
            "ALAKAZAM_GOOGLE_OAUTH_TOKEN_PATH in .env (see .env.example)."
        )
    if not os.path.isfile(token_path):
        raise DriveCredentialError(
            f"ALAKAZAM_GOOGLE_OAUTH_TOKEN_PATH points at {token_path!r}, which doesn't "
            "exist. Run scripts/authorize_google_drive.py once to generate it."
        )
    try:
        credentials = UserCredentials.from_authorized_user_file(token_path, scopes=OAUTH_SCOPES)
    except Exception as exc:  # malformed JSON, wrong shape, etc.
        raise DriveCredentialError(f"Could not load OAuth token from {token_path!r}: {exc}") from exc

    if credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except Exception as exc:
            raise DriveCredentialError(
                f"Google OAuth token at {token_path!r} could not be refreshed (it may have "
                f"been revoked): {exc}. Re-run scripts/authorize_google_drive.py to "
                "re-authorize."
            ) from exc
        try:
            save_oauth_token(token_path, credentials.to_json())
        except OSError:
            pass  # refreshed token still works for this run even if we couldn't persist it

    return credentials


class DriveClient:
    """Wraps the Drive API v3 for Alakazam's export pipeline. Auth is
    always OAuth (Alakazam's own credentials) — there is no
    service-account path here, unlike Noctrowl's client.
    """

    def __init__(self):
        from googleapiclient.discovery import build

        credentials = _load_oauth_credentials()
        self._service = build("drive", "v3", credentials=credentials, cache_discovery=False)

    def find_or_create_folder(self, name: str, parent_id: str | None = None) -> str:
        """Idempotent get-or-create by name. ``parent_id=None`` searches/
        creates directly under "My Drive" (Alakazam's export root folder is
        top-level — see docs/design/milestone-4-export-design.md); a real
        parent_id nests a folder under it (not currently needed by the
        runner, but kept general rather than hardcoding "top-level only").
        """
        escaped_name = name.replace("'", "\\'")
        parent_clause = f"'{parent_id}' in parents and " if parent_id else "'root' in parents and "
        query = (
            f"{parent_clause}trashed = false and mimeType = '{FOLDER_MIME_TYPE}' "
            f"and name = '{escaped_name}'"
        )
        response = self._service.files().list(q=query, fields="files(id, name)").execute()
        existing = response.get("files", [])
        if existing:
            return existing[0]["id"]

        body = {"name": name, "mimeType": FOLDER_MIME_TYPE}
        body["parents"] = [parent_id] if parent_id else ["root"]
        created = self._service.files().create(body=body, fields="id").execute()
        return created["id"]

    def upload_file(self, parent_id: str, name: str, content: bytes, mime_type: str) -> str:
        """Uploads ``content`` as a new file named ``name`` under
        ``parent_id``. Deliberately NOT idempotent-by-name (unlike
        Noctrowl's own upload_file) — each export run is its own distinct,
        timestamped file (see export/runner.py's naming), so two runs are
        never expected to collide on the same filename. Returns the new
        file's id.
        """
        import io

        from googleapiclient.http import MediaIoBaseUpload

        media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime_type, resumable=False)
        created = (
            self._service.files()
            .create(body={"name": name, "parents": [parent_id]}, media_body=media, fields="id")
            .execute()
        )
        return created["id"]
