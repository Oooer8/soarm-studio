from __future__ import annotations

from datetime import datetime, timezone

from soarm_studio.config import SessionConfig
from soarm_studio.datasets.lerobot_v3 import LeRobotV3Writer
from soarm_studio.hardware.runtime import HardwareSession
from soarm_studio.teleop import ControlSample

from .quality import RecordingQualityTracker


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
                first_sample_ns: int | None = None

                def add_sample(sample: ControlSample) -> None:
                    nonlocal first_sample_ns
                    if first_sample_ns is None:
                        first_sample_ns = sample.monotonic_time_ns
                    timestamp = (sample.monotonic_time_ns - first_sample_ns) / 1_000_000_000.0
                    episode.add_frame(
                        state=sample.follower_before.positions,
                        action=sample.action,
                        images=sample.camera_frames,
                        timestamp=timestamp,
                    )
                    quality.observe(sample)

                try:
                    metrics = hardware.run_teleop(seconds=seconds, on_sample=add_sample)
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


def _quality_summary(episodes: list[dict]) -> dict:
    if not episodes:
        return {
            "frames": 0,
            "dropped_camera_frames": 0,
            "max_loop_latency_ms": 0.0,
            "max_camera_latency_ms": 0.0,
        }
    frames = sum(int(item["quality"]["frames"]) for item in episodes)
    dropped = sum(int(item["quality"]["dropped_camera_frames"]) for item in episodes)
    max_loop = max(float(item["quality"]["max_loop_latency_ms"]) for item in episodes)
    max_camera = max(float(item["quality"]["max_camera_latency_ms"]) for item in episodes)
    return {
        "frames": frames,
        "dropped_camera_frames": dropped,
        "max_loop_latency_ms": max_loop,
        "max_camera_latency_ms": max_camera,
    }
