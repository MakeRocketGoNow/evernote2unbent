import hashlib
import json

import pytest
from evernote.edam.type.ttypes import (
    Data,
    Note,
    Notebook,
    Resource,
    ResourceAttributes,
)

from evernote2unbent.client import UnbentClient, hex_md5
from evernote2unbent.token_store import (
    clear_token,
    read_token,
    write_token,
)
from evernote2unbent.uploader import NoteUploader

UNBENT_URL = "http://unbent.test"

PNG = b"\x89PNG\r\n\x1a\n" + b"fake"
PNG_MD5 = hashlib.md5(PNG).hexdigest()


def make_note(guid="note-1", title="Test note", resources=None, tags=None):
    return Note(
        guid=guid,
        title=title,
        notebookGuid="nb-1",
        active=True,
        content="<en-note><div>Body</div></en-note>",
        created=1756600000000,
        updated=1756600001000,
        tagNames=tags if tags is not None else ["one"],
        resources=resources,
    )


def make_resource(with_hash=True, filename="pixel.png"):
    return Resource(
        guid="res-1",
        noteGuid="note-1",
        mime="image/png",
        data=Data(
            body=PNG,
            size=len(PNG),
            bodyHash=hashlib.md5(PNG).digest() if with_hash else None,
        ),
        attributes=ResourceAttributes(fileName=filename),
    )


class FakeStorage:
    """Just enough of SqliteStorage for the uploader to walk one notebook."""

    def __init__(self, notes, trash=None):
        self._notes = notes
        self._trash = trash or []
        outer = self

        class Notes:
            def get_notes_count(self, is_active=True):
                return len(outer._notes) if is_active else len(outer._trash)

            def iter_notes(self, notebook_guid):
                return iter(outer._notes)

            def iter_notes_trash(self):
                return iter(outer._trash)

        class Notebooks:
            def iter_notebooks(self):
                return iter([Notebook(guid="nb-1", name="Notebook One")])

            def get_notebook_notes_count(self, guid):
                return len(outer._notes)

        self.notes = Notes()
        self.notebooks = Notebooks()


def upload(storage, requests_mock, **kwargs):
    client = UnbentClient(UNBENT_URL, "tok")
    uploader = NoteUploader(
        storage=storage,
        client=client,
        include_trash=kwargs.pop("include_trash", False),
        filter_notebooks=kwargs.pop("filter_notebooks", ()),
        filter_tags=kwargs.pop("filter_tags", ()),
    )
    return uploader.upload_notebooks()


def test_hex_md5_converts_binary_hash():
    """
    ENML spells the hash in lowercase hex; Thrift carries it as raw bytes.

    Getting this wrong does not fail loudly — the server simply matches no attachment to
    the <en-media> element, and every image silently disappears from the imported note.
    """
    assert hex_md5(hashlib.md5(PNG).digest()) == PNG_MD5


def test_hex_md5_passes_through_a_string():
    assert hex_md5(PNG_MD5.upper()) == PNG_MD5


def test_uploads_a_note_as_enml(requests_mock):
    requests_mock.post(
        f"{UNBENT_URL}/api/import/enml",
        json={"id": "n1", "created": True, "usedMd5": []},
    )

    stats = upload(FakeStorage([make_note()]), requests_mock)

    assert stats.created == 1
    assert stats.skipped == 0
    body = requests_mock.request_history[0].json()
    # ENML, not Markdown: conversion is the server's job so it happens in one place.
    assert body["enml"] == "<en-note><div>Body</div></en-note>"
    assert body["notebook"] == "Notebook One"


def test_deduplicates_tags(requests_mock):
    requests_mock.post(
        f"{UNBENT_URL}/api/import/enml",
        json={"id": "n1", "created": True, "usedMd5": []},
    )

    upload(FakeStorage([make_note(tags=["a", "b", "a"])]), requests_mock)

    assert requests_mock.request_history[0].json()["tags"] == ["a", "b"]


def test_sends_resource_metadata_but_not_bytes(requests_mock):
    """Bytes go on their own request; inlining them would bloat every note upload."""
    requests_mock.post(
        f"{UNBENT_URL}/api/import/enml",
        json={"id": "n1", "created": True, "usedMd5": [PNG_MD5]},
    )
    requests_mock.put(f"{UNBENT_URL}/api/notes/n1/files", json={"filename": "pixel.png"})

    stats = upload(FakeStorage([make_note(resources=[make_resource()])]), requests_mock)

    payload = requests_mock.request_history[0].json()
    assert payload["resources"] == [
        {"md5": PNG_MD5, "filename": "pixel.png", "mime": "image/png"}
    ]
    assert "body" not in json.dumps(payload["resources"])
    assert stats.files == 1


def test_computes_md5_when_evernote_sent_no_hash(requests_mock):
    requests_mock.post(
        f"{UNBENT_URL}/api/import/enml",
        json={"id": "n1", "created": True, "usedMd5": []},
    )
    requests_mock.put(f"{UNBENT_URL}/api/notes/n1/files", json={"filename": "pixel.png"})

    upload(
        FakeStorage([make_note(resources=[make_resource(with_hash=False)])]),
        requests_mock,
    )

    assert requests_mock.request_history[0].json()["resources"][0]["md5"] == PNG_MD5


def test_names_an_unnamed_attachment_after_its_hash(requests_mock):
    """
    A stable name matters for resuming: the next run compares against what is stored, so
    a name that changed between runs would re-upload the same file forever.
    """
    requests_mock.post(
        f"{UNBENT_URL}/api/import/enml",
        json={"id": "n1", "created": True, "usedMd5": []},
    )
    requests_mock.put(
        f"{UNBENT_URL}/api/notes/n1/files", json={"filename": f"{PNG_MD5}.png"}
    )

    upload(
        FakeStorage([make_note(resources=[make_resource(filename=None)])]),
        requests_mock,
    )

    assert (
        requests_mock.request_history[0].json()["resources"][0]["filename"]
        == f"{PNG_MD5}.png"
    )


