import json
import sys

import pytest

from evernote2unbent.config import (
    config_path,
    normalize_url,
    read_url,
    write_url,
)


@pytest.fixture(autouse=True)
def _config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


class TestNormalizeUrl:
    def test_reduces_to_an_origin(self):
        assert normalize_url("https://notes.example.com/notes?tag=x") == (
            "https://notes.example.com"
        )

    def test_assumes_https_for_a_bare_host(self):
        assert normalize_url("notes.example.com") == "https://notes.example.com"

    def test_keeps_http_and_its_port(self):
        assert normalize_url("http://localhost:5173") == "http://localhost:5173"

    def test_reads_a_bare_host_port_as_a_host(self):
        """`localhost:5173` is what people type locally; a naive check rejects it."""
        assert normalize_url("localhost:5173") == "https://localhost:5173"
        assert normalize_url("127.0.0.1:8787") == "https://127.0.0.1:8787"

    def test_ignores_surrounding_whitespace(self):
        assert normalize_url("  notes.example.com  ") == "https://notes.example.com"

    @pytest.mark.parametrize(
        "value", ["file:///etc/passwd", "ftp://example.com", "javascript:alert(1)"]
    )
    def test_refuses_a_non_http_scheme(self, value):
        """
        Rejected before anything is prepended.

        Testing only for a leading `http(s)://` and prefixing everything else turns
        `file:///etc/passwd` into `https://file:///etc/passwd`, which parses as the
        host `file` — a real, fetchable origin nobody typed.
        """
        with pytest.raises(ValueError, match="http"):
            normalize_url(value)

    def test_refuses_an_empty_value(self):
        with pytest.raises(ValueError):
            normalize_url("   ")


class TestConfigStore:
    def test_round_trips(self):
        write_url("https://notes.example.com")
        assert read_url() == "https://notes.example.com"

    def test_unset_reads_as_none(self):
        assert read_url() is None

    def test_normalizes_on_the_way_in(self):
        write_url("notes.example.com/notes/")
        assert json.loads(config_path().read_text())["url"] == (
            "https://notes.example.com"
        )

    @pytest.mark.skipif(
        sys.platform == "win32", reason="Windows ignores POSIX mode bits."
    )
    def test_written_editable(self):
        """0644, unlike credentials.json — this file is meant to be opened and edited."""
        write_url("https://notes.example.com")
        assert config_path().stat().st_mode & 0o777 == 0o644

    def test_survives_a_corrupt_file(self):
        config_path().parent.mkdir(parents=True, exist_ok=True)
        config_path().write_text("not json")

        assert read_url() is None
        write_url("https://notes.example.com")
        assert read_url() == "https://notes.example.com"

    def test_survives_a_hand_edited_bad_value(self):
        """A typo in the file should prompt for a URL, not crash every command."""
        config_path().parent.mkdir(parents=True, exist_ok=True)
        config_path().write_text(json.dumps({"url": "ftp://nope"}))

        assert read_url() is None

    def test_picks_up_a_hand_edited_value(self):
        config_path().parent.mkdir(parents=True, exist_ok=True)
        config_path().write_text(json.dumps({"url": "https://edited.example.com"}))

        assert read_url() == "https://edited.example.com"

    def test_is_not_the_credentials_file(self):
        """The token lives in credentials.json at 0600; this must never be that file."""
        assert config_path().name == "config.json"
