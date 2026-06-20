from __future__ import annotations

import json

from soarm_studio.config import ArmEndpointConfig, DatasetConfig, SessionConfig
from soarm_studio.recording import record_lerobot_episodes


def test_record_lerobot_episodes_writes_quality_sidecars(tmp_path) -> None:
    root = tmp_path / "dataset"
    config = SessionConfig(
        name="test-recording",
        loop_hz=30,
        joints=["a", "b"],
        leader=ArmEndpointConfig(name="leader", mock=True, scripted=True),
        follower=ArmEndpointConfig(name="follower", mock=True, max_relative_target=0.1),
        cameras={},
        dataset=DatasetConfig(root=str(root), repo_id="local/test", fps=30),
    )

    result = record_lerobot_episodes(
        config,
        seconds=0.05,
        task="test task",
        overwrite=True,
    )

    assert result["dataset"] == str(root)
    assert (root / "meta" / "soarm_session.json").exists()
    assert (root / "meta" / "sync_quality.json").exists()
    quality = json.loads((root / "episodes" / "episode_000000" / "quality.json").read_text())
    assert quality["frames"] > 0
    assert quality["stale_camera_frames"] == 0
    assert quality["max_camera_age_ms"] == 0.0
