import logging
from collections.abc import Iterable
from dataclasses import dataclass, field

from click import progressbar
from evernote.edam.type.ttypes import Note, Resource
from evernote_backup.evernote_client_util import require
from evernote_backup.log_util import log_format_note, log_format_notebook
from evernote_backup.note_storage import SqliteStorage

from evernote2unbent.client import UnbentClient, hex_md5, md5_of
from evernote2unbent.errors import Evernote2UnbentError

logger = logging.getLogger(__name__)


@dataclass
class UploadStats:
    """What the run did, for the line printed at the end."""

    created: int = 0
    skipped: int = 0
    failed: int = 0
    files: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)

    def record_failure(self, title: str, reason: str) -> None:
        self.failed += 1
        # Capped: a systematic problem fails every note, and thousands of identical lines
        # bury the one detail that would explain it.
        if len(self.failures) < 20:
            self.failures.append((title, reason))


class NoteUploader:
    """
    Sends synced notes to Unbent.

    A sibling of NoteExporter: it consumes the same ``iter_notes`` stream from the same
    local database, and differs only in where the notes end up. Nothing here touches sync
    or storage, so both stay upstream code that can be rebased.

    Notes go up as ENML. Unbent converts server-side, which keeps one implementation of
    what Evernote's markup means rather than a second one here that would drift from it.
    """

    def __init__(
        self,
        storage: SqliteStorage,
        client: UnbentClient,
        include_trash: bool,
        filter_notebooks: tuple[str, ...],
        filter_tags: tuple[str, ...],
    ) -> None:
        self.storage = storage
        self.client = client
        self.include_trash = include_trash
        self.filter_notebooks = filter_notebooks
        self.filter_tags = filter_tags
        self.stats = UploadStats()

    def upload_notebooks(self) -> UploadStats:
        count_notes = self.storage.notes.get_notes_count()
        count_trash = self.storage.notes.get_notes_count(is_active=False)

        if count_notes == 0 and count_trash == 0:
            raise Evernote2UnbentError(
                "That database has no notes."
                " Run `evernote-backup sync` to populate it first."
            )

        if count_notes > 0:
            logger.info("Uploading notebooks...")
            self._upload_active()

        if count_trash > 0 and self.include_trash:
            logger.info("Uploading trash...")
            self._upload_notes(None, self.storage.notes.iter_notes_trash())

        return self.stats

    def _upload_active(self) -> None:
        notebooks = list(self.storage.notebooks.iter_notebooks())

        if self.filter_notebooks:
            notebooks = [n for n in notebooks if n.name in self.filter_notebooks]
            for missing in set(self.filter_notebooks) - {n.name for n in notebooks}:
                logger.warning(f"Notebook '{missing}' not found in database.")

        with progressbar(notebooks, show_pos=True) as notebooks_bar:
            for notebook in notebooks_bar:
                if logger.getEffectiveLevel() == logging.DEBUG:  # pragma: no cover
                    logger.debug(f"Uploading notebook {log_format_notebook(notebook)}")

                guid = require(notebook.guid)
                if self.storage.notebooks.get_notebook_notes_count(guid) == 0:
                    logger.debug("Notebook is empty, skip")
                    continue

                self._upload_notes(
                    require(notebook.name), self.storage.notes.iter_notes(guid)
                )

    def _matches_tag_filter(self, note: Note) -> bool:
        if not note.tagNames:
            return False
        return bool(set(note.tagNames) & set(self.filter_tags))

    def _upload_notes(
        self, notebook_name: str | None, notes_source: Iterable[Note]
    ) -> None:
        if self.filter_tags:
            notes_source = filter(self._matches_tag_filter, notes_source)

        for note in notes_source:
            try:
                self._upload_one(note, notebook_name)
            # Broad on purpose: one malformed note must not end a run of thousands.
            except Exception as e:
                if logger.getEffectiveLevel() == logging.DEBUG:  # pragma: no cover
                    logger.exception(f"Failed {log_format_note(note)}")
                self.stats.record_failure(note.title or "Untitled", str(e))

    def _upload_one(self, note: Note, notebook_name: str | None) -> None:
        resources = list(note.resources or [])

        result = self.client.import_note(
            title=note.title or "Untitled",
            enml=note.content or "",
            # Deduplicated and order-stable: Evernote allows the same tag twice on a note,
            # and the server counts tags against a limit.
            tags=list(dict.fromkeys(note.tagNames or [])),
            notebook=notebook_name,
            # Evernote timestamps are already epoch milliseconds, which is what the API
            # expects; a missing one falls back to the other rather than to "now", so a
            # re-run does not keep moving the note's dates.
            created_at=note.created or note.updated or 0,
            updated_at=note.updated or note.created or 0,
            resources=[self._describe(r) for r in resources],
            source_url=(note.attributes.sourceURL if note.attributes else None),
        )

        note_id = result["id"]

        if not result.get("created"):
            # The note is already there, but a previous run may have died between creating
            # it and finishing its attachments. Fill only what is missing.
            if resources:
                existing = self.client.list_files(note_id)
                missing = [r for r in resources if self._filename(r) not in existing]
                self._upload_resources(note_id, missing)
            self.stats.skipped += 1
            return

        # Counted only once the attachments are up, so a note whose upload dies partway is
        # reported as failed rather than as both created and failed. The note itself stays
        # on the server, which is what makes the retry cheap: a re-run skips it and fills
        # in only the files that never arrived.
        self._upload_resources(note_id, resources)
        self.stats.created += 1

    def _upload_resources(self, note_id: str, resources: list[Resource]) -> None:
        for resource in resources:
            data = resource.data
            if not data or not data.body:
                continue
            self.client.upload_file(
                note_id,
                self._filename(resource),
                resource.mime or "application/octet-stream",
                data.body,
            )
            self.stats.files += 1

    def _describe(self, resource: Resource) -> dict[str, str]:
        """
        Metadata the server needs to resolve ``<en-media hash>`` into a link.

        The bytes stay out of this: they follow on their own request, and inlining them
        here as base64 would add a third to the size of every note upload.
        """
        return {
            "md5": self._md5(resource),
            "filename": self._filename(resource),
            "mime": resource.mime or "application/octet-stream",
        }

    def _md5(self, resource: Resource) -> str:
        """
        The hash ``<en-media>`` refers to this resource by.

        bodyHash when Evernote sent one, computed from the body otherwise. A resource with
        neither is not addressable at all: nothing can match it to the element referencing
        it, so its image would silently vanish from the note rather than fail loudly.
        """
        data = resource.data
        if data and data.bodyHash:
            return hex_md5(data.bodyHash)
        if data and data.body:
            return md5_of(data.body)
        raise ValueError("Attachment has neither a hash nor a body")

    def _filename(self, resource: Resource) -> str:
        """
        The attachment's name, synthesized when Evernote has none.

        Many resources carry no fileName at all — inline images especially — and we
        needs something stable to store them under. The md5 is the only identifier such a
        resource has, and using it keeps the name the same across re-runs, so a resumed
        upload recognizes what it already sent.
        """
        attributes = getattr(resource, "attributes", None)
        name = getattr(attributes, "fileName", None) if attributes else None
        if name:
            return str(name)

        extension = (resource.mime or "").rpartition("/")[2] or "bin"
        return f"{self._md5(resource)}.{extension}"
