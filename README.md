# evernote2unbent

Move your Evernote notes into [Unbent](https://unbent.app) — notes, notebooks, tags and
attachments — without exporting an ENEX file first.

Evernote has no export API, so every other route runs through the desktop client's
File → Export, which is manual, unreliable past a few thousand notes, and produces
multi-gigabyte files you then have to feed somewhere. This syncs your account with
[evernote-backup](https://github.com/vzhd1701/evernote-backup) and uploads straight from
its local database.

## Install

```bash
uv tool install evernote2unbent
```

Or with pipx:

```bash
pipx install evernote2unbent
```

## Use

Sync your Evernote account first. This writes `en_backup.db` in the current directory and
is resumable — an interrupted sync continues rather than restarting:

```bash
evernote-backup init-db
evernote-backup sync
```

Then upload. The first command asks which Unbent instance to use and remembers it:

```bash
evernote2unbent login
evernote2unbent upload
```

**Start with one notebook.** Check the result in Unbent before moving everything:

```bash
evernote2unbent upload -n "Some Notebook"
```

## Re-running is safe

Unbent keys each note on a hash of its title and body, so a note that already landed is
skipped rather than duplicated. That makes an interrupted upload resumable by simply
repeating the command — and a note whose attachments failed partway gets only the missing
files on the next run.

Nothing here deletes or modifies anything in Evernote.

## Options

| Flag | Meaning |
| --- | --- |
| `-d, --database` | The database `evernote-backup sync` wrote (default `en_backup.db`) |
| `-n, --notebook` | Only this notebook. Repeatable |
| `-t, --tag` | Only notes with this tag. Repeatable |
| `--include-trash` | Include notes from Evernote's trash |
| `--url` | Instance for one command, without storing it |

## Signing in

`login` prints a short code and a link. Approving it in a browser you are already signed in
to grants an ordinary session — no password is typed into the terminal, and no long-lived
key is pasted from a settings page.

The token is stored in `~/.config/unbent/credentials.json` at mode `0600`, keyed by
instance so a token for one server is never sent to another. `logout` forgets it locally;
signing out everywhere in Unbent revokes it for real.

## Which instance

There is deliberately no default. A guessed instance means a forgotten flag can send a
migration to a server you did not choose, and that failure is silent, because uploading to
the wrong Unbent still succeeds.

The answer is stored in `~/.config/unbent/config.json` — plain JSON, mode `0644`, separate
from the credentials file so changing a URL never means opening one with a token in it.

```bash
evernote2unbent set-url https://unbent.app
evernote2unbent show-config
```

`$UNBENT_URL` overrides it for a shell session, `--url` for a single command.

## What gets converted

Notes arrive as Markdown. The conversion happens server-side in Unbent, which is why this
package does not implement one: Unbent's own importer already has a tested ENML converter,
and a second implementation here would drift from it.

Attachments upload separately as raw bytes and are linked from the note body by their
content hash, which is how an inline image ends up in the right place.

## Credit

The hard part — Evernote authentication, the Thrift API client, and incremental sync — is
[evernote-backup](https://github.com/vzhd1701/evernote-backup) by
[vzhd1701](https://github.com/vzhd1701), used here as a dependency rather than forked. This
package adds an uploader and a CLI on top of it and modifies none of it.

If you want ENEX files rather than a migration, use evernote-backup directly; it does that
well and this is not a replacement for it.

## License

MIT
