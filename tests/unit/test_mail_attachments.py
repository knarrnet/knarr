"""Tests for knarr-mail sidecar attachment integration."""
import base64
import hashlib
import json
import os
import tempfile
import time

import pytest
from knarr.mail import handler


class MockStorage:
    def __init__(self):
        self._mail = []
        self._mail_seq = 0

    def count_mail(self):
        return len(self._mail)

    def count_mail_inbox(self):
        return len(self._mail)

    def store_mail(self, **kwargs):
        self._mail_seq += 1
        kwargs["rowid"] = self._mail_seq
        kwargs["status"] = "unread"
        self._mail.append(kwargs)

    def poll_mail(self, to_node, since_rowid=0, from_node=None,
                  session_id=None, msg_type=None, status=None, limit=50,
                  system=None):
        rows = []
        for m in self._mail:
            if m["to_node"] != to_node:
                continue
            if m["rowid"] <= since_rowid:
                continue
            rows.append(m)
            if len(rows) >= limit:
                break
        return rows, False

    def count_mail_unread(self, to_node, system=None):
        return sum(1 for m in self._mail if m["to_node"] == to_node and m["status"] == "unread")


class MockNodeInfo:
    node_id = "abc123" + "0" * 58  # 64 chars


class MockNode:
    def __init__(self, asset_dir=""):
        self.node_info = MockNodeInfo()
        self.storage = MockStorage()
        self._config = {"mail": {"accept_from": "all"}}
        self._asset_dir = asset_dir

    def store_asset(self, data: bytes) -> str:
        content_hash = hashlib.sha256(data).hexdigest()
        path = os.path.join(self._asset_dir, content_hash)
        with open(path, "wb") as f:
            f.write(data)
        return content_hash


@pytest.fixture(autouse=True)
def _reset_handler():
    """Reset module-level state between tests."""
    old_node = handler._node
    old_config = handler._mail_config
    yield
    handler._node = old_node
    handler._mail_config = old_config


def _send(body, caller="sender" + "0" * 58, attachments=None):
    input_data = {
        "action": "send",
        "body": body,
        "_caller_node_id": caller,
    }
    if attachments is not None:
        input_data["attachments"] = attachments
    return handler.handle(input_data)


def _poll(node):
    return handler.handle({
        "action": "poll",
        "_caller_node_id": node.node_info.node_id,
    })


# --- Send with URI attachment ---

def test_send_uri_attachment_stores_message():
    """URI-only attachments are validated and stored in the body."""
    node = MockNode()
    handler.set_node(node)
    h = "a" * 64
    result = _send({"text": "hello", "attachments": [f"knarr-asset://{h}"]})
    assert result["status"] == "delivered"

    stored_body = json.loads(node.storage._mail[0]["body"])
    assert len(stored_body["attachments"]) == 1
    assert stored_body["attachments"][0]["uri"] == f"knarr-asset://{h}"


def test_send_bare_hash_attachment():
    """Bare 64-char hex hash is accepted and normalized to URI."""
    node = MockNode()
    handler.set_node(node)
    h = "b" * 64
    result = _send({"text": "hello", "attachments": [h]})
    assert result["status"] == "delivered"

    stored_body = json.loads(node.storage._mail[0]["body"])
    assert stored_body["attachments"][0]["uri"] == f"knarr-asset://{h}"


def test_send_invalid_hash_rejected():
    """Invalid hash in attachment is rejected."""
    node = MockNode()
    handler.set_node(node)
    result = _send({"text": "hello", "attachments": ["not-a-hash"]})
    assert "error" in result
    assert "invalid hash" in result["message"]


def test_send_too_many_attachments():
    """Exceeding MAX_ATTACHMENTS is rejected."""
    node = MockNode()
    handler.set_node(node)
    h = "c" * 64
    result = _send({"text": "hi", "attachments": [h] * 11})
    assert result["error"] == "too_many_attachments"


# --- Send with inline base64 attachment ---

def test_send_inline_attachment_stores_on_sidecar():
    """Inline base64 data is decoded, stored on sidecar, replaced with URI."""
    with tempfile.TemporaryDirectory() as tmpdir:
        node = MockNode(asset_dir=tmpdir)
        handler.set_node(node)

        raw_data = b"hello world binary"
        b64 = base64.b64encode(raw_data).decode()
        expected_hash = hashlib.sha256(raw_data).hexdigest()

        result = _send({"text": "see attached", "attachments": [
            {"data": b64, "name": "test.bin"}
        ]})
        assert result["status"] == "delivered"

        stored_body = json.loads(node.storage._mail[0]["body"])
        att = stored_body["attachments"][0]
        assert att["uri"] == f"knarr-asset://{expected_hash}"
        assert att["name"] == "test.bin"
        assert att["size"] == len(raw_data)

        # Verify file actually exists on disk
        assert os.path.isfile(os.path.join(tmpdir, expected_hash))


