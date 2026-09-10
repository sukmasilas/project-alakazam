"""The export orchestrator: query un-exported purchases, build the CSV,
upload it to Drive, and mark ``purchases.exported_at`` — but ONLY after a
confirmed successful upload.

Never commits/rolls back the given ``conn`` itself (same convention as
``inventory/purchases.py::save_purchase``) — the caller (a script, a test,
or eventually an in-app trigger) owns the transaction boundary. This
matters for the core invariant this module exists to protect: if the
Drive upload raises for any reason, this function re-raises without ever
touching ``exported_at`` for a single row — nothing gets marked exported
unless the caller's transaction actually commits a successful outcome,
and a retry (rerunning this function against the same un-exported rows)
naturally picks up exactly the same purchases again. No partial-credit
state: either every purchase queried for this run gets marked, or none do.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Protocol

from sqlalchemy import text
from sqlalchemy.engine import Connection

from export.csv_builder import build_export_csv

EXPORT_ROOT_FOLDER_NAME = "Alakazam Inventory Exports"


class SupportsDriveUpload(Protocol):
    """The minimal interface this module needs from a Drive client —
    lets tests substitute a stub/mock without depending on
    export.drive_client.DriveClient's real Google-API constructor.
    """

    def find_or_create_folder(self, name: str, parent_id: Optional[str] = None) -> str: ...

    def upload_file(self, parent_id: str, name: str, content: bytes, mime_type: str) -> str: ...


@dataclass
class ExportResult:
    ran_upload: bool  # False when there was nothing to export — no Drive call was made
    exported_purchase_ids: list[int] = field(default_factory=list)
    row_count: int = 0
    filename: Optional[str] = None
    drive_folder_id: Optional[str] = None
    drive_file_id: Optional[str] = None


def _get_unexported_purchase_ids(conn: Connection) -> list[int]:
    rows = conn.execute(
        text("SELECT id FROM purchases WHERE exported_at IS NULL ORDER BY id")
    ).all()
    return [r[0] for r in rows]


def _mark_exported(conn: Connection, purchase_ids: list[int], exported_at: datetime) -> None:
    conn.execute(
        text("UPDATE purchases SET exported_at = :exported_at WHERE id = ANY(:ids)"),
        {"exported_at": exported_at, "ids": purchase_ids},
    )


def run_export(
    conn: Connection,
    drive_client: SupportsDriveUpload,
    *,
    now: Optional[datetime] = None,
) -> ExportResult:
    """Runs one incremental export cycle.

    - Queries every purchase with ``exported_at IS NULL``.
    - If there are none, returns immediately with ``ran_upload=False`` —
      deliberately makes zero Drive API calls (no pointless empty-file
      upload, no folder-creation call) when there's genuinely nothing new.
    - Otherwise builds the CSV for exactly those purchases, uploads it to
      the (found-or-created) Alakazam export root folder, and — only if
      the upload call returns successfully — marks all of those purchases'
      ``exported_at`` to ``now`` in the given connection (not committed
      here; the caller commits).
    - Any exception raised by the Drive client (folder lookup/creation or
      upload) propagates unchanged, and no row is marked exported.
    """
    now = now or datetime.now(timezone.utc)

    purchase_ids = _get_unexported_purchase_ids(conn)
    if not purchase_ids:
        return ExportResult(ran_upload=False)

    csv_text, rows = build_export_csv(conn, purchase_ids=purchase_ids)
    filename = f"alakazam-export-{now.strftime('%Y%m%dT%H%M%SZ')}.csv"

    folder_id = drive_client.find_or_create_folder(EXPORT_ROOT_FOLDER_NAME)
    file_id = drive_client.upload_file(
        folder_id, filename, csv_text.encode("utf-8"), "text/csv"
    )

    # Only reached if the two Drive calls above didn't raise — the
    # "confirmed successful upload" this module's docstring promises.
    _mark_exported(conn, purchase_ids, now)

    return ExportResult(
        ran_upload=True,
        exported_purchase_ids=purchase_ids,
        row_count=len(rows),
        filename=filename,
        drive_folder_id=folder_id,
        drive_file_id=file_id,
    )
