import logging
import sys
from pathlib import Path

import click
from evernote_backup import config_defaults as defaults

from evernote2unbent.client import (
    UnbentClient,
    request_device_code,
    wait_for_token,
)
from evernote2unbent.config import config_path, normalize_url, read_url, write_url
from evernote2unbent.errors import Evernote2UnbentError, UnbentAuthError
from evernote2unbent.token_store import clear_token, read_token, write_token
from evernote2unbent.uploader import NoteUploader

logger = logging.getLogger(__name__)

DEFAULT_DATABASE = "en_backup.db"

opt_url = click.option(
    "--url",
    default=None,
    envvar="UNBENT_URL",
    help=(
        "Unbent instance to upload into. Asked for once and remembered;"
        " change it with `set-url` or by editing the config file."
    ),
)

opt_database = click.option(
    "--database",
    "-d",
    default=DEFAULT_DATABASE,
    show_default=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="The database `evernote-backup sync` wrote.",
)


def handle_errors(fn):
    """Report an actionable failure as a message, not a traceback."""

    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Evernote2UnbentError as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        except KeyboardInterrupt:
            # An interrupted upload is resumable, so this is not an error worth a stack
            # trace — the next run skips whatever already landed.
            click.echo("\nStopped. Re-run to pick up where this left off.", err=True)
            sys.exit(130)

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _resolve_url(url: str | None) -> str:
    """
    Which instance to talk to.

    Order: an explicit --url or $UNBENT_URL, then whatever this machine was pointed at
    before, then ask. There is deliberately no default — a guessed instance lets a
    forgotten flag send someone's notes to a server they did not choose, and that failure
    is silent, because uploading to the wrong Unbent still succeeds.
    """
    if url:
        return normalize_url(url)

    stored = read_url()
    if stored:
        return stored

    if not sys.stdin.isatty():
        raise Evernote2UnbentError(
            "No Unbent instance configured."
            " Run `evernote2unbent set-url https://unbent.app`, or pass --url."
        )

    click.echo("\nWhich Unbent instance? (for example https://unbent.app)")
    saved = write_url(click.prompt("  URL", type=str))
    click.echo(f"  Saved to {config_path()}\n")
    return saved


def _login(url: str) -> str:
    """
    Sign in without the terminal ever handling a credential.

    The device flow (RFC 8628): print a short code, let the user approve it in a browser
    they are already signed in to, then collect an ordinary session.
    """
    grant = request_device_code(url)
    target = grant.get("verification_uri_complete") or grant["verification_uri"]

    click.echo(f"\n  Open:  {target}")
    click.echo(f"  Code:  {grant['user_code']}\n")
    click.echo("  Waiting for approval...")

    token = wait_for_token(url, grant)
    write_token(url, token)
    click.echo("  Signed in.\n")
    return token


def _run_upload(
    database: Path,
    target: str,
    include_trash: bool,
    notebooks: tuple[str, ...],
    tags: tuple[str, ...],
) -> None:
    """The upload half, shared so `migrate` and `upload` cannot report it differently."""
    from evernote_backup.note_storage import SqliteStorage

    token = _ensure_token(target)

    uploader = NoteUploader(
        storage=SqliteStorage(database),
        client=UnbentClient(target, token),
        include_trash=include_trash,
        filter_notebooks=notebooks,
        filter_tags=tags,
    )

    try:
        stats = uploader.upload_notebooks()
    except UnbentAuthError:
        # The token was accepted at startup but refused mid-run, so it expired or was
        # revoked. Saying so beats a bare 401 at the end of a long upload.
        raise Evernote2UnbentError(
            "Unbent refused the session. Run `evernote2unbent login` and try again."
        ) from None

    click.echo(
        f"\n{stats.created} imported, {stats.skipped} already present, "
        f"{stats.files} files"
        + (f", {stats.failed} failed" if stats.failed else "")
        + "."
    )

    if stats.failures:
        click.echo("\nSome notes failed:")
        for title, reason in stats.failures:
            click.echo(f"  {title} — {reason}")
        if stats.failed > len(stats.failures):
            click.echo(f"  ...and {stats.failed - len(stats.failures)} more")
        click.echo("\nRe-run the same command to retry just these.")


def _ensure_token(url: str) -> str:
    token = read_token(url)
    if token:
        return token
    return _login(url)


