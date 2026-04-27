"""C-06: Cockpit skill-call wrapper — observable-contract tests.

Covers:
  * ``upload_inputs`` rewrites ``@file`` / ``@data`` / legacy ``"@path"`` to
    ``knarr-asset://`` URIs.
  * ``fetch_outputs`` downloads and classifies asset-valued output fields.
  * The new helpers are module-level and importable.
"""
import asyncio
import base64
import os
from unittest.mock import AsyncMock, patch

import pytest

from knarr.cli import main as cli_main


def test_helpers_are_module_level():
    assert hasattr(cli_main, "upload_inputs")
    assert hasattr(cli_main, "fetch_outputs")
    assert callable(cli_main.upload_inputs)
    assert callable(cli_main.fetch_outputs)


@pytest.mark.asyncio
async def test_upload_inputs_rewrites_at_file_shape(tmp_path):
    p = tmp_path / "payload.bin"
    p.write_bytes(b"payload-bytes")
    data = {"input_file": {"@file": str(p)}}

    uploads = []

    async def fake_upload(host, port, blob, signing_key):
        uploads.append((host, port, blob))
        return "deadbeef" * 8  # 64 hex chars

    with patch.object(cli_main, "upload_asset", side_effect=fake_upload):
        await cli_main.upload_inputs(data, "127.0.0.1", 9031, signing_key=object())

    assert data["input_file"] == "knarr-asset://" + "deadbeef" * 8
    assert uploads == [("127.0.0.1", 9031, b"payload-bytes")]


@pytest.mark.asyncio
async def test_upload_inputs_rewrites_at_data_shape():
    raw = b"inline-bytes"
    data = {"blob": {"@data": base64.b64encode(raw).decode("ascii")}}

    async def fake_upload(host, port, blob, signing_key):
        assert blob == raw
        return "a" * 64

    with patch.object(cli_main, "upload_asset", side_effect=fake_upload):
        await cli_main.upload_inputs(data, "h", 9999, signing_key=object())

    assert data["blob"] == "knarr-asset://" + "a" * 64


@pytest.mark.asyncio
async def test_upload_inputs_rewrites_legacy_at_string(tmp_path):
    p = tmp_path / "legacy.txt"
    p.write_bytes(b"legacy")
    data = {"doc": "@" + str(p)}

    async def fake_upload(host, port, blob, signing_key):
        return "b" * 64

    with patch.object(cli_main, "upload_asset", side_effect=fake_upload):
        await cli_main.upload_inputs(data, "h", 9031, signing_key=object())

    assert data["doc"] == "knarr-asset://" + "b" * 64


@pytest.mark.asyncio
async def test_upload_inputs_skips_when_sidecar_port_zero():
    data = {"keep": {"@data": base64.b64encode(b"x").decode("ascii")}}

    async def fake_upload(*a, **kw):
        raise AssertionError("upload_asset must not be called when sidecar_port=0")

    with patch.object(cli_main, "upload_asset", side_effect=fake_upload):
        await cli_main.upload_inputs(data, "h", 0, signing_key=object())

    # untouched
    assert "keep" in data
    assert isinstance(data["keep"], dict) and "@data" in data["keep"]


@pytest.mark.asyncio
async def test_upload_inputs_missing_file_raises(tmp_path):
    data = {"bad": {"@file": str(tmp_path / "does-not-exist.bin")}}
    with pytest.raises(FileNotFoundError):
        await cli_main.upload_inputs(data, "h", 9031, signing_key=object())


@pytest.mark.asyncio
async def test_fetch_outputs_downloads_asset_uri(tmp_path):
    out = {"artifact": "knarr-asset://" + "c" * 64}
    fetched = []

    async def fake_download(host, port, h, signing_key):
        fetched.append((host, port, h))
        return b"artifact-bytes"

    with patch.object(cli_main, "download_asset", side_effect=fake_download):
        results = await cli_main.fetch_outputs(
            out, "p", 9031, signing_key=object(), output_dir=str(tmp_path),
        )

    assert len(results) == 1
    r = results[0]
    assert r["key"] == "artifact"
    assert r["hash"] == "c" * 64
    assert r["bytes"] == len(b"artifact-bytes")
    assert r["path"] and os.path.exists(r["path"])
    assert fetched == [("p", 9031, "c" * 64)]


@pytest.mark.asyncio
async def test_fetch_outputs_accepts_bare_hex_hash():
    out = {"x": "d" * 64}

    async def fake_download(host, port, h, signing_key):
        return b"x"

    with patch.object(cli_main, "download_asset", side_effect=fake_download):
        results = await cli_main.fetch_outputs(
            out, "p", 9031, signing_key=object(), output_dir=None,
        )

    assert len(results) == 1
    assert results[0]["hash"] == "d" * 64
    assert results[0]["path"] is None
    assert results[0]["data"] == b"x"


