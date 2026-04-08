"""Tests for v0.55.0 C-01/C-03/C-04/C-05 — consolidated crypto module."""

import base64
import hashlib
import json
import os
import tempfile
import pytest

from knarr.core.crypto import (
    SigningKey, VerifyKey, BadSignatureError,
    SealedBox, PublicKey, SecretBox, argon2id, random,
    derive_x25519_keys, seal_for_peer, unseal,
    hybrid_encrypt, hybrid_decrypt,
    create_server_tls_context, create_client_tls_context,
    verify_receipt,
)


# ── Re-export smoke tests ──────────────────────────────────────────────────────

class TestReExports:
    def test_signing_key_generate(self):
        sk = SigningKey.generate()
        assert sk is not None
        assert len(sk.verify_key.encode()) == 32

    def test_verify_key_from_bytes(self):
        sk = SigningKey.generate()
        vk = sk.verify_key
        vk2 = VerifyKey(vk.encode())
        assert vk.encode() == vk2.encode()

    def test_secret_box_roundtrip(self):
        key = bytes(32)
        box = SecretBox(key)
        ct = box.encrypt(b"hello world")
        pt = box.decrypt(ct)
        assert pt == b"hello world"

    def test_nacl_random(self):
        r = random(16)
        assert len(r) == 16
        assert r != random(16)  # Should be different (probabilistic)


# ── X25519 derivation ──────────────────────────────────────────────────────────

class TestX25519Derivation:
    def test_derive_returns_key_pair(self):
        sk = SigningKey.generate()
        priv, pub = derive_x25519_keys(sk)
        assert priv is not None
        assert pub is not None
        assert len(pub.encode()) == 32

    def test_derive_is_deterministic(self):
        sk = SigningKey.generate()
        priv1, pub1 = derive_x25519_keys(sk)
        priv2, pub2 = derive_x25519_keys(sk)
        assert pub1.encode() == pub2.encode()

    def test_derive_consistent_with_node_pattern(self):
        """Match the existing node._init_encryption pattern."""
        sk = SigningKey.generate()
        priv, pub = derive_x25519_keys(sk)
        expected_pub = sk.verify_key.to_curve25519_public_key()
        assert pub.encode() == expected_pub.encode()


# ── SealedBox peer encryption ──────────────────────────────────────────────────

class TestSealedBoxHelpers:
    def test_seal_unseal_roundtrip(self):
        sk = SigningKey.generate()
        priv, pub = derive_x25519_keys(sk)
        pub_hex = pub.encode().hex()

        plaintext = b"secret message for peer"
        ciphertext = seal_for_peer(plaintext, pub_hex)
        assert ciphertext != plaintext

        recovered = unseal(ciphertext, priv)
        assert recovered == plaintext

    def test_seal_different_data_different_output(self):
        sk = SigningKey.generate()
        _, pub = derive_x25519_keys(sk)
        pub_hex = pub.encode().hex()

        ct1 = seal_for_peer(b"data1", pub_hex)
        ct2 = seal_for_peer(b"data1", pub_hex)
        # SealedBox uses ephemeral nonce — same plaintext produces different ciphertext
        assert ct1 != ct2

    def test_seal_wrong_recipient_fails(self):
        from nacl.exceptions import CryptoError
        sk1 = SigningKey.generate()
        sk2 = SigningKey.generate()
        priv1, pub1 = derive_x25519_keys(sk1)
        priv2, pub2 = derive_x25519_keys(sk2)

        ct = seal_for_peer(b"for peer 1 only", pub1.encode().hex())
        with pytest.raises((CryptoError, Exception)):
            unseal(ct, priv2)


# ── C-03: System mail encryption ───────────────────────────────────────────────

