import base64
import hashlib
import json
import logging
import time

from knarr.core.crypto import SigningKey, verify_skill_sample
from knarr.core.models import SkillSheet
from knarr.dht.node import DHTNode
from knarr.dht.storage import Storage


NOW = 1_800_000_000.0
MAX_AGE = 7 * 24 * 60 * 60


def _signed_sample(signing_key, *, timestamp=NOW, extra=None, sign_timestamp=True):
    pub_hex = signing_key.verify_key.encode().hex()
    payload = {
        "skill_name": "demo-skill",
        "provider_node_id": "11" * 32,
        "test_input_hash": "sha256:" + "22" * 32,
        "test_output_hash": "sha256:" + "33" * 32,
        "primary_score": 0.97,
        "verdict": "pass",
        "test_timestamp": "2026-04-22T00:00:00Z",
        "rater_pubkey": pub_hex,
        "rater_node_id": hashlib.sha256(bytes.fromhex(pub_hex)).hexdigest(),
    }
    if sign_timestamp:
        payload["timestamp"] = timestamp
    if extra:
        payload.update(extra)
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = signing_key.sign(payload_bytes).signature
    sample = dict(payload)
    if not sign_timestamp:
        sample["timestamp"] = timestamp
    sample["signature"] = base64.b64encode(signature).decode("ascii")
    return sample


def test_fresh_timestamp_accepts():
    key = SigningKey.generate()
    sample = _signed_sample(key, timestamp=NOW - 60)

    assert verify_skill_sample(sample, sample_max_age_seconds=MAX_AGE, now=NOW)


def test_stale_timestamp_rejects():
    key = SigningKey.generate()
    sample = _signed_sample(key, timestamp=NOW - MAX_AGE - 1)

    assert not verify_skill_sample(sample, sample_max_age_seconds=MAX_AGE, now=NOW)


def test_missing_timestamp_rejects_and_warns(caplog):
    key = SigningKey.generate()
    sample = _signed_sample(key)
    del sample["timestamp"]

    with caplog.at_level(logging.WARNING, logger="knarr.core.crypto"):
        assert not verify_skill_sample(sample, sample_max_age_seconds=MAX_AGE, now=NOW)

    assert "CRYPTO_VERIFY_SAMPLE_MISSING_TIMESTAMP" in caplog.text


def test_timestamp_outside_signed_payload_rejects():
    key = SigningKey.generate()
    sample = _signed_sample(key, timestamp=NOW - 60, sign_timestamp=False)

    assert not verify_skill_sample(sample, sample_max_age_seconds=MAX_AGE, now=NOW)


def test_trusted_raters_unknown_rater_rejects():
    key = SigningKey.generate()
    other = SigningKey.generate()
    sample = _signed_sample(key, timestamp=NOW - 60)

    assert not verify_skill_sample(
        sample,
        trusted_raters={other.verify_key.encode().hex()},
        sample_max_age_seconds=MAX_AGE,
        now=NOW,
    )


def test_trusted_raters_none_accepts_any_valid_signer():
    key = SigningKey.generate()
    sample = _signed_sample(key, timestamp=NOW - 60)

    assert verify_skill_sample(
        sample,
        trusted_raters=None,
        sample_max_age_seconds=MAX_AGE,
        now=NOW,
    )


def test_merge_path_drops_stale_samples_before_persisting(tmp_path, caplog):
    key = SigningKey.generate()
    current = time.time()
    fresh = _signed_sample(key, timestamp=current - 1, extra={"test_output_hash": "sha256:" + "44" * 32})
    stale = _signed_sample(key, timestamp=current - 30, extra={"test_output_hash": "sha256:" + "55" * 32})

    node = DHTNode.__new__(DHTNode)
    node._config = {"policy": {"sample_max_age_seconds": 10}}

    with caplog.at_level(logging.WARNING, logger="knarr.dht.node"):
        filtered = node._merge_peer_samples([fresh, stale])

    storage = Storage(str(tmp_path / "node.db"))
    sheet = SkillSheet(
        name="demo-skill",
        version="1.0",
        description="demo",
        tags=["demo"],
        input_schema={},
        output_schema={},
        samples=filtered,
    )
    storage.upsert_skill("demo-skill", "11" * 32, sheet, ttl=600)

    stored = storage.get_all_skills()[0]["skill_sheet"]["samples"]
    assert stored == [fresh]
    assert "SAMPLE_INGEST_DROPPED dropped_count=1 retained_count=1" in caplog.text
