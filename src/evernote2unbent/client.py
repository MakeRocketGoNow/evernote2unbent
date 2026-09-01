import binascii
import hashlib
import logging
import time
from pathlib import Path
from typing import Any

import requests

from evernote2unbent.errors import UnbentAuthError

logger = logging.getLogger(__name__)

CLIENT_ID = "evernote-backup"

#: Where the session lives between runs, so a resumed upload need not sign in again.
CREDENTIALS_PATH = Path.home() / ".config" / "unbent" / "credentials.json"


def hex_md5(body_hash: bytes | str) -> str:
    """
    Evernote's ``Data.bodyHash`` as ENML's ``<en-media hash>`` spells it.

    The Thrift field is the MD5 in *binary*, while the hash attribute inside the note is
    lowercase hex. Converting is what lets the server match an attachment to the element
    that references it, so getting this wrong silently strips images from every note.
    """
    if isinstance(body_hash, str):
        return body_hash.lower()
    return binascii.hexlify(body_hash).decode()


def md5_of(data: bytes) -> str:
    """Fallback for a resource whose bodyHash the server did not populate."""
    return hashlib.md5(data).hexdigest()


class UnbentClient:
    """
    Uploads notes to Unbent.

    Notes go up as ENML rather than Markdown: Unbent converts server-side so that the
    definition of what Evernote's markup means lives in one place, instead of being
    reimplemented here and drifting from the original.

    Attachments follow as raw bytes on a separate request, because they dwarf the note and
    have no business inflating a JSON body by a third as base64.
    """

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        response = self.session.request(
            method, f"{self.base_url}{path}", timeout=120, **kwargs
        )
        if response.status_code == 401:
            raise UnbentAuthError(
                "Unbent rejected the session. Run 'evernote-backup unbent-login' again."
            )
        response.raise_for_status()
        return response

    def import_note(
        self,
        title: str,
        enml: str,
        tags: list[str],
        notebook: str | None,
        created_at: int,
        updated_at: int,
        resources: list[dict[str, str]],
        source_url: str | None = None,
    ) -> dict[str, Any]:
        """
        Create one note, or report that it already exists.

        One request per note rather than a batch: an import of thousands will be
        interrupted, and the server keys on a hash of title and body, so a resumed run
        skips what already landed with no progress file to keep in sync.
        """
        payload: dict[str, Any] = {
            "title": title,
            "enml": enml,
            "tags": tags,
            "createdAt": created_at,
            "updatedAt": updated_at,
            "resources": resources,
            "sourceEnex": CLIENT_ID,
        }
        if notebook:
            payload["notebook"] = notebook
        if source_url:
            payload["sourceUrl"] = source_url

        return self._request("POST", "/api/import/enml", json=payload).json()

    def upload_file(
        self, note_id: str, filename: str, mime: str, data: bytes
    ) -> dict[str, Any]:
        """Attach one file. The server may rename it to avoid a collision."""
        # PUT, not POST: the attachment route is a PUT, and a POST to that path matches
        # no route at all — which surfaces as a 404 that reads like a missing note.
        return self._request(
            "PUT",
            f"/api/notes/{note_id}/files",
            headers={
                "X-Filename": requests.utils.quote(filename),
                "Content-Type": mime or "application/octet-stream",
            },
            data=data,
        ).json()

    def list_files(self, note_id: str) -> set[str]:
        """
        Filenames already stored for a note.

        Needed when a previous run created the note but died before its attachments
        finished: the note is then skipped as already-imported, and without this its
        missing files would never be retried.
        """
        response = self._request("GET", f"/api/notes/{note_id}/files")
        return {f["filename"] for f in response.json().get("files", [])}


def request_device_code(base_url: str) -> dict[str, Any]:
    """Start a device authorization, returning the code the user approves in a browser."""
    response = requests.post(
        f"{base_url.rstrip('/')}/api/auth/device/code",
        # JSON, not the form encoding RFC 8628 specifies: the server rejects
        # application/x-www-form-urlencoded with a 415.
        json={"client_id": CLIENT_ID},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def wait_for_token(base_url: str, grant: dict[str, Any]) -> str:
    """
    Poll until the user approves or refuses.

    ``authorization_pending`` is the ordinary case and simply means keep waiting.
    ``slow_down`` is the server asking for a longer gap, and per RFC 8628 the increase has
    to persist rather than applying to a single sleep.
    """
    deadline = time.monotonic() + grant.get("expires_in", 1800)
    interval = max(1, grant.get("interval", 5))

    while True:
        if time.monotonic() > deadline:
            raise UnbentAuthError(
                "That code expired before it was approved. Run the command again."
            )

        time.sleep(interval)

        response = requests.post(
            f"{base_url.rstrip('/')}/api/auth/device/token",
            json={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": grant["device_code"],
                "client_id": CLIENT_ID,
            },
            timeout=30,
        )
        body = response.json()

        if "access_token" in body:
            return str(body["access_token"])

        error = body.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error == "access_denied":
            raise UnbentAuthError(
                "That request was denied in the browser. Nothing was granted."
            )
        if error == "expired_token":
            raise UnbentAuthError(
                "That code expired before it was approved. Run the command again."
            )
        raise UnbentAuthError(body.get("error_description") or str(error))
