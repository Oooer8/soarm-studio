from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Protocol

from soarm_studio.config import SessionConfig
from soarm_studio.datasets.lerobot_v3 import LeRobotV3Writer
from soarm_studio.datasets.lerobot_v3.writer import EpisodeWriter
from soarm_studio.hardware.runtime import HardwareSession
from soarm_studio.types import CameraFrame, CameraSyncMetric
from soarm_studio.teleop import ControlSample

from .quality import RecordingQualityTracker


class _CameraHistoryRecorder(Protocol):
    def stop_history(self) -> list[CameraFrame]: ...


@dataclass(frozen=True)
class _CameraHistory:
    frames: list[CameraFrame]
    timestamps_ns: list[int]


def create_lerobot_writer(config: SessionConfig, *, overwrite: bool = False) -> LeRobotV3Writer:
    camera_shapes = {
        name: (camera.height, camera.width, 3)
        for name, camera in config.cameras.items()
        if camera.enabled
    }
    return LeRobotV3Writer(
        root=config.dataset.root,
        repo_id=config.dataset.repo_id,
        fps=config.dataset.fps,
        joint_names=config.joints,
        cameras=camera_shapes,
        robot_type=config.dataset.robot_type,
        overwrite=overwrite,
    )


def record_lerobot_episodes(
    config: SessionConfig,
    *,
    seconds: float,
    task: str,
    overwrite: bool = False,
    warmup: float = 0.0,
    episodes: int = 1,
) -> dict:
    writer = create_lerobot_writer(config, overwrite=overwrite)
    saved: list[dict] = []
    session_started = datetime.now(timezone.utc).isoformat()
    try:
        with HardwareSession(config) as hardware:
            writer.write_session_metadata(
                {
                    "session": config.name,
                    "started_at": session_started,
                    "bindings": hardware.bindings(),
                    "loop_hz": config.loop_hz,
                    "dataset_fps": config.dataset.fps,
                    "joints": config.joints,
                }
            )
            if warmup > 0:
                hardware.run_teleop(seconds=warmup)

            for _ in range(max(1, int(episodes))):
                quality = RecordingQualityTracker()
                episode = writer.start_episode(task)
                samples: list[ControlSample] = []
                history_recorders = _start_camera_histories(hardware.cameras)
                sample_cameras = _should_sample_cameras(hardware.cameras, history_recorders)

                def add_sample(sample: ControlSample) -> None:
                    samples.append(sample)

                try:
                    metrics = hardware.run_teleop(
                        seconds=seconds,
                        on_sample=add_sample,
                        sample_cameras=sample_cameras,
                    )
                    frame_histories = _stop_camera_histories(history_recorders)
                    _write_episode_samples(episode, samples, quality, frame_histories)
                    episode_index = episode.save()
                    quality_dict = quality.to_dict()
                    writer.write_episode_quality(episode_index, quality_dict)
                    saved.append(
                        {
                            "episode_index": episode_index,
                            "task": task,
                            "metrics": metrics,
                            "quality": quality_dict,
                        }
                    )
                except Exception:
                    _stop_camera_histories(history_recorders)
                    episode.discard()
                    raise

            writer.write_sync_quality(
                {
                    "episodes": saved,
                    "summary": _quality_summary(saved),
                }
            )
    finally:
        writer.finalize()
    return {
        "dataset": config.dataset.root,
        "task": task,
        "episodes": saved,
        "started_at": session_started,
    }


def _write_episode_samples(
    episode: EpisodeWriter,
    samples: list[ControlSample],
    quality: RecordingQualityTracker,
    frame_histories: dict[str, list[CameraFrame]] | None = None,
) -> None:
    histories = {
        name: _camera_history(frames)
        for name, frames in (frame_histories or {}).items()
    }
    first_sample_ns: int | None = None
    for sample in samples:
        if first_sample_ns is None:
            first_sample_ns = sample.monotonic_time_ns
        timestamp = (sample.monotonic_time_ns - first_sample_ns) / 1_000_000_000.0
        matched_sample = _sample_with_matched_camera_frames(sample, histories)
        episode.add_frame(
            state=matched_sample.follower_before.positions,
            action=matched_sample.action,
            images=matched_sample.camera_frames,
            timestamp=timestamp,
        )
        quality.observe(matched_sample)


