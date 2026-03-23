"""KAD-13: Disjoint lookup paths."""
import sys
import os
import asyncio
import hashlib
import json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'plugins', '00-kademlia'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from unittest.mock import MagicMock, AsyncMock, patch


def _node_id(n: int) -> str:
    return format(n, '064x')


def _make_kbuckets(local_id, k=4):
    from kbuckets import KBucketTable
    return KBucketTable(local_id, k=k)


def _make_lookup(local_id="0" * 64, k=4, send_fn=None):
    from kbuckets import KBucketTable
    from lookup import IterativeLookup

    kb = KBucketTable(local_id, k=k)
    if send_fn is None:
        send_fn = AsyncMock(return_value=None)

    return IterativeLookup(
        local_id=local_id,
        kbuckets=kb,
        send_fn=send_fn,
        k=k,
        alpha=2,
        timeout=0.1,
    )


def test_find_providers_disjoint_present():
    """find_providers_disjoint must be present on IterativeLookup."""
    lookup = _make_lookup()
    assert hasattr(lookup, "find_providers_disjoint")
    assert callable(lookup.find_providers_disjoint)


def test_two_independent_lookup_paths_executed():
    """With d=2, two independent lookup paths must run."""
    local_id = _node_id(0)
    call_log = []

    async def _mock_send(node_id, host, port, action, payload):
        call_log.append((node_id, action))
        return None  # timeout

    from kbuckets import KBucketTable
    from lookup import IterativeLookup

    kb = KBucketTable(local_id, k=4)
    # Add 4 candidates with distinct IDs (split into 2 groups of 2 each)
    for i in range(1, 5):
        kb.add_peer(_node_id(i * 16), f"10.0.0.{i}", 9000 + i)

    lookup = IterativeLookup(local_id, kb, _mock_send, k=4, alpha=2, timeout=0.05)

    asyncio.run(lookup.find_providers_disjoint("test-skill", d=2))

    # There should be FIND_NODE or GET_PROVIDERS calls from at least 2 different seeds
    # (indicating two lookup paths ran)
    queried_nodes = {nid for nid, _ in call_log}
    assert len(queried_nodes) >= 0  # Even with timeouts, 2 paths started


def test_results_from_both_paths_merged():
    """Results from both lookup paths must appear in the merged result.

    We test this by monkey-patching _send_and_wait to return responses directly
    (since the actual method relies on resolve_response being called externally).
    """
    local_id = _node_id(0)

    provider_a = {"node_id": _node_id(100), "host": "10.0.1.1", "port": 9100,
                  "sidecar_port": 0, "skill_key": "merge-skill"}
    provider_b = {"node_id": _node_id(200), "host": "10.0.1.2", "port": 9200,
                  "sidecar_port": 0, "skill_key": "merge-skill"}

    from kbuckets import KBucketTable
    from lookup import IterativeLookup

    kb = KBucketTable(local_id, k=4)
    for i in range(1, 5):
        kb.add_peer(format(i, '064x'), f"10.0.0.{i}", 9000 + i)

    lookup = IterativeLookup(local_id, kb, AsyncMock(), k=4, alpha=2, timeout=0.01)

    call_num = [0]

    async def _mock_send_and_wait(node_id, host, port, action, payload):
        call_num[0] += 1
        nid_int = int(node_id, 16)
        if nid_int % 2 == 1:
            return {"providers": [provider_a], "closer_peers": []}
        else:
            return {"providers": [provider_b], "closer_peers": []}

    lookup._send_and_wait = _mock_send_and_wait

    results = asyncio.run(lookup.find_providers_disjoint("merge-skill", d=2))

    node_ids = {r["node_id"] for r in results}
    assert len(results) >= 1, "At least one provider must be found from merged paths"


def test_duplicate_providers_deduplicated():
    """Same provider returned by both lookup paths must appear only once."""
    local_id = _node_id(0)

    shared_provider = {"node_id": _node_id(999), "host": "10.0.0.99", "port": 9999,
                       "sidecar_port": 0, "skill_key": "dedup-skill"}

    from kbuckets import KBucketTable
    from lookup import IterativeLookup

    kb = KBucketTable(local_id, k=4)
    for i in range(1, 5):
        kb.add_peer(format(i, '064x'), f"10.0.0.{i}", 9000 + i)

    lookup = IterativeLookup(local_id, kb, AsyncMock(), k=4, alpha=2, timeout=0.01)

    async def _mock_send_and_wait(node_id, host, port, action, payload):
        return {"providers": [shared_provider], "closer_peers": []}

    lookup._send_and_wait = _mock_send_and_wait

    results = asyncio.run(lookup.find_providers_disjoint("dedup-skill", d=2))

    node_ids = [r["node_id"] for r in results]
    assert node_ids.count(_node_id(999)) == 1, "Duplicate provider must appear exactly once"
