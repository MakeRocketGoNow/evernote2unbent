import json
import os
from pathlib import Path
from urllib.parse import urlparse

#: Deliberately the same file and shape as the TypeScript ``unbent-migrate`` CLI writes.
#: Signing in with one tool should not mean signing in again with the other.
CONFIG_DIR_NAME = "unbent"
CREDENTIALS_NAME = "credentials.json"


def _config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / CONFIG_DIR_NAME / CREDENTIALS_NAME


def _key(base_url: str) -> str:
    """Origin only, so http://host and http://host/ are not two separate entries."""
    parsed = urlparse(base_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def read_token(base_url: str) -> str | None:
    """The stored session for one instance, or None to mean 'sign in'."""
    try:
        data = json.loads(_config_path().read_text())
    except (OSError, ValueError):
        # Missing or unreadable is not an error: the caller's next move is to sign in
        # anyway. A corrupt file is treated the same rather than blocking the CLI.
        return None
    token = data.get("tokens", {}).get(_key(base_url))
    return str(token) if token else None


def write_token(base_url: str, token: str) -> None:
    """
    Store a session, keyed by instance so one server never receives another's token.

    The file holds a real credential, so it is created 0600 inside a 0700 directory. chmod
    runs after the write as well: the mode at creation does not apply to a file that
    already exists, which would otherwise keep whatever permissions it had.
    """
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}

    data.setdefault("tokens", {})[_key(base_url)] = token
    path.write_text(json.dumps(data, indent=2) + "\n")
    path.chmod(0o600)


def clear_token(base_url: str) -> None:
    """Forget one instance's session locally. Revocation itself happens in the app."""
    path = _config_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return

    data.get("tokens", {}).pop(_key(base_url), None)
    path.write_text(json.dumps(data, indent=2) + "\n")
    path.chmod(0o600)
