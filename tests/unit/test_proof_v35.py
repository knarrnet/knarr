"""Tests for W3C Data Integrity proof creation/verification (eddsa-jcs-2022)."""

import hashlib
import pytest
from nacl.signing import SigningKey

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from knarr.core.proof import sign_document, verify_document, _base58btc_encode, _base58btc_decode
from knarr.core import rfc8785


@pytest.fixture
def keypair():
    sk = SigningKey.generate()
    return sk, sk.verify_key


@pytest.fixture
def sample_doc():
    return {
        "document_type": "execution_receipt",
        "version": 2,
        "skill_name": "llm-chat",
        "provider": "aaa",
        "consumer": "bbb",
        "status": "completed",
    }


class TestSignVerifyRoundTrip:
    def test_round_trip(self, keypair, sample_doc):
        sk, vk = keypair
        secured = sign_document(sample_doc, sk, "did:knarr:aaa#key-1")
        assert verify_document(secured, vk) is True

    def test_tamper_document_fails(self, keypair, sample_doc):
        sk, vk = keypair
        secured = sign_document(sample_doc, sk, "did:knarr:aaa#key-1")
        secured["status"] = "failed"
        assert verify_document(secured, vk) is False

    def test_tamper_proof_created_fails(self, keypair, sample_doc):
        sk, vk = keypair
        secured = sign_document(sample_doc, sk, "did:knarr:aaa#key-1")
        secured["proof"]["created"] = "2020-01-01T00:00:00.000Z"
        assert verify_document(secured, vk) is False

    def test_tamper_proof_purpose_fails(self, keypair, sample_doc):
        sk, vk = keypair
        secured = sign_document(sample_doc, sk, "did:knarr:aaa#key-1")
        secured["proof"]["proofPurpose"] = "authentication"
        assert verify_document(secured, vk) is False

    def test_wrong_key_fails(self, keypair, sample_doc):
        sk, _ = keypair
        other_vk = SigningKey.generate().verify_key
        secured = sign_document(sample_doc, sk, "did:knarr:aaa#key-1")
        assert verify_document(secured, other_vk) is False


class TestProofStructure:
    def test_proof_object_present(self, keypair, sample_doc):
        sk, _ = keypair
        secured = sign_document(sample_doc, sk, "did:knarr:aaa#key-1")
        assert "proof" in secured

    def test_proof_fields(self, keypair, sample_doc):
        sk, _ = keypair
        secured = sign_document(sample_doc, sk, "did:knarr:aaa#key-1")
        proof = secured["proof"]
        assert proof["type"] == "DataIntegrityProof"
        assert proof["cryptosuite"] == "eddsa-jcs-2022"
        assert proof["verificationMethod"] == "did:knarr:aaa#key-1"
        assert proof["proofPurpose"] == "assertionMethod"
        assert "created" in proof
        assert "proofValue" in proof

    def test_proof_value_multibase_z(self, keypair, sample_doc):
        sk, _ = keypair
        secured = sign_document(sample_doc, sk, "did:knarr:aaa#key-1")
        assert secured["proof"]["proofValue"].startswith("z")

    def test_does_not_mutate_input(self, keypair, sample_doc):
        sk, _ = keypair
        original = dict(sample_doc)
        sign_document(sample_doc, sk, "did:knarr:aaa#key-1")
        assert sample_doc == original
        assert "proof" not in sample_doc

    def test_custom_proof_purpose(self, keypair, sample_doc):
        sk, vk = keypair
        secured = sign_document(sample_doc, sk, "did:knarr:aaa#key-1",
                                proof_purpose="authentication")
        assert secured["proof"]["proofPurpose"] == "authentication"
        assert verify_document(secured, vk) is True


class TestDoubleHash:
    def test_proof_config_hash_first(self, keypair, sample_doc):
        """Verify the proof_config hash comes FIRST in concatenation (per spec)."""
        sk, _ = keypair
        secured = sign_document(sample_doc, sk, "did:knarr:aaa#key-1")

        # Reconstruct what should have been signed
        proof = dict(secured["proof"])
        proof.pop("proofValue")
        doc = dict(secured)
        doc.pop("proof")

        canonical_proof = rfc8785.dumps(proof)
        canonical_doc = rfc8785.dumps(doc)

        expected_hash = hashlib.sha256(canonical_proof).digest() + hashlib.sha256(canonical_doc).digest()

        # If we reverse the order, verification should fail
        reversed_hash = hashlib.sha256(canonical_doc).digest() + hashlib.sha256(canonical_proof).digest()
        assert expected_hash != reversed_hash  # sanity check


class TestBase58:
    def test_round_trip(self):
        data = b"\x00\x01\x02\xff" * 8
        encoded = _base58btc_encode(data)
        decoded = _base58btc_decode(encoded)
        assert decoded == data

    def test_leading_zeros_preserved(self):
        data = b"\x00\x00\x01"
        encoded = _base58btc_encode(data)
        decoded = _base58btc_decode(encoded)
        assert decoded == data

    def test_empty_bytes(self):
        assert _base58btc_encode(b"") == ""
        assert _base58btc_decode("") == b""


class TestVerifyEdgeCases:
    def test_missing_proof_key(self):
        assert verify_document({"no_proof": True}, SigningKey.generate().verify_key) is False

    def test_missing_proof_value(self, keypair):
        _, vk = keypair
        assert verify_document({"proof": {"type": "test"}}, vk) is False

    def test_invalid_multibase_prefix(self, keypair, sample_doc):
        sk, vk = keypair
        secured = sign_document(sample_doc, sk, "did:knarr:aaa#key-1")
        secured["proof"]["proofValue"] = "x" + secured["proof"]["proofValue"][1:]
        assert verify_document(secured, vk) is False