@click.group()
@click.version_option()
def cli() -> None:
    """Move your Evernote notes into Unbent.

    \b
    Sync first with evernote-backup, then upload:
      evernote-backup init-db
      evernote-backup sync
      evernote2unbent upload
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")


@cli.command()
@opt_url
@handle_errors
def login(url: str | None) -> None:
    """Sign in to Unbent by approving a code in your browser."""
    _login(_resolve_url(url))


@cli.command()
@opt_url
@handle_errors
def logout(url: str | None) -> None:
    """Forget the stored session on this machine."""
    # Reads the stored instance rather than asking: signing out of one you never
    # configured is a no-op, and prompting for it would be theatre.
    target = url or read_url()
    if not target:
        click.echo("No Unbent instance configured; nothing to sign out of.")
        return
    clear_token(target)
    click.echo("Signed out locally. Revoke the session in Unbent to be certain.")


@cli.command("set-url")
@click.argument("url", required=True)
@handle_errors
def set_url(url: str) -> None:
    """Point this machine at an Unbent instance."""
    click.echo(f"Instance set to {write_url(url)}")
    click.echo(str(config_path()))


@cli.command("show-config")
@handle_errors
def show_config() -> None:
    """Show which instance this machine is pointed at."""
    click.echo(str(config_path()))
    stored = read_url()
    click.echo(f"  url: {stored}" if stored else "  (no instance set yet)")


@cli.command()
@click.option(
    "--database",
    "-d",
    default=DEFAULT_DATABASE,
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Where to keep the local sync database.",
)
@opt_url
@click.option(
    "--include-trash", is_flag=True, help="Include notes from Evernote's trash."
)
@click.option(
    "--notebook",
    "-n",
    "notebooks",
    multiple=True,
    help="Only upload this notebook. Repeatable. The sync still fetches everything.",
)
@click.option(
    "--resync",
    is_flag=True,
    help="Start the local database over. Re-downloads every note.",
)
@handle_errors
def migrate(
    database: Path,
    url: str | None,
    include_trash: bool,
    notebooks: tuple[str, ...],
    resync: bool,
) -> None:
    """Sync from Evernote and upload to Unbent, in one command.

    \b
    Equivalent to running, by hand:
      evernote-backup init-db
      evernote-backup sync
      evernote2unbent upload

    Safe to re-run. The sync is incremental and the upload skips notes that
    already landed, so an interrupted migration continues where it stopped.
    """
    from evernote_backup import cli_app as backup

    # Resolved before anything slow happens: being asked which instance to use after a
    # forty-minute sync, rather than before it, would be its own small betrayal.
    target = _resolve_url(url)

    if resync or not database.exists():
        click.echo("Signing in to Evernote...")
        backup.init_db(
            database=database,
            auth_user=None,
            auth_password=None,
            auth_oauth_port=defaults.OAUTH_LOCAL_PORT,
            auth_oauth_host=defaults.OAUTH_HOST,
            auth_token=None,
            force=resync,
            backend=defaults.BACKEND,
            network_retry_count=defaults.NETWORK_ERROR_RETRY_COUNT,
            use_system_ssl_ca=False,
            custom_api_data=None,
        )
    else:
        click.echo(f"Using the existing database at {database}.")

    click.echo("\nSyncing from Evernote. This is the slow part; it is resumable.")
    backup.sync(
        database=database,
        max_chunk_results=defaults.SYNC_CHUNK_MAX_RESULTS,
        max_download_workers=defaults.SYNC_MAX_DOWNLOAD_WORKERS,
        download_cache_memory_limit=defaults.SYNC_DOWNLOAD_CACHE_MEMORY_LIMIT,
        network_retry_count=defaults.NETWORK_ERROR_RETRY_COUNT,
        use_system_ssl_ca=False,
        token=None,
    )

    click.echo("\nUploading to Unbent.")
    _run_upload(
        database=database,
        target=target,
        include_trash=include_trash,
        notebooks=notebooks,
        tags=(),
    )


@cli.command()
@opt_database
@opt_url
@click.option(
    "--include-trash", is_flag=True, help="Include notes from Evernote's trash."
)
@click.option(
    "--notebook",
    "-n",
    "notebooks",
    multiple=True,
    help="Only this notebook. Repeatable. Start with one to check the result.",
)
@click.option(
    "--tag", "-t", "tags", multiple=True, help="Only notes with this tag. Repeatable."
)
@handle_errors
def upload(
    database: Path,
    url: str | None,
    include_trash: bool,
    notebooks: tuple[str, ...],
    tags: tuple[str, ...],
) -> None:
    """Upload already-synced notes into Unbent.

    Use this when `evernote-backup sync` has already run. `migrate` does the
    sync and this in one command.

    Re-running is safe: notes that already landed are skipped, so an
    interrupted run is resumed by repeating the same command.
    """
    _run_upload(
        database=database,
        target=_resolve_url(url),
        include_trash=include_trash,
        notebooks=notebooks,
        tags=tags,
    )


def main() -> None:
    cli()
