from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import time
from typing import Callable

from soarm_studio.hardware import Arm, Camera, JointStream
from soarm_studio.types import CameraFrame, CameraSyncMetric, ControlSample, JointSample, LoopMetrics


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
        sync_start: bool = True,
        stream_output_hz: float | None = None,
        stream_target_timeout_s: float | None = None,
        profile: bool = False,
        follower_readback_every: int = 0,
        parallel_arm_reads: bool = True,
        sample_cameras: bool = True,
    ) -> None:
        self.leader = leader
        self.follower = follower
        self.joint_names = joint_names
        self.follower_limits = dict(follower.joint_limits())
        self.hz = hz
        self.dt = 1.0 / hz
        self.max_relative_target = max_relative_target
        self.cameras = cameras or {}
        self.on_sample = on_sample
        self.on_frame = on_frame
        self.slow_camera_ms = float(slow_camera_ms)
        self.sync_start = bool(sync_start)
        self.stream_output_hz = stream_output_hz
        self.stream_target_timeout_s = (
            max(self.dt * 3.0, 0.15)
            if stream_target_timeout_s is None
            else float(stream_target_timeout_s)
        )
        self.follower_readback_every = int(follower_readback_every)
        if self.follower_readback_every < 0:
            raise ValueError("follower_readback_every must be >= 0")
        self.parallel_arm_reads = bool(parallel_arm_reads)
        self.sample_cameras = bool(sample_cameras)
        self.sleep_guard_s = min(0.004, self.dt * 0.25)
        self.sleep_spin_s = min(0.0005, self.sleep_guard_s)
        self.paused = False
        self.metrics = LoopMetrics(target_hz=hz, profile=profile)
        self._stream: JointStream | None = None
        self._read_executor: ThreadPoolExecutor | None = None

    def connect(self) -> None:
        self.leader.connect()
        self.follower.connect()
        for camera in self.cameras.values():
            camera.connect()

    def disconnect(self) -> None:
        self._stop_stream()
        self._shutdown_read_executor()
        for camera in self.cameras.values():
            camera.disconnect()
        self.follower.disconnect()
        self.leader.disconnect()

    def pause(self) -> None:
        self.paused = True
        self._stop_stream()
        self.follower.stop()

    def resume(self) -> None:
        self.paused = False

    def emergency_stop(self) -> None:
        self.paused = True
        self._stop_stream()
        self.follower.emergency_stop()

    def close(self) -> None:
        self._stop_stream()
        self._shutdown_read_executor()

    def step(self) -> ControlSample:
        started = time.monotonic()
        started_ns = time.monotonic_ns()
        frame_index = self.metrics.iterations

        leader_sample, follower_before = self._read_arm_pair(
            leader_phase="leader_read",
            follower_phase="follower_before_read",
        )

        self._require_joint_keys("leader", leader_sample.positions)
        self._require_joint_keys("follower", follower_before.positions)
        with self._phase("target_compute"):
            action = self._clip_relative(follower_before.positions, leader_sample.positions)
            action = self._clip_joint_limits(action)

        if not self.paused:
            with self._phase("stream_update"):
                self._ensure_stream().update_target(action)

        frames: dict[str, CameraFrame] = {}
        camera_metrics: dict[str, CameraSyncMetric] = {}
        if self.sample_cameras:
            for name, camera in self.cameras.items():
                camera_started = time.monotonic()
                try:
                    with self._phase(f"camera_read:{name}"):
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
                frame_age_ms = (time.monotonic_ns() - frame.monotonic_time_ns) / 1_000_000.0
                self.metrics.observe_phase(f"camera_age:{name}", frame_age_ms / 1000.0)
                camera_ok = latency_ms <= self.slow_camera_ms and frame_age_ms <= self.slow_camera_ms
                camera_error = None
                if latency_ms > self.slow_camera_ms:
                    camera_error = "slow camera read"
                elif frame_age_ms > self.slow_camera_ms:
                    camera_error = "stale camera frame"
                frames[name] = frame
                camera_metrics[name] = CameraSyncMetric(
                    camera=name,
                    ok=camera_ok,
                    timestamp=frame.timestamp,
                    monotonic_time_ns=frame.monotonic_time_ns,
                    read_latency_ms=latency_ms,
                    frame_age_ms=frame_age_ms,
                    width=frame.width,
                    height=frame.height,
                    error=camera_error,
                )

        follower_after = None
        if self._should_read_follower_after(frame_index):
            with self._phase("follower_after_read"):
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

        with self._phase("callbacks"):
            if self.on_sample is not None:
                self.on_sample(sample)
            if self.on_frame is not None:
                frame_state = (
                    follower_after.positions
                    if follower_after is not None
                    else follower_before.positions
                )
                self.on_frame(frame_state, action, frames)

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
        try:
            if self.sync_start and not self.paused:
                self._sync_start_to_leader()

            self.metrics.started_at = time.monotonic()
            self.metrics.finished_at = None
            deadline = None if seconds is None else self.metrics.started_at + seconds
            next_tick = self.metrics.started_at
            while True:
                if steps is not None and self.metrics.iterations >= steps:
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    break

                self.metrics.observe_phase(
                    "scheduler_lag",
                    max(0.0, time.monotonic() - next_tick),
                )
                self.step()
                if sleep:
                    next_tick += self.dt
                    if steps is not None and self.metrics.iterations >= steps:
                        continue
                    sleep_until = next_tick if deadline is None else min(next_tick, deadline)
                    if sleep_until > time.monotonic():
                        with self._phase("sleep"):
                            self._sleep_until(sleep_until)
        finally:
            if self.metrics.finished_at is None:
                self.metrics.finish()
            self._stop_stream()
            self._shutdown_read_executor()

        return self.metrics

    def _ensure_stream(self) -> JointStream:
        if self._stream is None:
            self._stream = self.follower.start_joint_stream(
                output_hz=self.stream_output_hz,
                target_timeout_s=self.stream_target_timeout_s,
                joint_names=self.joint_names,
            )
        return self._stream

    def _stop_stream(self) -> None:
        if self._stream is None:
            return
        self._stream.stop()
        self._stream = None

    def _sync_start_to_leader(self) -> None:
        leader_sample, follower_sample = self._read_arm_pair(
            leader_phase="sync_start:leader_read",
            follower_phase="sync_start:follower_read",
        )
        self._require_joint_keys("leader", leader_sample.positions)
        self._require_joint_keys("follower", follower_sample.positions)
        target = {name: float(leader_sample.positions[name]) for name in self.joint_names}
        target = self._clip_joint_limits(target)
        max_delta = max(
            abs(float(target[name]) - float(follower_sample.positions[name]))
            for name in self.joint_names
        )
        if max_delta <= 1e-4:
            return
        with self._phase("sync_start:move"):
            self.follower.move_joints(target)

    def _should_read_follower_after(self, frame_index: int) -> bool:
        return self.follower_readback_every > 0 and frame_index % self.follower_readback_every == 0

    def _sleep_until(self, deadline_s: float) -> None:
        while True:
            remaining = deadline_s - time.monotonic()
            if remaining <= 0:
                return
            if remaining > self.sleep_guard_s:
                time.sleep(max(0.0, remaining - self.sleep_guard_s))
            elif remaining > self.sleep_spin_s:
                time.sleep(0)
            else:
                while time.monotonic() < deadline_s:
                    pass
                return

    def _read_arm_pair(
        self,
        *,
        leader_phase: str,
        follower_phase: str,
    ) -> tuple[JointSample, JointSample]:
        if not self.parallel_arm_reads:
            with self._phase(leader_phase):
                leader_sample = self.leader.read_joints()
            with self._phase(follower_phase):
                follower_sample = self.follower.read_joints()
            return leader_sample, follower_sample

        executor = self._arm_read_executor()
        leader_future = executor.submit(self._timed_read_joints, self.leader)
        follower_future = executor.submit(self._timed_read_joints, self.follower)
        leader_sample, leader_latency_s = leader_future.result()
        follower_sample, follower_latency_s = follower_future.result()
        self.metrics.observe_phase(leader_phase, leader_latency_s)
        self.metrics.observe_phase(follower_phase, follower_latency_s)
        return leader_sample, follower_sample

    def _arm_read_executor(self) -> ThreadPoolExecutor:
        if self._read_executor is None:
            self._read_executor = ThreadPoolExecutor(
                max_workers=2,
                thread_name_prefix="soarm-arm-read",
            )
        return self._read_executor

    def _shutdown_read_executor(self) -> None:
        if self._read_executor is None:
            return
        self._read_executor.shutdown(wait=False, cancel_futures=True)
        self._read_executor = None

    @staticmethod
    def _timed_read_joints(arm: Arm) -> tuple[JointSample, float]:
        started = time.monotonic()
        sample = arm.read_joints()
        return sample, time.monotonic() - started

    @contextmanager
    def _phase(self, name: str) -> Iterator[None]:
        if not self.metrics.profile:
            yield
            return
        started = time.monotonic()
        try:
            yield
        finally:
            self.metrics.observe_phase(name, time.monotonic() - started)

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

    def _clip_joint_limits(self, target: dict[str, float]) -> dict[str, float]:
        clipped: dict[str, float] = {}
        for name in self.joint_names:
            position = float(target[name])
            limits = self.follower_limits.get(name)
            if limits is not None:
                lower, upper = limits
                position = max(lower, min(upper, position))
            clipped[name] = position
        return clipped

    def __del__(self) -> None:
        try:
            self._shutdown_read_executor()
        except Exception:
            pass
