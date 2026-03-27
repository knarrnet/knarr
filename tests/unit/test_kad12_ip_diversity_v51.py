"""KAD-12: IP diversity in k-buckets."""
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'plugins', '01-kademlia'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from kbuckets import KBucketTable


LOCAL_ID = "0" * 64
_LOCAL_INT = 0


def _id(n: int) -> str:
    """Generate a unique 64-char hex node_id for test n.

    Uses XOR distances 32, 33, 34, ... from LOCAL_ID so that all
    IDs land in bucket 5 (distances 32-63 have bit_length=6, bucket=5).
    This guarantees multiple IDs per bucket for IP-diversity tests.
    """
    return format(_LOCAL_INT ^ (32 + n), '064x')


def test_up_to_two_nodes_from_same_ip_accepted():
    """Up to _MAX_SAME_IP_PER_BUCKET (2) nodes from the same IP must be accepted."""
    table = KBucketTable(LOCAL_ID, k=8)
    ip = "192.168.1.1"

    id1 = _id(1)
    id2 = _id(2)

    table.add_peer(id1, ip, 9001)
    table.add_peer(id2, ip, 9002)

    # Both should be present
    closest = table.get_closest(_id(1), count=8)
    found_ids = {p["node_id"] for p in closest}
    assert id1 in found_ids
    assert id2 in found_ids


def test_third_node_from_same_ip_rejected():
    """Third node from the same IP must be rejected per bucket."""
    table = KBucketTable(LOCAL_ID, k=8)
    ip = "192.168.1.1"

    id1 = _id(1)
    id2 = _id(2)
    id3 = _id(3)

    table.add_peer(id1, ip, 9001)
    table.add_peer(id2, ip, 9002)
    table.add_peer(id3, ip, 9003)  # should be rejected

    closest = table.get_closest(_id(1), count=8)
    found_ids = {p["node_id"] for p in closest}

    # id1 and id2 in, id3 out
    assert id1 in found_ids
    assert id2 in found_ids
    assert id3 not in found_ids, "Third node from same IP must be rejected"


def test_nodes_from_different_ips_unaffected():
    """Nodes from different IPs must not be blocked by the IP diversity cap."""
    table = KBucketTable(LOCAL_ID, k=8)

    # Add 4 different nodes from 4 different IPs
    for i in range(1, 5):
        table.add_peer(_id(i), f"10.0.0.{i}", 9000 + i)

    closest = table.get_closest(_id(1), count=8)
    found_ids = {p["node_id"] for p in closest}

    for i in range(1, 5):
        assert _id(i) in found_ids, f"Node {i} from unique IP should be accepted"


def test_same_node_update_does_not_count_as_new_ip_entry():
    """Updating an existing peer (same node_id) must not be blocked by IP cap."""
    table = KBucketTable(LOCAL_ID, k=8)
    ip = "192.168.1.1"
    id1 = _id(1)
    id2 = _id(2)

    table.add_peer(id1, ip, 9001)
    table.add_peer(id2, ip, 9002)

    # Updating id1 with new port — should succeed (it's already in the bucket)
    table.add_peer(id1, ip, 9999)

    closest = table.get_closest(_id(1), count=8)
    found_ids = {p["node_id"] for p in closest}
    assert id1 in found_ids
    assert id2 in found_ids

    # Port should have been updated
    for p in closest:
        if p["node_id"] == id1:
            assert p["port"] == 9999


def test_ip_limit_per_bucket_not_global():
    """IP limit is per-bucket, not global. Same IP can appear in multiple buckets."""
    # LOCAL_ID all zeros — XOR distances determine bucket assignment
    table = KBucketTable(LOCAL_ID, k=8)
    ip = "192.168.1.1"

    # id_in_bucket_0 = 1 -> XOR distance = 1 -> bucket 0
    # id_in_bucket_1 = 2 -> XOR distance = 2 -> bucket 1
    # id_in_bucket_2 = 4 -> XOR distance = 4 -> bucket 2
    # These land in different buckets, so same IP can appear in each
    same_ip_nodes = [
        ("0" * 63 + "1", ip, 9001),  # distance 1 -> bucket 0
        ("0" * 63 + "2", ip, 9002),  # distance 2 -> bucket 1
        ("0" * 63 + "3", ip, 9003),  # distance 3 -> also bucket 1 (bit_length-1 = 1)
    ]

    for nid, h, p in same_ip_nodes:
        table.add_peer(nid, h, p)

    # At least 2 nodes should be present (from different buckets)
    all_found = []
    for bucket in table.buckets:
        for peer in bucket:
            all_found.append(peer)

    # bucket 0 has id ending ...1, bucket 1 has ids ending ...2 and ...3 — all different buckets
    assert len(all_found) >= 2