@pytest.mark.asyncio
async def test_fetch_outputs_ignores_non_asset_strings():
    out = {"a": "hello", "b": "not-a-hash", "c": 42}
    results = await cli_main.fetch_outputs(
        out, "p", 9031, signing_key=object(), output_dir=None,
    )
    assert results == []


@pytest.mark.asyncio
async def test_fetch_outputs_sanitizes_traversal_keys(tmp_path):
    """Provider-supplied keys with ../ segments must not escape output_dir."""
    malicious_key = "../../etc/passwd"
    out = {malicious_key: "knarr-asset://" + "e" * 64}

    async def fake_download(host, port, h, signing_key):
        return b"escape-attempt"

    with patch.object(cli_main, "download_asset", side_effect=fake_download):
        results = await cli_main.fetch_outputs(
            out, "p", 9031, signing_key=object(), output_dir=str(tmp_path),
        )

    assert len(results) == 1
    written = results[0]["path"]
    assert written is not None
    # The resolved path MUST be inside tmp_path — no traversal escape.
    resolved_dir = os.path.realpath(str(tmp_path))
    assert os.path.realpath(written).startswith(resolved_dir + os.sep), (
        f"path traversal escaped: {written!r} not under {resolved_dir!r}"
    )
    # And the offending separators are gone from the filename.
    basename = os.path.basename(written)
    assert ".." not in basename
    assert "/" not in basename and "\\" not in basename


def test_safe_output_filename_collapses_dangerous_input():
    out = cli_main._safe_output_filename("../../etc/passwd", "a" * 64)
    assert "/" not in out and "\\" not in out
    assert ".." not in out


def test_safe_output_filename_empty_key_falls_back():
    out = cli_main._safe_output_filename("", "b" * 64)
    assert out.startswith("asset_")


def test_cockpit_handler_does_not_accept_output_dir():
    """F7 hardening: the HTTP surface must not expose an output_dir knob.

    Rationale: any server-side file write primitive driven from the wire
    expands the cockpit surface beyond its intended scope. Callers that
    want local files use fetch_outputs via the CLI.
    """
    from pathlib import Path
    import knarr
    server_py = Path(knarr.__file__).parent / "dashboard" / "server.py"
    text = server_py.read_text(encoding="utf-8")
    # Locate the skills/call handler and assert output_dir isn't read from
    # the parsed body in that region.
    idx = text.find("async def _handle_api_skills_call")
    assert idx >= 0, "handler definition not found"
    region = text[idx: idx + 8000]
    assert 'data.get("output_dir")' not in region, (
        "cockpit handler must not accept output_dir from the request body"
    )


def test_cockpit_route_registered():
    """The /api/skills/call route must be wired in the POST dispatch block."""
    from pathlib import Path
    import knarr
    server_py = Path(knarr.__file__).parent / "dashboard" / "server.py"
    text = server_py.read_text(encoding="utf-8")
    assert "/api/skills/call" in text, "route path not registered in server.py"
    assert "_handle_api_skills_call" in text, "handler not defined in server.py"


def test_cockpit_handler_rejects_at_file_shape():
    """F7 hardening: the cockpit handler must not honour @file / @-prefix.

    Without this guard, any authenticated cockpit caller could cause the
    server process to read arbitrary local files and exfiltrate their
    bytes to a remote sidecar via upload_asset.
    """
    from pathlib import Path
    import knarr
    server_py = Path(knarr.__file__).parent / "dashboard" / "server.py"
    text = server_py.read_text(encoding="utf-8")
    idx = text.find("async def _handle_api_skills_call")
    assert idx >= 0, "handler definition not found"
    region = text[idx: idx + 8000]
    # The handler must guard @-prefixed strings and {"@file": ...} dicts
    # before invoking upload_inputs.
    assert "@file" in region, (
        "cockpit handler missing @file rejection guard"
    )
    assert 'startswith("@")' in region, (
        "cockpit handler missing @-prefix string rejection guard"
    )


def test_cmd_request_uses_fetch_outputs_for_downloads():
    """F7 hardening: the CLI request path must not open-code the download loop.

    The original cmd_request block did os.path.join(output_dir,
    f"{key}_{hash[:12]}") where key came from res.output_data — exactly the
    traversal vector fetch_outputs was written to defend against. Assert
    the inline pattern is gone and fetch_outputs is used instead.
    """
    from pathlib import Path
    import knarr
    main_py = Path(knarr.__file__).parent / "cli" / "main.py"
    text = main_py.read_text(encoding="utf-8")
    # The old vulnerable form must not be present:
    assert 'os.path.join(args.output_dir, f"{key}_' not in text, (
        "cmd_request still constructs filename inline — traversal vulnerable"
    )
    # And fetch_outputs must be called from cmd_request's completed branch:
    assert "await fetch_outputs(" in text, (
        "cmd_request does not delegate to fetch_outputs"
    )