def _start_camera_histories(cameras: dict[str, object]) -> dict[str, _CameraHistoryRecorder]:
    recorders: dict[str, _CameraHistoryRecorder] = {}
    for name, camera in cameras.items():
        start_history = getattr(camera, "start_history", None)
        stop_history = getattr(camera, "stop_history", None)
        if not callable(start_history) or not callable(stop_history):
            continue
        start_history()
        recorders[name] = camera
    return recorders


def _stop_camera_histories(
    recorders: dict[str, _CameraHistoryRecorder],
) -> dict[str, list[CameraFrame]]:
    histories: dict[str, list[CameraFrame]] = {}
    for name, recorder in recorders.items():
        histories[name] = recorder.stop_history()
    return histories


def _should_sample_cameras(
    cameras: dict[str, object],
    history_recorders: dict[str, _CameraHistoryRecorder],
) -> bool:
    return any(name not in history_recorders for name in cameras)


def _camera_history(frames: list[CameraFrame]) -> _CameraHistory:
    sorted_frames = sorted(frames, key=lambda frame: frame.monotonic_time_ns)
    return _CameraHistory(
        frames=sorted_frames,
        timestamps_ns=[frame.monotonic_time_ns for frame in sorted_frames],
    )


def _sample_with_matched_camera_frames(
    sample: ControlSample,
    frame_histories: dict[str, _CameraHistory],
) -> ControlSample:
    if not frame_histories:
        return sample

    frames = dict(sample.camera_frames)
    metrics = dict(sample.camera_metrics)
    for name, history in frame_histories.items():
        matched = _nearest_frame(history, sample.monotonic_time_ns)
        if matched is None:
            metrics[name] = CameraSyncMetric(
                camera=name,
                ok=False,
                timestamp=None,
                monotonic_time_ns=None,
                read_latency_ms=0.0,
                error="missing camera history",
            )
            continue
        frames[name] = matched
        offset_ms = abs(sample.monotonic_time_ns - matched.monotonic_time_ns) / 1_000_000.0
        metrics[name] = CameraSyncMetric(
            camera=name,
            ok=True,
            timestamp=matched.timestamp,
            monotonic_time_ns=matched.monotonic_time_ns,
            read_latency_ms=0.0,
            frame_age_ms=offset_ms,
            width=matched.width,
            height=matched.height,
        )
    return replace(sample, camera_frames=frames, camera_metrics=metrics)


def _nearest_frame(history: _CameraHistory, monotonic_time_ns: int) -> CameraFrame | None:
    if not history.frames:
        return None
    index = bisect_left(history.timestamps_ns, monotonic_time_ns)
    if index <= 0:
        return history.frames[0]
    if index >= len(history.frames):
        return history.frames[-1]
    before = history.frames[index - 1]
    after = history.frames[index]
    before_delta = abs(monotonic_time_ns - before.monotonic_time_ns)
    after_delta = abs(after.monotonic_time_ns - monotonic_time_ns)
    return before if before_delta <= after_delta else after


def _quality_summary(episodes: list[dict]) -> dict:
    if not episodes:
        return {
            "frames": 0,
            "dropped_camera_frames": 0,
            "stale_camera_frames": 0,
            "max_loop_latency_ms": 0.0,
            "max_camera_latency_ms": 0.0,
            "max_camera_age_ms": 0.0,
        }
    frames = sum(int(item["quality"]["frames"]) for item in episodes)
    dropped = sum(int(item["quality"]["dropped_camera_frames"]) for item in episodes)
    stale = sum(int(item["quality"].get("stale_camera_frames", 0)) for item in episodes)
    max_loop = max(float(item["quality"]["max_loop_latency_ms"]) for item in episodes)
    max_camera = max(float(item["quality"]["max_camera_latency_ms"]) for item in episodes)
    max_camera_age = max(float(item["quality"].get("max_camera_age_ms", 0.0)) for item in episodes)
    return {
        "frames": frames,
        "dropped_camera_frames": dropped,
        "stale_camera_frames": stale,
        "max_loop_latency_ms": max_loop,
        "max_camera_latency_ms": max_camera,
        "max_camera_age_ms": max_camera_age,
    }
