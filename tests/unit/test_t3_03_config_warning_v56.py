import logging

from knarr.cli.config import load_config


def test_load_config_warns_on_unknown_top_level_section(tmp_path, caplog):
    config_path = tmp_path / "knarr.toml"
    config_path.write_text(
        """
        [transport]
        tls = false

        [node]
        port = 9001
        """,
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="knarr.cli.config"):
        cfg = load_config(config_path)

    messages = " ".join(record.getMessage() for record in caplog.records)
    assert cfg["node"]["port"] == 9001
    assert "CONFIG_UNKNOWN_SECTION" in messages
    assert "transport" in messages
    assert "node" in messages


def test_load_config_does_not_warn_for_known_top_level_section(tmp_path, caplog):
    config_path = tmp_path / "knarr.toml"
    config_path.write_text(
        """
        [node]
        port = 9002

        [bridges]
        local = 5
        """,
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="knarr.cli.config"):
        load_config(config_path)

    section_warnings = [
        r for r in caplog.records
        if "CONFIG_UNKNOWN_SECTION" in r.getMessage()
    ]
    assert section_warnings == []
