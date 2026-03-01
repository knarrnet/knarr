"""Tests for ADR-007 URI taxonomy: validation, version matching, storage queries."""
import pytest
from knarr.core.validation import validate_skill_sheet, ValidationError, match_uri_version
from knarr.core.models import SkillSheet
from knarr.dht.storage import Storage


def _base_sheet(**overrides):
    """Helper to build a valid skill sheet dict with optional overrides."""
    d = {
        "name": "test-skill",
        "version": "1.0.0",
        "description": "Test skill",
        "tags": ["test"],
        "input_schema": {"text": "string"},
        "output_schema": {"text": "string"},
    }
    d.update(overrides)
    return d


class TestURIValidation:
    """URI format validation in validate_skill_sheet."""

    def test_valid_uri_three_slash(self):
        sheet = validate_skill_sheet(_base_sheet(uri="knarr:///compute/llm/gpt4@1.0"))
        assert sheet.uri == "knarr:///compute/llm/gpt4@1.0"

    def test_valid_uri_no_version(self):
        sheet = validate_skill_sheet(_base_sheet(uri="knarr:///tools/dev/echo"))
        assert sheet.uri == "knarr:///tools/dev/echo"

    def test_valid_uri_major_only(self):
        sheet = validate_skill_sheet(_base_sheet(uri="knarr:///tools/dev/echo@2"))
        assert sheet.uri == "knarr:///tools/dev/echo@2"

    def test_valid_uri_with_authority(self):
        sheet = validate_skill_sheet(_base_sheet(uri="knarr://abcdef0123456789/compute/llm/gpt4@1.0"))
        assert sheet.uri == "knarr://abcdef0123456789/compute/llm/gpt4@1.0"

    def test_valid_uri_with_underscores(self):
        """BUG-005: OASF subcategories use underscores (speech_synthesis, task_delegation)."""
        sheet = validate_skill_sheet(_base_sheet(uri="knarr:///audio/speech_synthesis/tts-lite@1.0"))
        assert sheet.uri == "knarr:///audio/speech_synthesis/tts-lite@1.0"

    def test_invalid_uri_bad_format(self):
        with pytest.raises(ValidationError, match="uri must match"):
            validate_skill_sheet(_base_sheet(uri="http://example.com/skill"))

    def test_invalid_uri_uppercase(self):
        with pytest.raises(ValidationError, match="uri must match"):
            validate_skill_sheet(_base_sheet(uri="knarr:///Compute/LLM/GPT4"))

    def test_invalid_uri_too_long(self):
        with pytest.raises(ValidationError, match="uri must not exceed"):
            validate_skill_sheet(_base_sheet(uri="knarr:///" + "a" * 250))

    def test_uri_none_allowed(self):
        """Skills without URI still pass validation."""
        sheet = validate_skill_sheet(_base_sheet())
        assert sheet.uri is None

    def test_uri_empty_string_skipped(self):
        """Empty string URI is treated as no URI."""
        sheet = validate_skill_sheet(_base_sheet(uri=""))
        assert sheet.uri == ""

    def test_uri_in_to_dict(self):
        sheet = validate_skill_sheet(_base_sheet(uri="knarr:///tools/dev/echo@1.0"))
        d = sheet.to_dict()
        assert d["uri"] == "knarr:///tools/dev/echo@1.0"

    def test_uri_absent_in_to_dict(self):
        sheet = validate_skill_sheet(_base_sheet())
        d = sheet.to_dict()
        assert "uri" not in d

    def test_uri_roundtrip(self):
        original = _base_sheet(uri="knarr:///compute/audio/tts@2.1")
        sheet = validate_skill_sheet(original)
        d = sheet.to_dict()
        restored = SkillSheet.from_dict(d)
        assert restored.uri == "knarr:///compute/audio/tts@2.1"


class TestInputSpecValidation:
    """input_spec (sidecar asset hash) validation."""

    def test_valid_input_spec(self):
        hash64 = "a" * 64
        sheet = validate_skill_sheet(_base_sheet(input_spec=hash64))
        assert sheet.input_spec == hash64

    def test_invalid_input_spec_too_short(self):
        with pytest.raises(ValidationError, match="input_spec must be"):
            validate_skill_sheet(_base_sheet(input_spec="abc123"))

    def test_invalid_input_spec_uppercase(self):
        with pytest.raises(ValidationError, match="input_spec must be"):
            validate_skill_sheet(_base_sheet(input_spec="A" * 64))

    def test_input_spec_none_allowed(self):
        sheet = validate_skill_sheet(_base_sheet())
        assert sheet.input_spec is None


