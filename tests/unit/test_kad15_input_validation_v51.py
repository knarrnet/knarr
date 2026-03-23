"""KAD-15: Input validation on get_closest."""
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'plugins', '00-kademlia'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from kbuckets import KBucketTable


LOCAL_ID = "a" * 64


def _make_table():
    return KBucketTable(LOCAL_ID, k=4)


def test_short_hex_returns_empty_no_crash():
    """Short hex string must return empty list, not raise."""
    table = _make_table()
    result = table.get_closest("deadbeef")  # 8 chars, not 64
    assert result == []


def test_non_hex_returns_empty_no_crash():
    """Non-hex string must return empty list, not raise."""
    table = _make_table()
    result = table.get_closest("not_hex_at_all_xxxx_yyyy_zzzz_123")
    assert result == []


def test_empty_string_returns_empty():
    """Empty string must return empty list."""
    table = _make_table()
    assert table.get_closest("") == []


def test_none_does_not_raise():
    """None input must return empty list, not raise."""
    table = _make_table()
    result = table.get_closest(None)  # type: ignore
    assert result == []


def test_valid_64_char_hex_works_normally():
    """Valid 64-character hex ID must return peers normally."""
    table = _make_table()
    peer_id = "b" * 64
    table.add_peer(peer_id, "10.0.0.1", 9001)
    result = table.get_closest(peer_id, count=4)
    assert len(result) == 1
    assert result[0]["node_id"] == peer_id


def test_63_char_hex_returns_empty():
    """63-character hex (1 short) must return empty list."""
    table = _make_table()
    short_id = "a" * 63
    assert table.get_closest(short_id) == []


def test_65_char_hex_returns_empty():
    """65-character hex (1 too many) must return empty list."""
    table = _make_table()
    long_id = "a" * 65
    assert table.get_closest(long_id) == []
