from __future__ import annotations

from pathlib import Path

from soarm_studio.config import load_session_config


def test_load_mock_config() -> None:
    config = load_session_config(Path("configs/sessions/mock.yaml"))

    assert config.name == "mock-dual-soarm"
    assert config.loop_hz == 30
    assert config.leader.mock is True
    assert config.follower.max_relative_target == 0.08
    assert list(config.cameras) == ["third_person", "wrist"]