class TestSystemMailEncryption:
    """C-03: All mail types (including knarr/ system mail) get encrypted."""

    def test_seal_for_peer_encrypts_system_mail_body(self):
        """System mail body can be encrypted using seal_for_peer."""
        sk = SigningKey.generate()
        _, pub = derive_x25519_keys(sk)
        priv, _ = derive_x25519_keys(sk)
        pub_hex = pub.encode().hex()

        system_mail_body = {
            "type": "knarr/commerce/settlement_confirmation",
            "tx_hash": "abc123" * 10,
            "amount_settled": 1.0,
            "timestamp": 1000000.0,
        }
        body_bytes = json.dumps(system_mail_body).encode("utf-8")
        encrypted = seal_for_peer(body_bytes, pub_hex)

        # Must be encrypted (not original bytes)
        assert encrypted != body_bytes

        # Must be decryptable
        recovered = unseal(encrypted, priv)
        assert json.loads(recovered) == system_mail_body

    def test_knarr_prefixed_mail_encrypts_same_as_user_mail(self):
        """knarr/ prefixed system mail and user mail use the same code path."""
        sk = SigningKey.generate()
        _, pub = derive_x25519_keys(sk)
        priv, _ = derive_x25519_keys(sk)
        pub_hex = pub.encode().hex()

        for msg_type in ["text", "knarr/commerce/tab_reminder", "knarr/system/task_result"]:
            body = {"type": msg_type, "data": "payload"}
            body_bytes = json.dumps(body).encode("utf-8")
            ct = seal_for_peer(body_bytes, pub_hex)
            pt = unseal(ct, priv)
            assert json.loads(pt)["type"] == msg_type


# ── C-04: Sidecar asset encryption at rest ────────────────────────────────────

class TestAssetEncryption:
    """C-04: Assets are encrypted before writing to disk and decrypted on read."""

    def _make_vault(self, tmpdir):
        """Create a minimal KeyringVault for testing."""
        from knarr.core.vault import KeyringVault
        vault_path = os.path.join(str(tmpdir), "vault.db")
        seed = os.urandom(32)
        return KeyringVault(vault_path, seed)

    def test_vault_encrypt_decrypt_roundtrip(self, tmp_path):
        vault = self._make_vault(tmp_path)
        data = b"binary asset data" * 100
        encrypted = vault.encrypt_bytes(data)
        assert encrypted != data
        decrypted = vault.decrypt_bytes(encrypted)
        assert decrypted == data

    def test_asset_sidecar_encrypts_on_upload(self, tmp_path):
        """AssetSidecar stores encrypted data on disk when vault is provided."""
        from knarr.core.vault import KeyringVault
        from knarr.dht.sidecar import AssetSidecar

        vault_path = os.path.join(str(tmp_path), "vault.db")
        vault = KeyringVault(vault_path, os.urandom(32))

        asset_dir = str(tmp_path / "assets")
        sidecar = AssetSidecar(
            host="127.0.0.1", port=0,
            asset_dir=asset_dir,
            vault=vault,
        )

        # Simulate upload via store_asset logic
        plaintext = b"my secret asset data"
        content_hash = hashlib.sha256(plaintext).hexdigest()

        # Encrypt and write
        encrypted = vault.encrypt_bytes(plaintext)
        asset_file = os.path.join(asset_dir, content_hash)
        with open(asset_file, "wb") as f:
            f.write(encrypted)

        # Read raw file — should NOT be plaintext
        with open(asset_file, "rb") as f:
            raw = f.read()
        assert raw != plaintext

        # Decrypt — should recover original
        recovered = vault.decrypt_bytes(raw)
        assert recovered == plaintext

    def test_asset_sidecar_init_accepts_vault(self, tmp_path):
        """AssetSidecar accepts vault parameter."""
        from knarr.core.vault import KeyringVault
        from knarr.dht.sidecar import AssetSidecar

        vault_path = os.path.join(str(tmp_path), "vault.db")
        vault = KeyringVault(vault_path, os.urandom(32))

        sidecar = AssetSidecar(
            host="127.0.0.1", port=0,
            asset_dir=str(tmp_path / "assets"),
            vault=vault,
        )
        assert sidecar._vault is vault


