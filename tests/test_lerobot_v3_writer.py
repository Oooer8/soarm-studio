from __future__ import annotations

import json

import pytest

from soarm_studio.datasets.lerobot_v3 import LeRobotV3Writer
from soarm_studio.datasets.lerobot_v3.deps import require_pyarrow
from soarm_studio.datasets.tools import inspect_dataset, validate_dataset
from soarm_studio.types import CameraFrame


def test_writer_creates_minimal_lerobot_v3_dataset(tmp_path) -> None:
    root = tmp_path / "dataset"
    with LeRobotV3Writer(
        root=root,
        repo_id="local/test",
        fps=10,
        joint_names=["a", "b"],
        cameras={},
    ) as writer:
        episode = writer.start_episode("test task")
        episode.add_frame(state={"a": 0.0, "b": 0.1}, action={"a": 0.2, "b": 0.3})
        episode.add_frame(state={"a": 0.1, "b": 0.2}, action={"a": 0.3, "b": 0.4})
        episode.save()

    summary = inspect_dataset(root)
    assert summary["total_episodes"] == 1
    assert summary["total_frames"] == 2
    assert validate_dataset(root) == []


def test_writer_aggregates_stats_across_episodes(tmp_path) -> None:
    root = tmp_path / "dataset"
    with LeRobotV3Writer(
        root=root,
        repo_id="local/test",
        fps=10,
        joint_names=["a", "b"],
        cameras={},
    ) as writer:
        episode = writer.start_episode("task one")
        episode.add_frame(state={"a": 0.0, "b": 2.0}, action={"a": 1.0, "b": 1.0})
        episode.save()

        episode = writer.start_episode("task two")
        episode.add_frame(state={"a": 2.0, "b": 4.0}, action={"a": 3.0, "b": 5.0})
        episode.save()

    stats = json.loads((root / "meta" / "stats.json").read_text())

    assert stats["observation.state"]["mean"] == [1.0, 3.0]
    assert stats["observation.state"]["std"] == [1.0, 1.0]
    assert stats["observation.state"]["min"] == [0.0, 2.0]
    assert stats["observation.state"]["max"] == [2.0, 4.0]
    assert stats["action"]["mean"] == [2.0, 3.0]
    assert stats["action"]["std"] == [1.0, 2.0]


def test_writer_includes_video_stats(tmp_path) -> None:
    pytest.importorskip("cv2")
    pytest.importorskip("numpy")

    root = tmp_path / "dataset"
    with LeRobotV3Writer(
        root=root,
        repo_id="local/test",
        fps=10,
        joint_names=["a", "b"],
        cameras={"wrist": (2, 2, 3)},
    ) as writer:
        episode = writer.start_episode("test task")
        episode.add_frame(
            state={"a": 0.0, "b": 0.0},
            action={"a": 0.0, "b": 0.0},
            images={
                "wrist": CameraFrame(
                    name="wrist",
                    width=2,
                    height=2,
                    rgb=bytes([0, 127, 255]) * 4,
                ),
            },
        )
        episode.add_frame(
            state={"a": 1.0, "b": 1.0},
            action={"a": 1.0, "b": 1.0},
            images={
                "wrist": CameraFrame(
                    name="wrist",
                    width=2,
                    height=2,
                    rgb=bytes([255, 127, 0]) * 4,
                ),
            },
        )
        episode.save()

    stats = json.loads((root / "meta" / "stats.json").read_text())
    camera_stats = stats["observation.images.wrist"]

    assert camera_stats["mean"] == pytest.approx([0.5, 127.0 / 255.0, 0.5])
    assert camera_stats["std"] == pytest.approx([0.5, 0.0, 0.5])
    assert camera_stats["min"] == pytest.approx([0.0, 127.0 / 255.0, 0.0])
    assert camera_stats["max"] == pytest.approx([1.0, 127.0 / 255.0, 1.0])

    _, pq = require_pyarrow(purpose="test")
    episodes = pq.read_table(root / "meta" / "episodes" / "chunk-000" / "file-000.parquet")
    assert "stats/observation.images.wrist/mean" in episodes.column_names
    assert validate_dataset(root) == []


def test_writer_accepts_explicit_frame_timestamps(tmp_path) -> None:
    root = tmp_path / "dataset"
    with LeRobotV3Writer(
        root=root,
        repo_id="local/test",
        fps=10,
        joint_names=["a", "b"],
        cameras={},
    ) as writer:
        episode = writer.start_episode("test task")
        episode.add_frame(
            state={"a": 0.0, "b": 0.1},
            action={"a": 0.2, "b": 0.3},
            timestamp=1.25,
        )
        episode.save()

    _, pq = require_pyarrow(purpose="test")
    table = pq.read_table(root / "data" / "chunk-000" / "file-000.parquet")
    assert table.column("timestamp").to_pylist() == [1.25]


def test_writer_creates_sidecar_metadata(tmp_path) -> None:
    root = tmp_path / "dataset"
    with LeRobotV3Writer(
        root=root,
        repo_id="local/test",
        fps=10,
        joint_names=["a", "b"],
        cameras={},
    ) as writer:
        writer.write_session_metadata({"session": "test"})
        writer.write_sync_quality({"summary": {"frames": 0}})
        writer.write_episode_quality(0, {"frames": 0})
        writer.write_episode_camera_timing(0, {"cameras": {}})

    assert json.loads((root / "meta" / "soarm_session.json").read_text())["session"] == "test"
    assert (root / "meta" / "sync_quality.json").exists()
    assert (root / "episodes" / "episode_000000" / "quality.json").exists()
    assert (root / "episodes" / "episode_000000" / "camera_timing.json").exists()
