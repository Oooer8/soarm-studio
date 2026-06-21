from __future__ import annotations

import json

from soarm_studio.config import ArmEndpointConfig, DatasetConfig, SessionConfig
from soarm_studio.recording.quality import RecordingQualityTracker
from soarm_studio.recording.session import _write_episode_samples
from soarm_studio.recording import record_lerobot_episodes
from soarm_studio.types import CameraFrame, ControlSample, JointSample


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
    assert not (root / "episodes" / "episode_000000" / "camera_timing.json").exists()
    quality = json.loads((root / "episodes" / "episode_000000" / "quality.json").read_text())
    assert quality["frames"] > 0
    assert quality["stale_camera_frames"] == 0
    assert quality["max_camera_age_ms"] == 0.0


def test_record_lerobot_episodes_writes_camera_timing_only_in_debug(tmp_path) -> None:
    root = tmp_path / "dataset"
    config = SessionConfig(
        name="test-recording-debug",
        loop_hz=30,
        joints=["a", "b"],
        leader=ArmEndpointConfig(name="leader", mock=True, scripted=True),
        follower=ArmEndpointConfig(name="follower", mock=True, max_relative_target=0.1),
        cameras={},
        dataset=DatasetConfig(root=str(root), repo_id="local/test", fps=30),
    )

    record_lerobot_episodes(
        config,
        seconds=0.05,
        task="test task",
        overwrite=True,
        debug=True,
    )

    timing = json.loads(
        (root / "episodes" / "episode_000000" / "camera_timing.json").read_text()
    )
    assert timing["sample_count"] > 0
    assert timing["cameras"] == {}


def test_write_episode_samples_matches_nearest_camera_history_frame() -> None:
    captured: list[dict] = []

    class FakeEpisode:
        def add_frame(self, **kwargs) -> None:
            captured.append(kwargs)

    samples = [
        _sample(frame_index=0, monotonic_time_ns=1_000_000_000),
        _sample(frame_index=1, monotonic_time_ns=1_100_000_000),
    ]
    early = _frame(monotonic_time_ns=960_000_000, pixel=b"\x01\x00\x00")
    late = _frame(monotonic_time_ns=1_090_000_000, pixel=b"\x02\x00\x00")
    quality = RecordingQualityTracker()

    timing = _write_episode_samples(
        FakeEpisode(),
        samples,
        quality,
        {"wrist": [late, early]},
    )

    assert captured[0]["images"]["wrist"] is early
    assert captured[1]["images"]["wrist"] is late
    assert [item["timestamp"] for item in captured] == [0.0, 0.1]
    assert quality.to_dict()["max_camera_age_ms"] == 40.0
    assert timing["cameras"]["wrist"]["raw_intervals_ms"] == [130.0]
    assert timing["cameras"]["wrist"]["matched_samples"][0]["camera_frame_index"] == 0
    assert timing["cameras"]["wrist"]["matched_samples"][1]["camera_frame_index"] == 1


def _sample(*, frame_index: int, monotonic_time_ns: int) -> ControlSample:
    joints = {"a": float(frame_index)}
    return ControlSample(
        frame_index=frame_index,
        monotonic_time_ns=monotonic_time_ns,
        leader=JointSample(joints),
        follower_before=JointSample(joints),
        action={"a": float(frame_index) + 0.1},
        follower_after=None,
        camera_frames={},
        camera_metrics={},
        latency_ms=1.0,
    )


def _frame(*, monotonic_time_ns: int, pixel: bytes) -> CameraFrame:
    return CameraFrame(
        name="wrist",
        width=1,
        height=1,
        rgb=pixel,
        timestamp=monotonic_time_ns / 1_000_000_000.0,
        monotonic_time_ns=monotonic_time_ns,
    )