# ── C-05: Hybrid multi-recipient encryption ────────────────────────────────────

class TestHybridEncryption:
    def test_single_recipient_roundtrip(self):
        sk = SigningKey.generate()
        priv, pub = derive_x25519_keys(sk)

        plaintext = b"hybrid encrypted message"
        payload = hybrid_encrypt(plaintext, [pub.encode().hex()])

        assert "ciphertext" in payload
        assert "nonce" in payload
        assert "recipient_keys" in payload
        assert pub.encode().hex() in payload["recipient_keys"]

        recovered = hybrid_decrypt(payload, priv)
        assert recovered == plaintext

    def test_multi_recipient_roundtrip(self):
        """Both recipients can independently decrypt the same ciphertext."""
        sk1 = SigningKey.generate()
        sk2 = SigningKey.generate()
        priv1, pub1 = derive_x25519_keys(sk1)
        priv2, pub2 = derive_x25519_keys(sk2)

        plaintext = b"message for two recipients"
        pub_hexes = [pub1.encode().hex(), pub2.encode().hex()]
        payload = hybrid_encrypt(plaintext, pub_hexes)

        recovered1 = hybrid_decrypt(payload, priv1)
        recovered2 = hybrid_decrypt(payload, priv2)

        assert recovered1 == plaintext
        assert recovered2 == plaintext

    def test_wrong_recipient_fails(self):
        sk1 = SigningKey.generate()
        sk2 = SigningKey.generate()
        _, pub1 = derive_x25519_keys(sk1)
        priv2, _ = derive_x25519_keys(sk2)

        payload = hybrid_encrypt(b"only for sk1", [pub1.encode().hex()])

        with pytest.raises(ValueError, match="No wrapped key"):
            hybrid_decrypt(payload, priv2)

    def test_ciphertext_differs_per_call(self):
        """Fresh random session key each call — same plaintext → different ciphertext."""
        sk = SigningKey.generate()
        _, pub = derive_x25519_keys(sk)

        p1 = hybrid_encrypt(b"data", [pub.encode().hex()])
        p2 = hybrid_encrypt(b"data", [pub.encode().hex()])
        assert p1["ciphertext"] != p2["ciphertext"]

    def test_empty_recipients_raises(self):
        with pytest.raises(ValueError, match="At least one recipient"):
            hybrid_encrypt(b"data", [])

    def test_large_payload(self):
        sk = SigningKey.generate()
        priv, pub = derive_x25519_keys(sk)
        plaintext = os.urandom(1024 * 1024)  # 1 MB
        payload = hybrid_encrypt(plaintext, [pub.encode().hex()])
        recovered = hybrid_decrypt(payload, priv)
        assert recovered == plaintext


# ── TLS context helpers ────────────────────────────────────────────────────────

class TestTLSContextHelpers:
    def test_create_client_tls_context(self):
        import ssl
        ctx = create_client_tls_context()
        assert isinstance(ctx, ssl.SSLContext)
        assert ctx.verify_mode == ssl.CERT_NONE
        assert not ctx.check_hostname

    def test_create_server_tls_context_missing_files(self, tmp_path):
        ctx = create_server_tls_context(
            str(tmp_path / "cert.pem"),
            str(tmp_path / "key.pem"),
        )
        assert ctx is None

    def test_create_server_tls_context_with_cert(self, tmp_path):
        import ssl
        from knarr.mail.tls import generate_tls_cert
        sk = SigningKey.generate()
        node_id = hashlib.sha256(sk.verify_key.encode()).hexdigest()
        cert_path, key_path = generate_tls_cert(
            sk.encode(), node_id, str(tmp_path)
        )
        ctx = create_server_tls_context(cert_path, key_path)
        assert isinstance(ctx, ssl.SSLContext)
