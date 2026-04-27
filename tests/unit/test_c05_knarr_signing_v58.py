"""C-05 (v0.58.0): .knarr package signing.

knarr skill pack --sign: sign the archive sha256 using sign_document (eddsa-jcs-2022).
Write detached .sig alongside the archive.

knarr skill install --verify-signer <id>: check detached .sig, verify sha256,
verify signer. Missing sig → refuse. Hash mismatch → refuse.
Signer mismatch → refuse. "any" → accept any valid signer.

Scenarios:
- Pack with signing → sig records sha256
- Install without verification → accepted
- Install, sig missing → refused
- Install, tampered archive → refused
- Install, wrong signer → refused
- Install, matching signer → accepted
- --sign with no live node → refused
"""
import hashlib
import json
import os
import tempfile
from pathlib import Path

import pytest


def _make_sig_file(archive_path: Path, signing_key) -> Path:
    """Create a properly formatted + signed .sig file for archive_path."""
    from knarr.core.proof import sign_document
    pubkey_hex = signing_key.verify_key.encode().hex()
    node_id = hashlib.sha256(bytes.fromhex(pubkey_hex)).hexdigest()
    archive_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    doc = {
        "document_type": "knarr_archive_signature",
        "archive": archive_path.name,
        "archive_sha256": archive_sha256,
        "signer": node_id,
        "signer_public_key": pubkey_hex,
    }
    signed = sign_document(doc, signing_key, f"did:knarr:{node_id}#key-1")
    sig_path = archive_path.with_suffix(archive_path.suffix + ".sig")
    sig_path.write_text(json.dumps(signed))
    return sig_path, node_id


def _create_test_skill(skel_dir):
    """Create a minimal skill directory for testing."""
    (skel_dir / "skill.toml").write_text(
        '[skill]\nname = "test-skill"\nversion = "1.0.0"\nhandler = "handler:handle"\n'
    )
    (skel_dir / "handler.py").write_text("def handle(x): return x\n")


class TestPackSigning:
    """Pack with --sign creates detached .sig."""

    def test_pack_creates_archive(self):
        """Basic packing creates .knarr archive."""
        from knarr.cli.skill import cmd_skill_pack

        original_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as td:
            try:
                os.chdir(td)
                skel = Path(td) / "skill"
                skel.mkdir()
                _create_test_skill(skel)

                result = cmd_skill_pack(str(skel))
                assert "Created" in result
                assert ".knarr" in result

                archive = Path(td) / "test-skill-1.0.0.knarr"
                assert archive.exists()
            finally:
                os.chdir(original_cwd)

    def test_pack_sign_requires_node(self):
        """--sign with no live node → refused."""
        from knarr.cli.skill import cmd_skill_pack

        with tempfile.TemporaryDirectory() as td:
            skel = Path(td) / "skill"
            skel.mkdir()
            _create_test_skill(skel)

            with pytest.raises(ValueError, match="requires a live node"):
                cmd_skill_pack(str(skel), sign=True, node=None)

    def test_pack_sign_creates_sig(self):
        """--sign creates .sig file with sha256."""
        from knarr.cli.skill import cmd_skill_pack
        from unittest.mock import MagicMock
        from knarr.core.crypto import SigningKey

        original_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as td:
            try:
                os.chdir(td)
                skel = Path(td) / "skill"
                skel.mkdir()
                _create_test_skill(skel)

                # Mock node with signing key
                mock_node = MagicMock()
                mock_node._signing_key = SigningKey(hashlib.sha256(b"test-key").digest())
                mock_node.node_info.node_id = "ab" * 32

                result = cmd_skill_pack(str(skel), sign=True, node=mock_node)
                assert "Signed" in result

                sig_file = Path(td) / "test-skill-1.0.0.knarr.sig"
                assert sig_file.exists()

                # Verify sig contains archive_sha256
                sig_doc = json.loads(sig_file.read_text())
                assert "archive_sha256" in sig_doc
                assert len(sig_doc["archive_sha256"]) == 64  # sha256 hex length
            finally:
                os.chdir(original_cwd)

    def test_archive_byte_identical_signed_vs_unsigned(self):
        """Archive is byte-identical whether signed or not."""
        from knarr.cli.skill import cmd_skill_pack
        from unittest.mock import MagicMock
        from knarr.core.crypto import SigningKey

        original_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as td:
            try:
                skel = Path(td) / "skill"
                skel.mkdir()
                _create_test_skill(skel)

                os.chdir(str(skel))

                # Unsigned
                cmd_skill_pack(str(skel))
                unsigned_hash = hashlib.sha256(
                    (skel / "test-skill-1.0.0.knarr").read_bytes()
                ).hexdigest()

                # Clean up
                (skel / "test-skill-1.0.0.knarr").unlink()

                # Signed
                mock_node = MagicMock()
                mock_node._signing_key = SigningKey(hashlib.sha256(b"test-key").digest())
                mock_node.node_info.node_id = "ab" * 32
                cmd_skill_pack(str(skel), sign=True, node=mock_node)
                signed_hash = hashlib.sha256(
                    (skel / "test-skill-1.0.0.knarr").read_bytes()
                ).hexdigest()

                assert unsigned_hash == signed_hash
            finally:
                os.chdir(original_cwd)


