"""E-02 tests: parse_knarr_uri."""

from knarr.core.uri import parse_knarr_uri


def test_parse_knarr_uri_tier_anonymous():
    """Empty authority URI parses for anonymous/discovery routing."""
    authority, selector, resource = parse_knarr_uri("knarr:///s/llm/chat@1.0")
    assert authority == ""
    assert selector == "s"
    assert resource == "llm/chat@1.0"


def test_parse_knarr_uri_tier_identity_scoped():
    """Identity authority is returned unchanged for directed routing."""
    authority_id = "a" * 64
    authority, selector, resource = parse_knarr_uri(f"knarr://{authority_id}/c/receipt/r-123")
    assert authority == authority_id
    assert selector == "c"
    assert resource == "receipt/r-123"


def test_parse_knarr_uri_tier_nested_resource():
    """Nested resource paths remain intact."""
    authority, selector, resource = parse_knarr_uri("knarr:///p/catalog/offers/current")
    assert authority == ""
    assert selector == "p"
    assert resource == "catalog/offers/current"


def test_parse_knarr_uri_all_selectors():
    """Every selector from the brief is recognized."""
    for selector in ("s", "p", "c", "m", "k", "g", "o"):
        authority, parsed_selector, resource = parse_knarr_uri(f"knarr:///{selector}/resource")
        assert authority == ""
        assert parsed_selector == selector
        assert resource == "resource"


def test_parse_knarr_uri_empty_and_invalid_return_empty_tuple():
    """Empty or malformed values fail closed."""
    assert parse_knarr_uri("") == ("", "", "")
    assert parse_knarr_uri(None) == ("", "", "")
    assert parse_knarr_uri("http://example.com") == ("", "", "")
    assert parse_knarr_uri("knarr://") == ("", "", "")
    assert parse_knarr_uri("knarr:///") == ("", "", "")
    assert parse_knarr_uri("knarr:///x") == ("", "", "")
    assert parse_knarr_uri("knarr:///z/resource") == ("", "", "")
    assert parse_knarr_uri("knarr:///p/") == ("", "p", "")  # TP-8: empty resource is valid
