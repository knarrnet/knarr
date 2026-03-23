"""KAD-07: Key canonicalization before hashing."""
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'plugins', '00-kademlia'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from handler import default_key_function, _canonicalize_path


def test_same_key_regardless_of_case():
    """Uppercase and lowercase paths must produce the same DHT key."""
    k1 = default_key_function("s", "knowledge/translate")
    k2 = default_key_function("s", "Knowledge/Translate")
    k3 = default_key_function("s", "KNOWLEDGE/TRANSLATE")
    assert k1 == k2 == k3


def test_same_key_regardless_of_trailing_slash():
    """Paths with and without trailing slash must produce the same DHT key."""
    k1 = default_key_function("s", "knowledge/translate")
    k2 = default_key_function("s", "knowledge/translate/")
    k3 = default_key_function("s", "knowledge/translate//")
    assert k1 == k2 == k3


def test_same_key_regardless_of_surrounding_whitespace():
    """Paths with surrounding whitespace must produce the same DHT key."""
    k1 = default_key_function("s", "knowledge/translate")
    k2 = default_key_function("s", "  knowledge/translate  ")
    k3 = default_key_function("s", "\tknowledge/translate\n")
    assert k1 == k2 == k3


def test_combined_normalization():
    """Combined case + trailing slash + whitespace normalization."""
    k1 = default_key_function("s", "knowledge/translate")
    k2 = default_key_function("s", "  Knowledge/Translate/  ")
    assert k1 == k2


def test_different_paths_produce_different_keys():
    """Different canonical paths must still produce different keys."""
    k1 = default_key_function("s", "knowledge/translate")
    k2 = default_key_function("s", "knowledge/summarize")
    assert k1 != k2


def test_canonicalize_path_function():
    """_canonicalize_path utility behaves correctly."""
    assert _canonicalize_path("  FOO/BAR/  ") == "foo/bar"
    assert _canonicalize_path("foo/bar/") == "foo/bar"
    assert _canonicalize_path("foo/bar") == "foo/bar"
