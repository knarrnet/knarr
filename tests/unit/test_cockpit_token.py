"""Tests for cockpit auth token auto-generation and persistence."""
import os
import tempfile
import pytest
from knarr.cli.main import resolve_cockpit_token


def test_configured_token_returned_as_is():
    """Explicit auth_token in config takes priority over auto-generation."""
    with tempfile.TemporaryDirectory() as d:
        assert resolve_cockpit_token(d, "my-explicit-token") == "my-explicit-token"
        assert not os.path.exists(os.path.join(d, ".cockpit_token"))


def test_auto_generates_token_when_none_configured():
    """Generates a knarr-prefixed token and persists it to .cockpit_token."""
    with tempfile.TemporaryDirectory() as d:
        token = resolve_cockpit_token(d, "")
        assert token.startswith("knarr-")
        assert len(token) == 18  # "knarr-" + 12 hex chars
        # Verify persistence
        token_path = os.path.join(d, ".cockpit_token")
        assert os.path.isfile(token_path)
        with open(token_path) as f:
            assert f.read().strip() == token


def test_reads_persisted_token_on_subsequent_call():
    """Second call reads token from file instead of generating new one."""
    with tempfile.TemporaryDirectory() as d:
        first = resolve_cockpit_token(d, "")
        second = resolve_cockpit_token(d, "")
        assert first == second


def test_init_template_includes_cockpit():
    """Sentinel: knarr init template includes [cockpit] section."""
    from knarr.cli.init import KNARR_TOML_TEMPLATE
    rendered = KNARR_TOML_TEMPLATE.substitute(port=9000, bootstrap="bootstrap1.knarr.network:9000")
    assert "[cockpit]" in rendered
    assert "port = 8090" in rendered
