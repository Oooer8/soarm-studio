from __future__ import annotations

from contextlib import contextmanager
import json

import soarm_studio.hardware.arms as arms
import soarm_studio.recording.session as recording_session
from soarm_studio.config import ArmEndpointConfig, DatasetConfig, SessionConfig
from soarm_studio.recording.quality import RecordingQualityTracker
from soarm_studio.recording.session import (
    RecordingControls,
    _run_continuous_recording_loop,
    _start_camera_histories,
)
from soarm_studio.recording.timing import (
    RecordingTimingCalibration,
    camera_phase_alignment_from_warmup,
    timing_calibration_from_warmup,
    write_episode_samples,
)
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
    assert timing["timing_model"] == {
        "joint_read_lead_ms": 0.0,
        "camera_receive_to_estimated_exposure_ms": {},
    }
    assert timing["cameras"] == {}


def test_record_lerobot_episodes_can_retry_current_episode(tmp_path) -> None:
    root = tmp_path / "dataset"
    config = SessionConfig(
        name="test-recording-retry",
        loop_hz=30,
        joints=["a", "b"],
        leader=ArmEndpointConfig(name="leader", mock=True, scripted=True),
        follower=ArmEndpointConfig(name="follower", mock=True, max_relative_target=0.1),
        cameras={},
        dataset=DatasetConfig(root=str(root), repo_id="local/test", fps=30),
    )
    seen_attempts: list[int] = []
    decisions = iter(["retry", "save"])

    def before_episode(info) -> bool:
        seen_attempts.append(int(info.attempt))
        return True

    result = record_lerobot_episodes(
        config,
        seconds=0.05,
        task="test task",
        overwrite=True,
        controls=RecordingControls(
            before_episode=before_episode,
            after_episode=lambda info: next(decisions),
        ),
    )

    assert seen_attempts == [1, 2]
    assert result["episodes"][0]["episode_index"] == 0
    assert result["episodes"][0]["episode_number"] == 1
    assert result["episodes"][0]["attempt"] == 2
    assert (root / "episodes" / "episode_000000" / "quality.json").exists()
    assert not (root / "episodes" / "episode_000001").exists()


def test_record_lerobot_episodes_marks_early_stop(tmp_path) -> None:
    root = tmp_path / "dataset"
    config = SessionConfig(
        name="test-recording-stop",
        loop_hz=30,
        joints=["a", "b"],
        leader=ArmEndpointConfig(name="leader", mock=True, scripted=True),
        follower=ArmEndpointConfig(name="follower", mock=True, max_relative_target=0.1),
        cameras={},
        dataset=DatasetConfig(root=str(root), repo_id="local/test", fps=30),
    )

    @contextmanager
    def stop_after_first_sample(loop: object):
        original_on_sample = loop.on_sample

        def wrapped_on_sample(sample) -> None:
            original_on_sample(sample)
            loop.stop_requested = True

        loop.on_sample = wrapped_on_sample
        yield

    result = record_lerobot_episodes(
        config,
        seconds=1.0,
        task="test task",
        overwrite=True,
        controls=RecordingControls(recording_context=stop_after_first_sample),
    )

    metrics = result["episodes"][0]["metrics"]
    assert metrics["stopped_early"] is True
    assert metrics["iterations"] == 1


def test_record_lerobot_episodes_can_retry_from_recording_context(tmp_path) -> None:
    root = tmp_path / "dataset"
    config = SessionConfig(
        name="test-recording-context-retry",
        loop_hz=30,
        joints=["a", "b"],
        leader=ArmEndpointConfig(name="leader", mock=True, scripted=True),
        follower=ArmEndpointConfig(name="follower", mock=True, max_relative_target=0.1),
        cameras={},
        dataset=DatasetConfig(root=str(root), repo_id="local/test", fps=30),
    )
    requested_retry = False
    decisions: list[str] = []

    @contextmanager
    def retry_after_first_sample(loop: object):
        nonlocal requested_retry
        original_on_sample = loop.on_sample

        def wrapped_on_sample(sample) -> None:
            nonlocal requested_retry
            original_on_sample(sample)
            if not requested_retry:
                requested_retry = True
                loop.stop_reason = "retry"
                loop.stop_requested = True

        loop.on_sample = wrapped_on_sample
        yield

    def after_episode(info) -> str:
        decision = "retry" if info.metrics.get("stop_reason") == "retry" else "save"
        decisions.append(decision)
        return decision

    result = record_lerobot_episodes(
        config,
        seconds=0.05,
        task="test task",
        overwrite=True,
        controls=RecordingControls(
            after_episode=after_episode,
            recording_context=retry_after_first_sample,
        ),
    )

    assert decisions == ["retry", "save"]
    assert result["episodes"][0]["episode_index"] == 0
    assert result["episodes"][0]["attempt"] == 2