class TestJurisdictionValidation:
    """Jurisdiction field validation [E-3]."""

    def test_valid_jurisdiction(self):
        sheet = validate_skill_sheet(_base_sheet(jurisdiction=["eu.se", "eu.no"]))
        assert sheet.jurisdiction == ["eu.se", "eu.no"]

    def test_valid_jurisdiction_country_only(self):
        sheet = validate_skill_sheet(_base_sheet(jurisdiction=["us"]))
        assert sheet.jurisdiction == ["us"]

    def test_invalid_jurisdiction_format(self):
        with pytest.raises(ValidationError, match="jurisdiction.*must be lowercase"):
            validate_skill_sheet(_base_sheet(jurisdiction=["EU.SE"]))

    def test_invalid_jurisdiction_too_many(self):
        with pytest.raises(ValidationError, match="jurisdiction must be a list of max 10"):
            validate_skill_sheet(_base_sheet(jurisdiction=["eu"] * 11))

    def test_jurisdiction_bare_string_auto_wrapped(self):
        """BUG-005: bare string auto-wraps to single-element list."""
        sheet = validate_skill_sheet(_base_sheet(jurisdiction="eu.se"))
        assert sheet.jurisdiction == ["eu.se"]

    def test_jurisdiction_none_allowed(self):
        sheet = validate_skill_sheet(_base_sheet())
        assert sheet.jurisdiction is None


class TestVersionMatching:
    """match_uri_version() semver-compatible URI matching."""

    def test_exact_match(self):
        assert match_uri_version("knarr:///x@1.2", "knarr:///x@1.2") is True

    def test_exact_no_match(self):
        assert match_uri_version("knarr:///x@1.2", "knarr:///x@1.3") is False

    def test_major_matches_any_minor(self):
        assert match_uri_version("knarr:///x@1", "knarr:///x@1.0") is True
        assert match_uri_version("knarr:///x@1", "knarr:///x@1.5") is True
        assert match_uri_version("knarr:///x@1", "knarr:///x@1.99") is True

    def test_major_no_match(self):
        assert match_uri_version("knarr:///x@1", "knarr:///x@2.0") is False

    def test_no_version_matches_all(self):
        assert match_uri_version("knarr:///x", "knarr:///x@1.0") is True
        assert match_uri_version("knarr:///x", "knarr:///x@2.5") is True

    def test_different_base_no_match(self):
        assert match_uri_version("knarr:///x@1.0", "knarr:///y@1.0") is False

    def test_no_version_both(self):
        assert match_uri_version("knarr:///x", "knarr:///x") is True


class TestStorageURIQueries:
    """Storage-level URI queries."""

    def _setup_storage(self):
        from knarr.core.models import NodeInfo
        s = Storage(":memory:")
        # Add a peer
        peer = NodeInfo("node1", "127.0.0.1", 9000)
        s.upsert_peer(peer)
        return s, peer

    def test_uri_stored_on_upsert(self):
        s, peer = self._setup_storage()
        sheet = SkillSheet(
            name="test", version="1.0.0", description="Test",
            tags=["test"], input_schema={}, output_schema={},
            uri="knarr:///tools/dev/test@1.0"
        )
        s.upsert_skill("test", "node1", sheet, ttl=600)
        # Check URI was stored
        conn = s._get_conn()
        cursor = conn.execute("SELECT uri FROM skills WHERE skill_key = 'test'")
        row = cursor.fetchone()
        assert row[0] == "knarr:///tools/dev/test@1.0"

    def test_query_by_uri_exact(self):
        s, peer = self._setup_storage()
        sheet = SkillSheet(
            name="test", version="1.0.0", description="Test",
            tags=["test"], input_schema={}, output_schema={},
            uri="knarr:///tools/dev/test@1.0"
        )
        s.upsert_skill("test", "node1", sheet, ttl=600)
        results = s.query_skills_by_uri("knarr:///tools/dev/test@1.0")
        assert len(results) == 1
        assert results[0]["node_id"] == "node1"

    def test_query_by_uri_no_match(self):
        s, peer = self._setup_storage()
        sheet = SkillSheet(
            name="test", version="1.0.0", description="Test",
            tags=["test"], input_schema={}, output_schema={},
            uri="knarr:///tools/dev/test@1.0"
        )
        s.upsert_skill("test", "node1", sheet, ttl=600)
        results = s.query_skills_by_uri("knarr:///tools/dev/other@1.0")
        assert len(results) == 0

    def test_query_by_uri_prefix(self):
        s, peer = self._setup_storage()
        for name, uri in [("a", "knarr:///audio/tts@1.0"), ("b", "knarr:///audio/stt@1.0"),
                           ("c", "knarr:///compute/llm@1.0")]:
            sheet = SkillSheet(
                name=name, version="1.0.0", description="Test",
                tags=["test"], input_schema={}, output_schema={}, uri=uri
            )
            s.upsert_skill(name, "node1", sheet, ttl=600)
        results = s.query_skills_by_uri_prefix("knarr:///audio/")
        assert len(results) == 2

    def test_wallet_column(self):
        s, peer = self._setup_storage()
        s.update_peer_wallet("node1", "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU")
        peers = s.get_peers_full()
        assert peers[0]["wallet"] == "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