def test_skips_a_note_that_already_landed(requests_mock):
    requests_mock.post(
        f"{UNBENT_URL}/api/import/enml",
        json={"id": "n1", "created": False, "usedMd5": []},
    )

    stats = upload(FakeStorage([make_note()]), requests_mock)

    assert stats.created == 0
    assert stats.skipped == 1


def test_backfills_attachments_a_previous_run_never_finished(requests_mock):
    """
    The note is already there, so it is skipped — but its files may not be. Without this
    a run that died mid-upload would leave a permanent hole no retry could fill.
    """
    requests_mock.post(
        f"{UNBENT_URL}/api/import/enml",
        json={"id": "n1", "created": False, "usedMd5": []},
    )
    requests_mock.get(f"{UNBENT_URL}/api/notes/n1/files", json={"files": []})
    requests_mock.put(f"{UNBENT_URL}/api/notes/n1/files", json={"filename": "pixel.png"})

    stats = upload(FakeStorage([make_note(resources=[make_resource()])]), requests_mock)

    assert stats.skipped == 1
    assert stats.files == 1


def test_does_not_reupload_attachments_that_are_already_there(requests_mock):
    requests_mock.post(
        f"{UNBENT_URL}/api/import/enml",
        json={"id": "n1", "created": False, "usedMd5": []},
    )
    requests_mock.get(
        f"{UNBENT_URL}/api/notes/n1/files", json={"files": [{"filename": "pixel.png"}]}
    )

    stats = upload(FakeStorage([make_note(resources=[make_resource()])]), requests_mock)

    assert stats.files == 0


def test_a_failed_attachment_does_not_count_as_created(requests_mock):
    """
    Counting on creation rather than completion reported one note as both created and
    failed, so the totals added up to more notes than the database held.
    """
    requests_mock.post(
        f"{UNBENT_URL}/api/import/enml",
        json={"id": "n1", "created": True, "usedMd5": []},
    )
    requests_mock.put(f"{UNBENT_URL}/api/notes/n1/files", status_code=500)

    stats = upload(FakeStorage([make_note(resources=[make_resource()])]), requests_mock)

    assert stats.created == 0
    assert stats.failed == 1


def test_one_bad_note_does_not_end_the_run(requests_mock):
    requests_mock.post(
        f"{UNBENT_URL}/api/import/enml",
        [
            {"status_code": 500},
            {"json": {"id": "n2", "created": True, "usedMd5": []}},
        ],
    )

    stats = upload(
        FakeStorage([make_note(guid="a"), make_note(guid="b", title="Second")]),
        requests_mock,
    )

    assert stats.failed == 1
    assert stats.created == 1


def test_uploads_attachments_with_put_not_post(requests_mock):
    """The attachment route is a PUT; a POST matches no route and 404s misleadingly."""
    requests_mock.post(
        f"{UNBENT_URL}/api/import/enml",
        json={"id": "n1", "created": True, "usedMd5": []},
    )
    put = requests_mock.put(
        f"{UNBENT_URL}/api/notes/n1/files", json={"filename": "pixel.png"}
    )

    upload(FakeStorage([make_note(resources=[make_resource()])]), requests_mock)

    assert put.called
    assert put.last_request.method == "PUT"


class TestTokenStore:
    @pytest.fixture(autouse=True)
    def _config_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    def test_round_trips(self):
        write_token(UNBENT_URL, "tok_abc")
        assert read_token(UNBENT_URL) == "tok_abc"

    def test_missing_reads_as_none(self):
        assert read_token(UNBENT_URL) is None

    def test_written_private(self, tmp_path):
        write_token(UNBENT_URL, "tok_abc")
        path = tmp_path / "unbent" / "credentials.json"
        assert path.stat().st_mode & 0o777 == 0o600

    def test_tightens_permissions_on_an_existing_file(self, tmp_path):
        path = tmp_path / "unbent" / "credentials.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"tokens":{}}')
        path.chmod(0o644)

        write_token(UNBENT_URL, "tok_abc")

        assert path.stat().st_mode & 0o777 == 0o600

    def test_keeps_instances_apart(self):
        write_token("https://a.test", "tok_a")
        write_token("https://b.test", "tok_b")

        assert read_token("https://a.test") == "tok_a"
        assert read_token("https://b.test") == "tok_b"

    def test_ignores_a_trailing_slash(self):
        write_token(UNBENT_URL, "tok_abc")
        assert read_token(f"{UNBENT_URL}/") == "tok_abc"

    def test_survives_a_corrupt_file(self, tmp_path):
        path = tmp_path / "unbent" / "credentials.json"
        path.parent.mkdir(parents=True)
        path.write_text("not json")

        assert read_token(UNBENT_URL) is None
        write_token(UNBENT_URL, "tok_abc")
        assert read_token(UNBENT_URL) == "tok_abc"

    def test_clears_one_instance_only(self):
        write_token("https://a.test", "tok_a")
        write_token("https://b.test", "tok_b")

        clear_token("https://a.test")

        assert read_token("https://a.test") is None
        assert read_token("https://b.test") == "tok_b"

    def test_shares_the_file_with_the_typescript_cli(self, tmp_path):
        """Same path and shape, so signing in with either tool satisfies both."""
        write_token(UNBENT_URL, "tok_abc")
        data = json.loads((tmp_path / "unbent" / "credentials.json").read_text())
        assert data == {"tokens": {UNBENT_URL: "tok_abc"}}
