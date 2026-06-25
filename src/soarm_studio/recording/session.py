from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol

from soarm_studio.config import SessionConfig
from soarm_studio.datasets.lerobot_v3 import LeRobotV3Writer
from soarm_studio.hardware.runtime import HardwareSession
from soarm_studio.types import CameraFrame
from soarm_studio.teleop import ControlSample

from .quality import RecordingQualityTracker
from .timing import (
    RecordingTimingCalibration,
    merge_timing_calibration,
    timing_calibration_from_warmup,
    timing_calibration_to_dict,
    write_episode_samples,
)


class _CameraHistoryRecorder(Protocol):
    def start_history(self, *, seed_latest: bool = False) -> None: ...

    def stop_history(self) -> list[CameraFrame]: ...


class RecordingLoopControl(Protocol):
    stop_requested: bool
    stop_reason: str | None


EpisodeDecision = Literal["save", "retry", "abort"]


@dataclass(frozen=True)
class EpisodeStartInfo:
    episode_number: int
    total_episodes: int
    attempt: int
    task: str
    seconds: float
    warmup: float


@dataclass(frozen=True)
class EpisodeResultInfo:
    episode_number: int
    total_episodes: int
    attempt: int
    task: str
    metrics: dict
    quality: dict


@dataclass(frozen=True)
class RecordingControls:
    before_episode: Callable[[EpisodeStartInfo], bool] | None = None
    after_episode: Callable[[EpisodeResultInfo], EpisodeDecision] | None = None
    recording_context: Callable[[RecordingLoopControl], AbstractContextManager[None]] | None = None


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
    debug: bool = False,
    controls: RecordingControls | None = None,
) -> dict:
    controls = controls or RecordingControls()
    writer = create_lerobot_writer(config, overwrite=overwrite)
    saved: list[dict] = []
    session_started = datetime.now(timezone.utc).isoformat()
    total_episodes = max(1, int(episodes))
    aborted = False
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
            attempt = 0
            timing_calibration = RecordingTimingCalibration()
            while len(saved) < total_episodes:
                episode_number = len(saved) + 1
                attempt += 1
                episode_info = EpisodeStartInfo(
                    episode_number=episode_number,
                    total_episodes=total_episodes,
                    attempt=attempt,
                    task=task,
                    seconds=seconds,
                    warmup=warmup if not saved else 0.0,
                )
                if (
                    controls.before_episode is not None
                    and not controls.before_episode(episode_info)
                ):
                    aborted = True
                    break

                quality = RecordingQualityTracker()
                episode = writer.start_episode(task)
                samples: list[ControlSample] = []

                def add_sample(sample: ControlSample) -> None:
                    samples.append(sample)

                try:
                    metrics, frame_histories, episode_timing_calibration = _run_continuous_recording_loop(
                        hardware,
                        seconds=seconds,
                        warmup=warmup if not saved else 0.0,
                        on_sample=add_sample,
                        recording_context=controls.recording_context,
                        timing_calibration=timing_calibration,
                    )
                    timing_calibration = episode_timing_calibration
                    camera_timing = write_episode_samples(
                        episode,
                        samples,
                        quality,
                        frame_histories,
                        timing_calibration,
                    )
                    quality_dict = quality.to_dict()
                    pending = EpisodeResultInfo(
                        episode_number=episode_number,
                        total_episodes=total_episodes,
                        attempt=attempt,
                        task=task,
                        metrics=metrics,
                        quality=quality_dict,
                    )
                    decision = (
                        "save"
                        if controls.after_episode is None
                        else controls.after_episode(pending)
                    )
                    if decision == "retry":
                        episode.discard()
                        continue
                    if decision == "abort":
                        episode.discard()
                        aborted = True
                        break
                    if decision != "save":
                        raise ValueError(
                            "after_episode must return 'save', 'retry', or 'abort'"
                        )

                    episode_index = episode.save()
                    writer.write_episode_quality(episode_index, quality_dict)
                    if debug:
                        writer.write_episode_camera_timing(episode_index, camera_timing)
                    saved.append(
                        {
                            "episode_index": episode_index,
                            "episode_number": episode_number,
                            "attempt": attempt,
                            "task": task,
                            "metrics": metrics,
                            "quality": quality_dict,
                        }
                    )
                    attempt = 0
                except Exception:
                    episode.discard()
                    raise

            writer.write_sync_quality(
                {
                    "episodes": saved,
                    "summary": _quality_summary(saved),
                    "aborted": aborted,
                }
            )
    finally:
        writer.finalize()
    return {
        "dataset": config.dataset.root,
        "task": task,
        "episodes": saved,
        "started_at": session_started,
        "aborted": aborted,
    }