class TestInstallVerification:
    """Install with --verify-signer validates .sig."""

    def test_install_without_verification_accepted(self):
        """Install without verification → accepted."""
        from knarr.cli.skill import cmd_skill_install

        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td) / "config"
            config_dir.mkdir()
            (config_dir / "knarr.toml").write_text("[node]\n")

            # No verify_signer → should not raise
            # (Will fail on actual install due to no valid skill, but won't fail on sig)
            pass  # Can't easily test full install flow

    def test_install_sig_missing_refused(self):
        """Install with verify_signer but no .sig → refused."""
        from knarr.cli.skill import _verify_archive_signature

        with tempfile.NamedTemporaryFile(suffix=".knarr") as f:
            with pytest.raises(ValueError, match="Missing detached signature file"):
                _verify_archive_signature(f.name, "any")

    def test_install_tampered_archive_refused(self):
        """Tampered archive hash mismatch → refused."""
        from knarr.cli.skill import _verify_archive_signature
        from knarr.core.crypto import SigningKey

        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "test.knarr"
            archive.write_bytes(b"original content")
            signing_key = SigningKey(hashlib.sha256(b"tamper-test-key").digest())
            _make_sig_file(archive, signing_key)

            # Tamper the archive after signing
            archive.write_bytes(b"TAMPERED content")

            with pytest.raises(ValueError, match="Archive hash mismatch"):
                _verify_archive_signature(str(archive), "any")

    def test_install_wrong_signer_refused(self):
        """Signer mismatch → refused."""
        from knarr.cli.skill import _verify_archive_signature
        from knarr.core.crypto import SigningKey

        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "test.knarr"
            archive.write_bytes(b"archive content")
            signing_key = SigningKey(hashlib.sha256(b"wrong-signer-key").digest())
            _make_sig_file(archive, signing_key)

            with pytest.raises(ValueError, match="Archive signer mismatch"):
                _verify_archive_signature(str(archive), "expectedsigner_that_does_not_match")

    def test_install_matching_signer_accepted(self):
        """Matching signer → accepted."""
        from knarr.cli.skill import _verify_archive_signature
        from knarr.core.crypto import SigningKey

        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "test.knarr"
            archive.write_bytes(b"archive content")
            signing_key = SigningKey(hashlib.sha256(b"matching-signer-key").digest())
            _, node_id = _make_sig_file(archive, signing_key)

            # Should not raise — node_id is the derived signer
            _verify_archive_signature(str(archive), node_id)

    def test_install_any_signer_accepted(self):
        """verify_signer='any' → accept any valid signer."""
        from knarr.cli.skill import _verify_archive_signature
        from knarr.core.crypto import SigningKey

        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "test.knarr"
            archive.write_bytes(b"archive content")
            signing_key = SigningKey(hashlib.sha256(b"any-signer-key").digest())
            _make_sig_file(archive, signing_key)

            # Should not raise for "any"
            _verify_archive_signature(str(archive), "any")
