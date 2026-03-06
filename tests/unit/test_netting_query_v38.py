"""Tests for A5.4: netting query — 3 scopes, raw/computed, empty results."""
import unittest


def _make_storage(entries):
    """Build a minimal storage mock."""
    import types
    s = types.SimpleNamespace()
    s.get_all_ledger_entries = lambda: entries
    return s


def _entry(pk, balance=-5.0, prepaid=0.0, hard_limit=-10.0, soft_limit=-5.0, credit_limit=3.0):
    return {
        "peer_public_key": pk,
        "balance": balance,
        "prepaid": prepaid,
        "pub_tab": 0.0,
        "soft_limit": soft_limit,
        "hard_limit": hard_limit,
        "credit_limit": credit_limit,
        "tasks_provided": 5,
        "tasks_consumed": 3,
        "trust": 0.3,
    }


class TestNettingQueryRawBook(unittest.TestCase):
    from knarr.commerce.netting_query import query

    def test_book_scope_returns_all(self):
        from knarr.commerce.netting_query import query
        storage = _make_storage([_entry("aaa"), _entry("bbb")])
        results = query(storage, "book", None, raw=True, config={})
        self.assertEqual(len(results), 2)

    def test_raw_contains_balance(self):
        from knarr.commerce.netting_query import query
        storage = _make_storage([_entry("aaa", balance=-7.0)])
        results = query(storage, "book", None, raw=True, config={})
        self.assertEqual(results[0]["peer_key"], "aaa")
        self.assertAlmostEqual(results[0]["balance"], -7.0)

    def test_raw_utilization_computed(self):
        from knarr.commerce.netting_query import query
        storage = _make_storage([_entry("aaa", balance=-7.0, hard_limit=-10.0)])
        results = query(storage, "book", None, raw=True, config={})
        self.assertAlmostEqual(results[0]["utilization_pct"], 70.0, places=1)

    def test_empty_book(self):
        from knarr.commerce.netting_query import query
        storage = _make_storage([])
        results = query(storage, "book", None, raw=True, config={})
        self.assertEqual(results, [])


class TestNettingQueryRawNode(unittest.TestCase):

    def test_node_scope_filters_by_target(self):
        from knarr.commerce.netting_query import query
        storage = _make_storage([_entry("aaa"), _entry("bbb")])
        results = query(storage, "node", "aaa", raw=True, config={})
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["peer_key"], "aaa")

    def test_node_scope_no_match_returns_empty(self):
        from knarr.commerce.netting_query import query
        storage = _make_storage([_entry("aaa")])
        results = query(storage, "node", "zzz", raw=True, config={})
        self.assertEqual(results, [])

    def test_node_scope_missing_target_returns_empty(self):
        from knarr.commerce.netting_query import query
        storage = _make_storage([_entry("aaa")])
        results = query(storage, "node", None, raw=True, config={})
        self.assertEqual(results, [])


class TestNettingQueryRawFelag(unittest.TestCase):

    def test_felag_scope_filters_group(self):
        from knarr.commerce.netting_query import query
        storage = _make_storage([_entry("aaa"), _entry("bbb"), _entry("ccc")])
        config = {"groups": {"mygroup": {"members": ["aaa", "ccc"]}}}
        results = query(storage, "felag", "mygroup", raw=True, config=config)
        self.assertEqual(len(results), 2)
        pks = {r["peer_key"] for r in results}
        self.assertEqual(pks, {"aaa", "ccc"})

    def test_felag_unknown_group_returns_empty(self):
        from knarr.commerce.netting_query import query
        storage = _make_storage([_entry("aaa")])
        results = query(storage, "felag", "nonexistent", raw=True, config={})
        self.assertEqual(results, [])


class TestNettingQueryComputed(unittest.TestCase):

    def test_computed_returns_action_field(self):
        from knarr.commerce.netting_query import query
        storage = _make_storage([_entry("aaa", balance=-9.0, hard_limit=-10.0, credit_limit=3.0)])
        config = {"economy": {"settlement": {"soft_threshold": 0.8, "soft_target": 0.5, "min_settlement_amount": 0.1}}}
        results = query(storage, "book", None, raw=False, config=config)
        self.assertEqual(len(results), 1)
        self.assertIn("action", results[0])
        self.assertIn("amount", results[0])

    def test_below_threshold_is_skip(self):
        from knarr.commerce.netting_query import query
        storage = _make_storage([_entry("aaa", balance=-1.0, hard_limit=-10.0, credit_limit=3.0)])
        config = {"economy": {"settlement": {"soft_threshold": 0.8}}}
        results = query(storage, "book", None, raw=False, config=config)
        self.assertEqual(results[0]["action"], "skip")

    def test_above_threshold_is_settle(self):
        from knarr.commerce.netting_query import query
        storage = _make_storage([_entry("aaa", balance=-9.0, hard_limit=-10.0, credit_limit=3.0)])
        config = {"economy": {"settlement": {"soft_threshold": 0.8, "soft_target": 0.5, "min_settlement_amount": 0.1}}}
        results = query(storage, "book", None, raw=False, config=config)
        self.assertEqual(results[0]["action"], "settle")


if __name__ == "__main__":
    unittest.main()