def test_send_inline_no_sidecar_rejected():
    """Inline attachment without sidecar returns error."""
    node = MockNode(asset_dir="")
    handler.set_node(node)

    b64 = base64.b64encode(b"data").decode()
    result = _send({"text": "hi", "attachments": [{"data": b64}]})
    assert result["error"] == "sidecar_unavailable"


def test_send_inline_bad_base64_rejected():
    """Invalid base64 in inline attachment is rejected."""
    with tempfile.TemporaryDirectory() as tmpdir:
        node = MockNode(asset_dir=tmpdir)
        handler.set_node(node)

        result = _send({"text": "hi", "attachments": [{"data": "not!valid!base64!!!"}]})
        assert result["error"] == "invalid_attachment"
        assert "base64" in result["message"]


# --- Poll with attachment resolution ---

def test_poll_resolves_available_attachments():
    """Poll enriches attachments with available=True when asset exists locally."""
    with tempfile.TemporaryDirectory() as tmpdir:
        node = MockNode(asset_dir=tmpdir)
        handler.set_node(node)

        # Store an asset
        raw = b"poll test data"
        content_hash = node.store_asset(raw)

        # Send a message with that attachment URI
        _send({"text": "file attached", "attachments": [
            {"uri": f"knarr-asset://{content_hash}", "name": "data.bin"}
        ]})

        result = _poll(node)
        assert len(result["messages"]) == 1
        att = result["messages"][0]["body"]["attachments"][0]
        assert att["available"] is True
        assert att["size"] == len(raw)


def test_poll_marks_missing_attachments_unavailable():
    """Poll marks attachments as available=False when asset doesn't exist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        node = MockNode(asset_dir=tmpdir)
        handler.set_node(node)

        h = "d" * 64  # hash that doesn't exist on disk
        _send({"text": "missing file", "attachments": [f"knarr-asset://{h}"]})

        result = _poll(node)
        att = result["messages"][0]["body"]["attachments"][0]
        assert att["available"] is False


# --- Mailbox full rejects before storing attachments (V010-003) ---

def test_mailbox_full_rejects_before_attachment_store():
    """V010-003 sentinel: mailbox_full rejection must happen before attachments are stored."""
    with tempfile.TemporaryDirectory() as tmpdir:
        node = MockNode(asset_dir=tmpdir)
        node._config = {"mail": {"accept_from": "all", "max_messages": 0}}
        handler.set_node(node)

        b64 = base64.b64encode(b"should not be stored").decode()
        result = _send({"text": "hi", "attachments": [{"data": b64, "name": "orphan.bin"}]})
        assert result["status"] == "rejected"
        assert result["reason"] == "mailbox_full"

        # Verify no asset file was written
        assert os.listdir(tmpdir) == []


# --- BUG-002 sentinel: top-level attachments (documented API) ---

def test_top_level_attachments_stored(tmp_path):
    """BUG-002 sentinel: attachments at input_data top level (not inside body) must work."""
    node = MockNode(asset_dir=str(tmp_path))
    handler.set_node(node)

    raw_data = b"top-level attachment data"
    b64 = base64.b64encode(raw_data).decode()
    expected_hash = hashlib.sha256(raw_data).hexdigest()

    result = _send(
        body={"type": "text", "content": "see attached"},
        attachments=[{"data": b64, "name": "report.pdf"}],
    )
    assert result["status"] == "delivered"

    stored_body = json.loads(node.storage._mail[0]["body"])
    assert len(stored_body["attachments"]) == 1
    assert stored_body["attachments"][0]["uri"] == f"knarr-asset://{expected_hash}"
    assert stored_body["attachments"][0]["name"] == "report.pdf"


def test_top_level_uri_attachments_stored():
    """BUG-002: top-level URI attachments are stored correctly."""
    node = MockNode()
    handler.set_node(node)
    h = "e" * 64

    result = _send(
        body={"type": "text", "content": "file ref"},
        attachments=[f"knarr-asset://{h}"],
    )
    assert result["status"] == "delivered"

    stored_body = json.loads(node.storage._mail[0]["body"])
    assert stored_body["attachments"][0]["uri"] == f"knarr-asset://{h}"


# --- Send with no attachments (unchanged behavior) ---

def test_send_without_attachments_unchanged():
    """Messages without attachments work exactly as before."""
    node = MockNode()
    handler.set_node(node)
    result = _send({"text": "plain message"})
    assert result["status"] == "delivered"

    stored_body = json.loads(node.storage._mail[0]["body"])
    assert "attachments" not in stored_body
