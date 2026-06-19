from __future__ import annotations

import time
from typing import Callable

from soarm_studio.hardware import Arm, Camera
from soarm_studio.types import CameraFrame, CameraSyncMetric, ControlSample, LoopMetrics


class TeleopLoop:
    def __init__(
        self,
        *,
        leader: Arm,
        follower: Arm,
        joint_names: list[str],
        hz: int,
        max_relative_target: float | None = None,
        cameras: dict[str, Camera] | None = None,
        on_sample: Callable[[ControlSample], None] | None = None,
        on_frame: Callable[[dict[str, float], dict[str, float], dict[str, CameraFrame]], None]
        | None = None,
        slow_camera_ms: float = 100.0,
    ) -> None:
        self.leader = leader
        self.follower = follower
        self.joint_names = joint_names
        self.hz = hz
        self.dt = 1.0 / hz
        self.max_relative_target = max_relative_target
        self.cameras = cameras or {}
        self.on_sample = on_sample
        self.on_frame = on_frame
        self.slow_camera_ms = float(slow_camera_ms)
        self.paused = False
        self.metrics = LoopMetrics(target_hz=hz)

    def connect(self) -> None:
        self.leader.connect()
        self.follower.connect()
        for camera in self.cameras.values():
            camera.connect()

    def disconnect(self) -> None:
        for camera in self.cameras.values():
            camera.disconnect()
        self.follower.disconnect()
        self.leader.disconnect()

    def pause(self) -> None:
        self.paused = True
        self.follower.stop()

    def resume(self) -> None:
        self.paused = False

    def emergency_stop(self) -> None:
        self.paused = True
        self.follower.emergency_stop()

    def step(self) -> ControlSample:
        started = time.monotonic()
        started_ns = time.monotonic_ns()
        frame_index = self.metrics.iterations
        leader_sample = self.leader.read_joints()
        follower_before = self.follower.read_joints()
        self._require_joint_keys("leader", leader_sample.positions)
        self._require_joint_keys("follower", follower_before.positions)
        action = self._clip_relative(follower_before.positions, leader_sample.positions)

        if not self.paused:
            self.follower.send_joints(action)

        frames: dict[str, CameraFrame] = {}
        camera_metrics: dict[str, CameraSyncMetric] = {}
        for name, camera in self.cameras.items():
            camera_started = time.monotonic()
            try:
                frame = camera.read()
            except Exception as exc:
                latency_ms = (time.monotonic() - camera_started) * 1000.0
                camera_metrics[name] = CameraSyncMetric(
                    camera=name,
                    ok=False,
                    timestamp=None,
                    monotonic_time_ns=None,
                    read_latency_ms=latency_ms,
                    error=str(exc),
                )
                raise
            latency_ms = (time.monotonic() - camera_started) * 1000.0
            frames[name] = frame
            camera_metrics[name] = CameraSyncMetric(
                camera=name,
                ok=latency_ms <= self.slow_camera_ms,
                timestamp=frame.timestamp,
                monotonic_time_ns=frame.monotonic_time_ns,
                read_latency_ms=latency_ms,
                width=frame.width,
                height=frame.height,
                error=None if latency_ms <= self.slow_camera_ms else "slow camera read",
            )

        follower_after = self.follower.read_joints()
        self._require_joint_keys("follower_after", follower_after.positions)
        latency_ms = (time.monotonic() - started) * 1000.0
        sample = ControlSample(
            frame_index=frame_index,
            monotonic_time_ns=started_ns,
            leader=leader_sample,
            follower_before=follower_before,
            action=action,
            follower_after=follower_after,
            camera_frames=frames,
            camera_metrics=camera_metrics,
            latency_ms=latency_ms,
        )

        if self.on_sample is not None:
            self.on_sample(sample)
        if self.on_frame is not None:
            self.on_frame(follower_after.positions, action, frames)

        self.metrics.iterations += 1
        self.metrics.observe_latency(time.monotonic() - started)
        return sample

    def run(
        self,
        *,
        seconds: float | None = None,
        steps: int | None = None,
        sleep: bool = True,
    ) -> LoopMetrics:
        if seconds is None and steps is None:
            raise ValueError("Either seconds or steps is required")
        deadline = None if seconds is None else time.monotonic() + seconds

        while True:
            if steps is not None and self.metrics.iterations >= steps:
                break
            if deadline is not None and time.monotonic() >= deadline:
                break

            step_started = time.monotonic()
            self.step()
            if sleep:
                remaining = self.dt - (time.monotonic() - step_started)
                if remaining > 0:
                    time.sleep(remaining)

        return self.metrics

    def _require_joint_keys(self, source: str, positions: dict[str, float]) -> None:
        missing = [name for name in self.joint_names if name not in positions]
        if missing:
            raise RuntimeError(f"{source} is missing joints: {', '.join(missing)}")

    def _clip_relative(
        self,
        current: dict[str, float],
        target: dict[str, float],
    ) -> dict[str, float]:
        if self.max_relative_target is None:
            return {name: float(target[name]) for name in self.joint_names}

        clipped: dict[str, float] = {}
        for name in self.joint_names:
            now = float(current[name])
            wanted = float(target[name])
            delta = max(-self.max_relative_target, min(self.max_relative_target, wanted - now))
            clipped[name] = now + delta
        return clipped
