import sys

import pytest

from knarr.cli.init import init_project
from knarr.cli.main import main


def test_init_project_minimal_profile_scaffolds_template_and_refuses_overwrite(tmp_path):
    target = tmp_path / "minimal-node"
    summary = init_project(str(target), port=9012, bootstrap="boot:99", profile="minimal")

    assert (target / "knarr.toml").exists()
    assert (target / "plugins.toml").exists()
    assert (target / "README.md").exists()
    assert "profile 'minimal'" in summary

    knarr_toml = (target / "knarr.toml").read_text(encoding="utf-8")
    plugins_toml = (target / "plugins.toml").read_text(encoding="utf-8")
    readme = (target / "README.md").read_text(encoding="utf-8")
    combined = "\n".join([knarr_toml, plugins_toml, readme]).lower()

    assert 'port = 9012' in knarr_toml
    assert 'bootstrap = ["boot:99"]' in knarr_toml
    assert "[plugins]" in plugins_toml
    for banned in ("thrall", "recipes", "personas", "ollama", "[identities."):
        assert banned not in combined

    with pytest.raises(SystemExit):
        init_project(str(target), profile="minimal")


def test_init_project_unknown_profile_lists_available_profiles(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        init_project(str(tmp_path / "bad-profile"), profile="not-real")

    captured = capsys.readouterr()
    assert exc.value.code == 1
    assert "Unknown init profile" in captured.err
    assert "minimal" in captured.err


def test_main_init_profile_flag_dispatches_to_init_project(monkeypatch, capsys):
    called = {}

    def _fake_init(directory, port, bootstrap, profile=""):
        called.update(
            directory=directory,
            port=port,
            bootstrap=bootstrap,
            profile=profile,
        )
        return "ok"

    monkeypatch.setattr("knarr.cli.main.init_project", _fake_init)
    monkeypatch.setattr(
        sys,
        "argv",
        ["knarr", "init", "demo-node", "--profile", "minimal", "--port", "9015", "--bootstrap", "boot:15"],
    )

    main()

    assert called == {
        "directory": "demo-node",
        "port": 9015,
        "bootstrap": "boot:15",
        "profile": "minimal",
    }
    assert capsys.readouterr().out.strip() == "ok"