def test_record_warmup_and_episode_share_one_stream(monkeypatch, tmp_path) -> None:
    created: list[arms.MockArm] = []

    class CountingMockArm(arms.MockArm):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.move_calls = 0
            self.stream_starts = 0
            created.append(self)

        def read_joints(self):
            if self.name == "leader":
                return JointSample({"a": 1.0, "b": -1.0})
            return super().read_joints()

        def move_joints(self, targets, *, duration=None) -> None:
            self.move_calls += 1
            super().move_joints(targets, duration=duration)

        def start_joint_stream(self, **kwargs):
            self.stream_starts += 1
            return super().start_joint_stream(**kwargs)

    monkeypatch.setattr(arms, "MockArm", CountingMockArm)
    root = tmp_path / "dataset"
    config = SessionConfig(
        name="test-recording-warmup",
        loop_hz=30,
        joints=["a", "b"],
        leader=ArmEndpointConfig(name="leader", mock=True),
        follower=ArmEndpointConfig(name="follower", mock=True),
        cameras={},
        dataset=DatasetConfig(root=str(root), repo_id="local/test", fps=30),
    )

    result = record_lerobot_episodes(
        config,
        seconds=0.05,
        warmup=0.05,
        task="test task",
        overwrite=True,
    )

    follower = next(arm for arm in created if arm.name == "follower")
    assert follower.move_calls == 1
    assert follower.stream_starts == 1
    assert 0 < result["episodes"][0]["metrics"]["iterations"] <= 3


def test_write_episode_samples_matches_nearest_estimated_exposure_frame() -> None:
    captured: list[dict] = []

    class FakeEpisode:
        def add_frame(self, **kwargs) -> None:
            captured.append(kwargs)

    samples = [
        _sample(frame_index=0, monotonic_time_ns=1_000_000_000),
        _sample(frame_index=1, monotonic_time_ns=1_100_000_000),
    ]
    early = _frame(monotonic_time_ns=1_040_000_000, pixel=b"\x01\x00\x00")
    late = _frame(monotonic_time_ns=1_140_000_000, pixel=b"\x02\x00\x00")
    quality = RecordingQualityTracker()

    timing = write_episode_samples(
        FakeEpisode(),
        samples,
        quality,
        {"wrist": [late, early]},
    )

    assert captured[0]["images"]["wrist"] is early
    assert captured[1]["images"]["wrist"] is late
    assert [item["timestamp"] for item in captured] == [0.0, 0.1]
    assert quality.to_dict()["max_camera_age_ms"] == 10.0
    assert timing["cameras"]["wrist"]["raw_intervals_ms"] == [100.0]
    assert timing["cameras"]["wrist"]["raw_observed_fps"] == 10.0
    assert timing["cameras"]["wrist"]["matched_samples"][0]["camera_frame_index"] == 0
    assert timing["cameras"]["wrist"]["matched_samples"][1]["camera_frame_index"] == 1


