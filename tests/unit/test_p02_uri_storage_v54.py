"""P-02 tests: receipt_log URI column population and lookup."""

from knarr.dht.storage import Storage


def test_receipt_write_populates_uri():
    storage = Storage(":memory:")
    storage.write_receipt(
        receipt_id="r-1",
        document_type="execution_receipt",
        timestamp="2026-03-30T00:00:00Z",
        identity="a" * 64,
        counterparty="b" * 64,
        order_ref="order-1",
        proof_purpose="assertion",
        payload_json="{}",
        signature="sig",
    )
    receipt = storage.get_receipt("r-1")
    assert receipt is not None
    assert receipt["uri"] == f"knarr://{'a' * 64}/c/receipt/r-1"


def test_receipt_uri_like_query_returns_only_matching_identity():
    storage = Storage(":memory:")
    identity_a = "a" * 64
    identity_b = "b" * 64

    storage.write_receipt("ra-1", "execution_receipt", "2026-03-30T00:00:00Z", identity_a, None, None, "assertion", "{}", None)
    storage.write_receipt("ra-2", "execution_receipt", "2026-03-30T00:01:00Z", identity_a, None, None, "assertion", "{}", None)
    storage.write_receipt("rb-1", "execution_receipt", "2026-03-30T00:02:00Z", identity_b, None, None, "assertion", "{}", None)

    rows = storage._get_conn().execute(
        "SELECT receipt_id, uri FROM receipt_log WHERE uri LIKE ? ORDER BY receipt_id",
        (f"knarr://{identity_a}/c/receipt/%",),
    ).fetchall()

    assert rows == [
        ("ra-1", f"knarr://{identity_a}/c/receipt/ra-1"),
        ("ra-2", f"knarr://{identity_a}/c/receipt/ra-2"),
    ]
