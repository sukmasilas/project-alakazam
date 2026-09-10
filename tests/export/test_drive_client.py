"""Tests for export.drive_client. Deliberately mocked, not live — the test
suite must run without real Google OAuth credentials/network access (no
real Alakazam Drive client/token exists yet — see the milestone report for
what setup steps remain). Adapted from Project-Noctrowl's own
tests/ingestion/test_drive_client.py OAuth test shape, pointed at
Alakazam's own env var (ALAKAZAM_GOOGLE_OAUTH_TOKEN_PATH) and the narrower
OAuth-only DriveClient (no service-account path here).
"""
from __future__ import annotations

import json
import os
import stat
from unittest import mock

import pytest

from export.drive_client import DriveCredentialError, save_oauth_token


# ---------------------------------------------------------------------------
# Token file writing (save_oauth_token) — atomicity + restricted permissions
# ---------------------------------------------------------------------------


def test_save_oauth_token_writes_content_and_restricts_permissions(tmp_path):
    token_path = tmp_path / "nested" / "alakazam-google-oauth-token.json"
    save_oauth_token(str(token_path), '{"token": "abc"}')

    assert token_path.read_text() == '{"token": "abc"}'
    mode = stat.S_IMODE(os.stat(token_path).st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"
    assert not (tmp_path / "nested" / "alakazam-google-oauth-token.json.tmp").exists()


def test_save_oauth_token_overwrites_existing_file_atomically(tmp_path):
    token_path = tmp_path / "alakazam-google-oauth-token.json"
    save_oauth_token(str(token_path), '{"token": "old"}')
    save_oauth_token(str(token_path), '{"token": "new"}')

    assert token_path.read_text() == '{"token": "new"}'
    assert not (tmp_path / "alakazam-google-oauth-token.json.tmp").exists()


def test_save_oauth_token_cleans_up_tmp_file_on_write_failure(tmp_path, monkeypatch):
    token_path = tmp_path / "alakazam-google-oauth-token.json"

    real_fdopen = os.fdopen

    def _boom_fdopen(fd, mode="r", *args, **kwargs):
        f = real_fdopen(fd, mode, *args, **kwargs)
        f.close()
        raise RuntimeError("simulated crash mid-write")

    monkeypatch.setattr(os, "fdopen", _boom_fdopen)

    with pytest.raises(RuntimeError, match="simulated crash mid-write"):
        save_oauth_token(str(token_path), '{"token": "abc"}')

    assert not token_path.exists()
    assert not (tmp_path / "alakazam-google-oauth-token.json.tmp").exists()


# ---------------------------------------------------------------------------
# OAuth credential loading (_load_oauth_credentials)
# ---------------------------------------------------------------------------


def _fake_token_json(*, expiry: str | None = None) -> str:
    info = {
        "token": "fake-access-token",
        "refresh_token": "fake-refresh-token",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "fake-alakazam-client-id.apps.googleusercontent.com",
        "client_secret": "fake-client-secret",
        "scopes": ["https://www.googleapis.com/auth/drive"],
    }
    if expiry is not None:
        info["expiry"] = expiry
    return json.dumps(info)


def test_missing_token_path_env_raises_clear_error(monkeypatch):
    from export.drive_client import _load_oauth_credentials

    monkeypatch.delenv("ALAKAZAM_GOOGLE_OAUTH_TOKEN_PATH", raising=False)
    with pytest.raises(DriveCredentialError, match="not set"):
        _load_oauth_credentials()


def test_nonexistent_token_file_raises_clear_error(monkeypatch, tmp_path):
    from export.drive_client import _load_oauth_credentials

    missing_path = tmp_path / "does-not-exist.json"
    monkeypatch.setenv("ALAKAZAM_GOOGLE_OAUTH_TOKEN_PATH", str(missing_path))
    with pytest.raises(DriveCredentialError, match="doesn't exist"):
        _load_oauth_credentials()


def test_malformed_token_file_raises_clear_error(monkeypatch, tmp_path):
    from export.drive_client import _load_oauth_credentials

    token_path = tmp_path / "alakazam-google-oauth-token.json"
    token_path.write_text("this is not valid json at all {")
    monkeypatch.setenv("ALAKAZAM_GOOGLE_OAUTH_TOKEN_PATH", str(token_path))
    with pytest.raises(DriveCredentialError, match="Could not load OAuth token"):
        _load_oauth_credentials()


def test_unexpired_token_loads_without_refreshing(monkeypatch, tmp_path):
    from export.drive_client import _load_oauth_credentials

    token_path = tmp_path / "alakazam-google-oauth-token.json"
    far_future = "2099-01-01T00:00:00Z"
    token_path.write_text(_fake_token_json(expiry=far_future))
    monkeypatch.setenv("ALAKAZAM_GOOGLE_OAUTH_TOKEN_PATH", str(token_path))

    with mock.patch("google.oauth2.credentials.Credentials.refresh") as refresh_mock:
        credentials = _load_oauth_credentials()

    refresh_mock.assert_not_called()
    assert credentials.token == "fake-access-token"
    assert json.loads(token_path.read_text())["token"] == "fake-access-token"


def test_expired_token_refreshes_and_persists_new_token(monkeypatch, tmp_path):
    from export.drive_client import _load_oauth_credentials

    token_path = tmp_path / "alakazam-google-oauth-token.json"
    # No "expiry" key => from_authorized_user_info treats it as already
    # expired, exercising the refresh path without a fabricated timestamp.
    token_path.write_text(_fake_token_json())
    monkeypatch.setenv("ALAKAZAM_GOOGLE_OAUTH_TOKEN_PATH", str(token_path))

    def fake_refresh(self, request):
        self.token = "refreshed-access-token"

    with mock.patch("google.oauth2.credentials.Credentials.refresh", new=fake_refresh):
        credentials = _load_oauth_credentials()

    assert credentials.token == "refreshed-access-token"
    saved = json.loads(token_path.read_text())
    assert saved["token"] == "refreshed-access-token"
    assert saved["refresh_token"] == "fake-refresh-token"


def test_refresh_failure_raises_clear_error(monkeypatch, tmp_path):
    from export.drive_client import _load_oauth_credentials

    token_path = tmp_path / "alakazam-google-oauth-token.json"
    token_path.write_text(_fake_token_json())
    monkeypatch.setenv("ALAKAZAM_GOOGLE_OAUTH_TOKEN_PATH", str(token_path))

    def failing_refresh(self, request):
        raise RuntimeError("invalid_grant: token has been revoked")

    with mock.patch("google.oauth2.credentials.Credentials.refresh", new=failing_refresh):
        with pytest.raises(DriveCredentialError, match="could not be refreshed"):
            _load_oauth_credentials()


# ---------------------------------------------------------------------------
# DriveClient construction + find_or_create_folder / upload_file logic
# ---------------------------------------------------------------------------


def test_driveclient_construction_uses_oauth_credentials(monkeypatch):
    from export.drive_client import DriveClient

    monkeypatch.setenv("ALAKAZAM_GOOGLE_OAUTH_TOKEN_PATH", "/fake/path/token.json")
    with mock.patch("export.drive_client._load_oauth_credentials", return_value=mock.Mock()) as oauth_mock:
        with mock.patch("googleapiclient.discovery.build") as build_mock:
            DriveClient()

    oauth_mock.assert_called_once()
    build_mock.assert_called_once()
    _, kwargs = build_mock.call_args
    assert kwargs["credentials"] is oauth_mock.return_value


def test_driveclient_missing_credentials_raises_before_any_api_call(monkeypatch):
    from export.drive_client import DriveClient

    monkeypatch.delenv("ALAKAZAM_GOOGLE_OAUTH_TOKEN_PATH", raising=False)
    with pytest.raises(DriveCredentialError, match="not set"):
        DriveClient()


def _client_with_mocked_service(monkeypatch):
    from export.drive_client import DriveClient

    monkeypatch.setenv("ALAKAZAM_GOOGLE_OAUTH_TOKEN_PATH", "/fake/path/token.json")
    with mock.patch("export.drive_client._load_oauth_credentials", return_value=mock.Mock()):
        with mock.patch("googleapiclient.discovery.build") as build_mock:
            service = mock.Mock()
            build_mock.return_value = service
            client = DriveClient()
    return client, service


def test_find_or_create_folder_returns_existing_id_without_creating(monkeypatch):
    client, service = _client_with_mocked_service(monkeypatch)
    service.files.return_value.list.return_value.execute.return_value = {
        "files": [{"id": "existing-folder-id", "name": "Alakazam Inventory Exports"}]
    }

    folder_id = client.find_or_create_folder("Alakazam Inventory Exports")

    assert folder_id == "existing-folder-id"
    service.files.return_value.create.assert_not_called()
    list_kwargs = service.files.return_value.list.call_args.kwargs
    assert "'root' in parents" in list_kwargs["q"]
    assert "Alakazam Inventory Exports" in list_kwargs["q"]


def test_find_or_create_folder_creates_when_not_found(monkeypatch):
    client, service = _client_with_mocked_service(monkeypatch)
    service.files.return_value.list.return_value.execute.return_value = {"files": []}
    service.files.return_value.create.return_value.execute.return_value = {"id": "new-folder-id"}

    folder_id = client.find_or_create_folder("Alakazam Inventory Exports")

    assert folder_id == "new-folder-id"
    create_kwargs = service.files.return_value.create.call_args.kwargs
    assert create_kwargs["body"]["name"] == "Alakazam Inventory Exports"
    assert create_kwargs["body"]["mimeType"] == "application/vnd.google-apps.folder"
    assert create_kwargs["body"]["parents"] == ["root"]


def test_find_or_create_folder_with_explicit_parent(monkeypatch):
    client, service = _client_with_mocked_service(monkeypatch)
    service.files.return_value.list.return_value.execute.return_value = {"files": []}
    service.files.return_value.create.return_value.execute.return_value = {"id": "child-folder-id"}

    folder_id = client.find_or_create_folder("Subfolder", parent_id="parent-id-123")

    assert folder_id == "child-folder-id"
    list_kwargs = service.files.return_value.list.call_args.kwargs
    assert "'parent-id-123' in parents" in list_kwargs["q"]
    create_kwargs = service.files.return_value.create.call_args.kwargs
    assert create_kwargs["body"]["parents"] == ["parent-id-123"]


def test_upload_file_creates_new_file_and_returns_id(monkeypatch):
    client, service = _client_with_mocked_service(monkeypatch)
    service.files.return_value.create.return_value.execute.return_value = {"id": "uploaded-file-id"}

    with mock.patch("googleapiclient.http.MediaIoBaseUpload") as media_mock:
        file_id = client.upload_file("folder-id", "export.csv", b"purchase_ref,...\n", "text/csv")

    assert file_id == "uploaded-file-id"
    create_kwargs = service.files.return_value.create.call_args.kwargs
    assert create_kwargs["body"] == {"name": "export.csv", "parents": ["folder-id"]}
    media_mock.assert_called_once()
    _, media_call_kwargs = media_mock.call_args
    assert media_call_kwargs["mimetype"] == "text/csv"
