from __future__ import annotations

import builtins
from pathlib import Path

import pytest

from soarm_studio.config import load_config_mapping, load_session_config


def test_load_mock_config() -> None:
    config = load_session_config(Path("configs/sessions/mock.yaml"))

    assert config.name == "mock-dual-soarm"
    assert config.loop_hz == 30
    assert config.leader.mock is True
    assert config.follower.max_relative_target == 0.08
    assert list(config.cameras) == ["third_person", "wrist"]


def test_real_yaml_without_pyyaml_has_actionable_error(tmp_path, monkeypatch) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("name: real-yaml\n")
    original_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yaml":
            raise ModuleNotFoundError("No module named 'yaml'")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(RuntimeError, match="PyYAML is not installed"):
        load_config_mapping(path)
