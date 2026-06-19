from __future__ import annotations

import time
from typing import Callable

from soarm_studio.hardware import Arm, Camera
from soarm_studio.types import CameraFrame, LoopMetrics


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
        on_frame: Callable[[dict[str, float], dict[str, float], dict[str, CameraFrame]], None]
        | None = None,
    ) -> None:
        self.leader = leader
        self.follower = follower
        self.joint_names = joint_names
        self.hz = hz
        self.dt = 1.0 / hz
        self.max_relative_target = max_relative_target
        self.cameras = cameras or {}
        self.on_frame = on_frame
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

    def step(self) -> LoopMetrics:
        started = time.monotonic()
        leader_positions = self.leader.read_joints().positions
        follower_positions = self.follower.read_joints().positions
        action = self._clip_relative(follower_positions, leader_positions)

        if not self.paused:
            self.follower.send_joints(action)

        frames = {name: camera.read() for name, camera in self.cameras.items()}
        if self.on_frame is not None:
            state_after = self.follower.read_joints().positions
            self.on_frame(state_after, action, frames)

        self.metrics.iterations += 1
        self.metrics.observe_latency(time.monotonic() - started)
        return self.metrics

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
