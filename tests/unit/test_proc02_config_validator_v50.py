from knarr.cli.config import load_config


def test_invalid_tab_reminder_auto_netting_type_warns(tmp_path, capsys):
    config_path = tmp_path / "knarr.toml"
    config_path.write_text('[settlement]\ntab_reminder_auto_netting = "yes"\n', encoding="utf-8")

    load_config(config_path)

    err = capsys.readouterr().err
    assert "tab_reminder_auto_netting" in err
    assert "expected bool" in err


def test_invalid_tab_reminder_threshold_type_warns(tmp_path, capsys):
    config_path = tmp_path / "knarr.toml"
    config_path.write_text('[settlement]\ntab_reminder_threshold = "high"\n', encoding="utf-8")

    load_config(config_path)

    err = capsys.readouterr().err
    assert "tab_reminder_threshold" in err
    assert "expected float" in err
