"""E-04: Identity config parser.

Tests:
1. No [identities] section returns empty list.
2. Single identity is parsed correctly (name, data_dir, skills).
3. Multiple identities are all returned.
4. data_dir defaults to "identity-{name}" when not specified.
5. skills defaults to empty list when not specified.
6. Optional fields (vault_key, mail, debug) are passed through.
7. Non-dict identity values log warning and are skipped.
8. parse_identity_configs is importable and callable.
"""
import pytest


class TestParseIdentityConfigs:
    def test_no_identities_returns_empty_list(self):
        """No [identities] section returns empty list."""
        from knarr.cli.config import parse_identity_configs
        config = {"node": {"port": 9000}}
        result = parse_identity_configs(config)
        assert result == []

    def test_empty_identities_returns_empty_list(self):
        """Empty [identities] section returns empty list."""
        from knarr.cli.config import parse_identity_configs
        config = {"identities": {}}
        result = parse_identity_configs(config)
        assert result == []

    def test_single_identity_parsed(self):
        """Single identity is parsed with name, data_dir, skills."""
        from knarr.cli.config import parse_identity_configs
        config = {
            "identities": {
                "alice": {
                    "data_dir": "identity-alice",
                    "skills": ["llm/chat@1.0"],
                }
            }
        }
        result = parse_identity_configs(config)
        assert len(result) == 1
        entry = result[0]
        assert entry["name"] == "alice"
        assert entry["data_dir"] == "identity-alice"
        assert entry["skills"] == ["llm/chat@1.0"]

    def test_multiple_identities_all_returned(self):
        """All configured identities are returned."""
        from knarr.cli.config import parse_identity_configs
        config = {
            "identities": {
                "alice": {"data_dir": "identity-alice", "skills": []},
                "bob": {"data_dir": "identity-bob", "skills": ["tools/dev/echo@1.0"]},
            }
        }
        result = parse_identity_configs(config)
        assert len(result) == 2
        names = {e["name"] for e in result}
        assert names == {"alice", "bob"}

    def test_data_dir_defaults_to_identity_name(self):
        """data_dir defaults to 'identity-{name}' when not specified."""
        from knarr.cli.config import parse_identity_configs
        config = {
            "identities": {
                "charlie": {"skills": []},
            }
        }
        result = parse_identity_configs(config)
        assert result[0]["data_dir"] == "identity-charlie"

    def test_skills_defaults_to_empty_list(self):
        """skills defaults to empty list when not specified."""
        from knarr.cli.config import parse_identity_configs
        config = {
            "identities": {
                "dave": {"data_dir": "identity-dave"},
            }
        }
        result = parse_identity_configs(config)
        assert result[0]["skills"] == []

    def test_optional_vault_key_passed_through(self):
        """vault_key is included in entry when present."""
        from knarr.cli.config import parse_identity_configs
        config = {
            "identities": {
                "eve": {
                    "data_dir": "identity-eve",
                    "vault_key": "my-vault-secret",
                }
            }
        }
        result = parse_identity_configs(config)
        assert result[0].get("vault_key") == "my-vault-secret"

    def test_optional_debug_passed_through(self):
        """debug flag is included in entry when present."""
        from knarr.cli.config import parse_identity_configs
        config = {
            "identities": {
                "frank": {"data_dir": "identity-frank", "debug": True},
            }
        }
        result = parse_identity_configs(config)
        assert result[0].get("debug") is True

    def test_non_dict_identity_skipped(self):
        """Non-dict identity values are skipped (warns but doesn't crash)."""
        from knarr.cli.config import parse_identity_configs
        config = {
            "identities": {
                "good": {"data_dir": "identity-good"},
                "bad": "not_a_table",
            }
        }
        result = parse_identity_configs(config)
        names = [e["name"] for e in result]
        assert "good" in names
        assert "bad" not in names

    def test_function_is_importable(self):
        """parse_identity_configs is importable and callable."""
        from knarr.cli.config import parse_identity_configs
        assert callable(parse_identity_configs)