def _run_continuous_recording_loop(
    hardware: HardwareSession,
    *,
    seconds: float,
    warmup: float,
    on_sample: Callable[[ControlSample], None],
    sample_cameras: bool | None = None,
    recording_context: Callable[[RecordingLoopControl], AbstractContextManager[None]] | None = None,
    timing_calibration: RecordingTimingCalibration | None = None,
) -> tuple[dict, dict[str, list[CameraFrame]], RecordingTimingCalibration]:
    history_recorders: dict[str, _CameraHistoryRecorder] = {}
    warmup_recorders: dict[str, _CameraHistoryRecorder] = {}
    timing_calibration = timing_calibration or RecordingTimingCalibration()
    with hardware.running_loop(on_sample=None, sample_cameras=False) as loop:
        try:
            warmup_samples: list[ControlSample] = []
            warmup_frame_histories: dict[str, list[CameraFrame]] = {}
            if warmup > 0:
                warmup_recorders = _start_camera_histories(
                    hardware.cameras,
                    seed_latest=False,
                )
                loop.on_sample = warmup_samples.append
                loop.run(seconds=warmup, close_on_finish=False)
                warmup_frame_histories = _stop_camera_histories(warmup_recorders)
                warmup_recorders = {}
                loop.on_sample = None
            else:
                loop.run(steps=0, close_on_finish=False)
            loop.sync_start = False
            loop.reset_metrics()
            warmup_timing_calibration = timing_calibration_from_warmup(
                warmup_samples,
                warmup_frame_histories,
            )
            timing_calibration = merge_timing_calibration(
                timing_calibration,
                warmup_timing_calibration,
            )
            loop.sensor_read_lead_s = timing_calibration.joint_read_lead_ns / 1_000_000_000.0

            history_recorders = _start_camera_histories(hardware.cameras)
            if sample_cameras is None:
                sample_cameras = _should_sample_cameras(hardware.cameras, history_recorders)
            loop.on_sample = on_sample
            loop.sample_cameras = sample_cameras
            loop.stop_requested = False
            loop.stop_reason = None
            context = nullcontext() if recording_context is None else recording_context(loop)
            with context:
                metrics = loop.run(seconds=seconds, close_on_finish=False)
            frame_histories = _stop_camera_histories(history_recorders)
            history_recorders = {}
            metrics_dict = metrics.to_dict()
            if getattr(loop, "stop_requested", False):
                metrics_dict["stopped_early"] = True
            if getattr(loop, "stop_reason", None) is not None:
                metrics_dict["stop_reason"] = loop.stop_reason
            metrics_dict["sensor_timing_calibration_ms"] = timing_calibration_to_dict(
                timing_calibration
            )
            return metrics_dict, frame_histories, timing_calibration
        finally:
            _stop_camera_histories(warmup_recorders)
            _stop_camera_histories(history_recorders)


def _start_camera_histories(
    cameras: dict[str, object],
    *,
    seed_latest: bool = True,
) -> dict[str, _CameraHistoryRecorder]:
    recorders: dict[str, _CameraHistoryRecorder] = {}
    for name, camera in cameras.items():
        start_history = getattr(camera, "start_history", None)
        stop_history = getattr(camera, "stop_history", None)
        if not callable(start_history) or not callable(stop_history):
            continue
        recorders[name] = camera
    for camera in recorders.values():
        camera.start_history(seed_latest=seed_latest)
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