def test_write_episode_samples_trims_uncovered_tail_samples() -> None:
    captured: list[dict] = []

    class FakeEpisode:
        def add_frame(self, **kwargs) -> None:
            captured.append(kwargs)

    samples = [
        _sample(frame_index=0, monotonic_time_ns=1_000_000_000),
        _sample(frame_index=1, monotonic_time_ns=1_100_000_000),
        _sample(frame_index=2, monotonic_time_ns=1_200_000_000),
        _sample(frame_index=3, monotonic_time_ns=1_300_000_000),
    ]
    frames = [
        _frame(monotonic_time_ns=1_000_000_000, pixel=b"\x01\x00\x00"),
        _frame(monotonic_time_ns=1_100_000_000, pixel=b"\x02\x00\x00"),
        _frame(monotonic_time_ns=1_200_000_000, pixel=b"\x03\x00\x00"),
    ]
    quality = RecordingQualityTracker()

    timing = write_episode_samples(
        FakeEpisode(),
        samples,
        quality,
        {"wrist": frames},
        RecordingTimingCalibration(
            camera_receive_to_exposure_shift_ns={"wrist": 0},
        ),
    )

    assert len(captured) == 3
    assert quality.to_dict()["frames"] == 3
    assert quality.to_dict()["max_camera_age_ms"] == 0.0
    assert timing["sample_count"] == 3
    assert timing["original_sample_count"] == 4
    assert timing["tail_trim"] == {
        "original_sample_count": 4,
        "trimmed_sample_count": 1,
        "trimmed_frame_indices": [3],
        "reason": "trailing sample target tick exceeded camera coverage",
        "camera_tail_coverage_limit_s": {"wrist": 1.25},
    }
    assert [
        item["sample_frame_index"]
        for item in timing["cameras"]["wrist"]["matched_samples"]
    ] == [0, 1, 2]


def test_write_episode_samples_uses_estimated_camera_exposure_time() -> None:
    captured: list[dict] = []

    class FakeEpisode:
        def add_frame(self, **kwargs) -> None:
            captured.append(kwargs)

    sample = _sample(frame_index=0, monotonic_time_ns=1_000_000_000)
    early = _frame(monotonic_time_ns=1_010_000_000, pixel=b"\x01\x00\x00")
    aligned = _frame(monotonic_time_ns=1_040_000_000, pixel=b"\x02\x00\x00")
    quality = RecordingQualityTracker()

    timing = write_episode_samples(
        FakeEpisode(),
        [sample],
        quality,
        {"wrist": [early, aligned]},
        RecordingTimingCalibration(
            camera_receive_to_exposure_shift_ns={"wrist": -40_000_000},
        ),
    )

    assert captured[0]["images"]["wrist"] is aligned
    assert quality.to_dict()["max_camera_age_ms"] == 0.0
    camera_timing = timing["cameras"]["wrist"]
    assert camera_timing["receive_to_estimated_exposure_ms"] == -40.0
    assert camera_timing["matched_samples"][0]["offset_ms"] == 0.0
    assert camera_timing["matched_samples"][0]["receive_offset_ms"] == 40.0


def test_timing_calibration_from_warmup_estimates_joint_lead_and_camera_shift() -> None:
    samples = [
        _sample(frame_index=0, monotonic_time_ns=1_000_000_000, joint_estimated_offset_ns=3_000_000),
        _sample(frame_index=1, monotonic_time_ns=1_100_000_000, joint_estimated_offset_ns=3_000_000),
        _sample(frame_index=2, monotonic_time_ns=1_200_000_000, joint_estimated_offset_ns=3_000_000),
    ]
    frames = [
        _frame(monotonic_time_ns=1_000_000_000, pixel=b"\x01\x00\x00"),
        _frame(monotonic_time_ns=1_100_000_000, pixel=b"\x02\x00\x00"),
        _frame(monotonic_time_ns=1_200_000_000, pixel=b"\x03\x00\x00"),
    ]

    calibration = timing_calibration_from_warmup(samples, {"wrist": frames})

    assert calibration.joint_read_lead_ns == 3_000_000
    assert calibration.camera_receive_to_exposure_shift_ns == {"wrist": -50_000_000}


def test_timing_calibration_from_warmup_uses_dominant_timing_bucket() -> None:
    samples = [
        _sample(frame_index=0, monotonic_time_ns=1_000_000_000, joint_estimated_offset_ns=24_000_000),
        _sample(frame_index=1, monotonic_time_ns=1_100_000_000, joint_estimated_offset_ns=25_000_000),
        _sample(frame_index=2, monotonic_time_ns=1_200_000_000, joint_estimated_offset_ns=2_900_000),
        _sample(frame_index=3, monotonic_time_ns=1_300_000_000, joint_estimated_offset_ns=3_000_000),
        _sample(frame_index=4, monotonic_time_ns=1_400_000_000, joint_estimated_offset_ns=3_100_000),
    ]
    frames = [
        _frame(monotonic_time_ns=1_000_000_000, pixel=b"\x01\x00\x00"),
        _frame(monotonic_time_ns=1_150_000_000, pixel=b"\x02\x00\x00"),
        _frame(monotonic_time_ns=1_250_000_000, pixel=b"\x03\x00\x00"),
        _frame(monotonic_time_ns=1_350_000_000, pixel=b"\x04\x00\x00"),
        _frame(monotonic_time_ns=1_450_000_000, pixel=b"\x05\x00\x00"),
    ]

    calibration = timing_calibration_from_warmup(samples, {"wrist": frames})

    assert calibration.joint_read_lead_ns == 3_000_000
    assert calibration.camera_receive_to_exposure_shift_ns == {"wrist": -50_000_000}


