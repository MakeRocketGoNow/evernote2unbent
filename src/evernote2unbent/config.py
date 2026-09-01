import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

#: Deliberately a separate file from `credentials.json`, and the same one the TypeScript
#: CLI writes. The credentials file is 0600 and holds a live session token; this one is
#: meant to be opened and edited by hand, and telling someone to hand-edit a secrets file
#: to change a URL is how tokens end up pasted into issues and screenshots.
CONFIG_NAME = "config.json"

_SCHEME = re.compile(r"^([a-z][a-z0-9+.-]*):(?!\d)", re.IGNORECASE)


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "unbent" / CONFIG_NAME


def normalize_url(value: str) -> str:
    """
    Reduce user input to an origin, refusing anything that is not http(s).

    A bare host is assumed https, because someone typing `unbent.app` means the
    site and failing them over a missing scheme is pedantry. A scheme that is present but
    not http(s) is rejected *before* anything is prepended: prefixing blindly turns
    `file:///etc/passwd` into `https://file:///etc/passwd`, which parses cleanly as the
    host `file` — a real, fetchable origin nobody typed. Failing to parse is safe; parsing
    into something else is not.
    """
    trimmed = value.strip()
    if not trimmed:
        raise ValueError("No URL given.")

    match = _SCHEME.match(trimmed)
    scheme = match.group(1).lower() if match else None
    if scheme and scheme not in ("http", "https"):
        raise ValueError(f"Not an http(s) URL: {value}")

    # The negative lookahead above keeps `localhost:5173` a host and port rather than a
    # scheme named "localhost", which is the likeliest thing anyone types locally.
    candidate = trimmed if scheme else f"https://{trimmed}"

    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"Not an http(s) URL: {value}")
    return f"{parsed.scheme}://{parsed.netloc}"


def read_url() -> str | None:
    """The stored instance, or None when this machine has not been pointed at one."""
    try:
        data = json.loads(config_path().read_text())
    except (OSError, ValueError):
        # Missing or unreadable means "not configured", which the caller handles by
        # asking. A corrupt file is treated the same rather than blocking every command.
        return None
    url = data.get("url")
    if not url:
        return None
    try:
        return normalize_url(str(url))
    except ValueError:
        # A hand-edited file with a bad value should prompt, not crash.
        return None


def write_url(value: str) -> str:
    """Point this machine at an instance. 0644, because it is meant to be edited."""
    normalized = normalize_url(value)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps({"url": normalized}, indent=2) + "\n")
    path.chmod(0o644)
    return normalized