def test_timing_calibration_from_warmup_ignores_insufficient_camera_history() -> None:
    calibration = timing_calibration_from_warmup(
        [],
        {"wrist": [_frame(monotonic_time_ns=1_000_000_000, pixel=b"\x01\x00\x00")]},
    )

    assert calibration.camera_receive_to_exposure_shift_ns == {}


def test_camera_phase_alignment_targets_next_estimated_exposure() -> None:
    frames = [
        _frame(monotonic_time_ns=1_000_000_000, pixel=b"\x01\x00\x00"),
        _frame(monotonic_time_ns=1_100_000_000, pixel=b"\x02\x00\x00"),
        _frame(monotonic_time_ns=1_200_000_000, pixel=b"\x03\x00\x00"),
    ]
    calibration = RecordingTimingCalibration(
        camera_receive_to_exposure_shift_ns={"wrist": -50_000_000},
    )

    alignment = camera_phase_alignment_from_warmup(
        {"wrist": frames},
        calibration,
        earliest_target_ns=1_180_000_000,
    )

    assert alignment is not None
    assert alignment.target_tick_ns == 1_250_000_000
    assert alignment.phase_wait_ns == 70_000_000
    assert alignment.expected_camera_offset_ns == {"wrist": 0}
    assert alignment.camera_period_ns == {"wrist": 100_000_000}


def test_camera_phase_alignment_compromises_across_cameras() -> None:
    calibration = RecordingTimingCalibration(
        camera_receive_to_exposure_shift_ns={
            "third_person": -50_000_000,
            "wrist": -50_000_000,
        },
    )

    alignment = camera_phase_alignment_from_warmup(
        {
            "third_person": [
                _frame(monotonic_time_ns=1_000_000_000, pixel=b"\x01\x00\x00"),
                _frame(monotonic_time_ns=1_100_000_000, pixel=b"\x02\x00\x00"),
                _frame(monotonic_time_ns=1_200_000_000, pixel=b"\x03\x00\x00"),
            ],
            "wrist": [
                _frame(monotonic_time_ns=1_010_000_000, pixel=b"\x04\x00\x00"),
                _frame(monotonic_time_ns=1_110_000_000, pixel=b"\x05\x00\x00"),
                _frame(monotonic_time_ns=1_210_000_000, pixel=b"\x06\x00\x00"),
            ],
        },
        calibration,
        earliest_target_ns=1_180_000_000,
    )

    assert alignment is not None
    assert alignment.target_tick_ns == 1_255_000_000
    assert alignment.expected_camera_offset_ns == {
        "third_person": -5_000_000,
        "wrist": 5_000_000,
    }


def test_recording_loop_uses_phase_aligned_first_target_tick(monkeypatch) -> None:
    run_first_targets, metrics = _run_fake_recording_loop_for_phase_alignment(
        monkeypatch,
        warmup=0.1,
        warmup_frames=[
            _frame(monotonic_time_ns=1_000_000_000, pixel=b"\x01\x00\x00"),
            _frame(monotonic_time_ns=1_100_000_000, pixel=b"\x02\x00\x00"),
            _frame(monotonic_time_ns=1_200_000_000, pixel=b"\x03\x00\x00"),
        ],
    )

    assert run_first_targets == [None, 1_250_000_000]
    assert metrics["camera_phase_alignment_ms"]["phase_wait_ms"] == 69.0


def test_recording_loop_uses_phase_reference_without_warmup(monkeypatch) -> None:
    run_first_targets, metrics = _run_fake_recording_loop_for_phase_alignment(
        monkeypatch,
        warmup=0.0,
        timing_calibration=RecordingTimingCalibration(
            camera_receive_to_exposure_shift_ns={"wrist": -50_000_000},
        ),
        phase_reference_histories={
            "wrist": [
                _frame(monotonic_time_ns=1_000_000_000, pixel=b"\x01\x00\x00"),
                _frame(monotonic_time_ns=1_100_000_000, pixel=b"\x02\x00\x00"),
                _frame(monotonic_time_ns=1_200_000_000, pixel=b"\x03\x00\x00"),
            ],
        },
    )

    assert run_first_targets == [None, 1_250_000_000]
    assert metrics["camera_phase_alignment_ms"]["expected_camera_offset_ms"] == {"wrist": 0.0}


def test_start_camera_histories_seeds_latest_frame() -> None:
    class FakeCamera:
        def __init__(self) -> None:
            self.seed_latest = None

        def start_history(self, *, seed_latest: bool = False) -> None:
            self.seed_latest = seed_latest

        def stop_history(self):
            return []

    camera = FakeCamera()

    recorders = _start_camera_histories({"wrist": camera})

    assert recorders == {"wrist": camera}
    assert camera.seed_latest is True


def _run_fake_recording_loop_for_phase_alignment(
    monkeypatch,
    *,
    warmup: float,
    warmup_frames: list[CameraFrame] | None = None,
    timing_calibration: RecordingTimingCalibration | None = None,
    phase_reference_histories: dict[str, list[CameraFrame]] | None = None,
) -> tuple[list[int | None], dict]:
    run_first_targets: list[int | None] = []

    class FakeMetrics:
        def to_dict(self) -> dict:
            return {"iterations": 1}

    class FakeLoop:
        def __init__(self) -> None:
            self.on_sample = None
            self.sample_cameras = False
            self.sensor_read_lead_s = 0.0
            self.sync_start = True
            self.stop_requested = False
            self.stop_reason = None

        def run(
            self,
            *,
            seconds=None,
            steps=None,
            close_on_finish=True,
            first_target_tick_ns=None,
        ):
            run_first_targets.append(first_target_tick_ns)
            if self.on_sample is not None:
                self.on_sample(_sample(frame_index=0, monotonic_time_ns=1_000_000_000))
            return FakeMetrics()

        def reset_metrics(self) -> None:
            return None

    class FakeCamera:
        def __init__(self) -> None:
            self.starts = 0

        def start_history(self, *, seed_latest: bool = False) -> None:
            self.starts += 1

        def stop_history(self) -> list[CameraFrame]:
            if self.starts == 1 and warmup_frames is not None:
                return list(warmup_frames)
            return []

    class FakeRunningLoop:
        def __init__(self, loop: FakeLoop) -> None:
            self.loop = loop

        def __enter__(self) -> FakeLoop:
            return self.loop

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class FakeHardware:
        def __init__(self) -> None:
            self.cameras = {"wrist": FakeCamera()}
            self.loop = FakeLoop()

        def running_loop(self, *, on_sample=None, sample_cameras=False):
            self.loop.on_sample = on_sample
            self.loop.sample_cameras = sample_cameras
            return FakeRunningLoop(self.loop)

    monkeypatch.setattr(recording_session.time, "monotonic_ns", lambda: 1_180_000_000)
    samples: list[ControlSample] = []
    metrics, _frame_histories, _calibration = _run_continuous_recording_loop(
        FakeHardware(),
        seconds=0.1,
        warmup=warmup,
        on_sample=samples.append,
        timing_calibration=timing_calibration,
        phase_reference_histories=phase_reference_histories,
    )
    return run_first_targets, metrics


def _sample(
    *,
    frame_index: int,
    monotonic_time_ns: int,
    joint_estimated_offset_ns: int = 0,
) -> ControlSample:
    joints = {"a": float(frame_index)}
    joint_sample = JointSample(
        joints,
        timestamp=monotonic_time_ns / 1_000_000_000.0,
        monotonic_time_ns=monotonic_time_ns,
        request_start_monotonic_time_ns=monotonic_time_ns - 1_000_000,
        receive_monotonic_time_ns=monotonic_time_ns + 1_000_000,
        estimated_sample_monotonic_time_ns=monotonic_time_ns + joint_estimated_offset_ns,
    )
    return ControlSample(
        frame_index=frame_index,
        monotonic_time_ns=monotonic_time_ns,
        leader=joint_sample,
        follower_before=joint_sample,
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
