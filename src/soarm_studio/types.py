from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping


DEFAULT_JOINT_NAMES = (
    "shoulder_pan",
    "shoulder_lift",
    "elbow_flex",
    "wrist_flex",
    "wrist_roll",
    "gripper",
)


@dataclass(frozen=True)
class JointSample:
    positions: dict[str, float]
    timestamp: float = field(default_factory=time.time)
    monotonic_time_ns: int = field(default_factory=time.monotonic_ns)

    def ordered(self, joint_names: list[str] | tuple[str, ...]) -> list[float]:
        return [float(self.positions[name]) for name in joint_names]


@dataclass(frozen=True)
class CameraFrame:
    name: str
    width: int
    height: int
    rgb: bytes
    timestamp: float = field(default_factory=time.time)
    monotonic_time_ns: int = field(default_factory=time.monotonic_ns)


@dataclass(frozen=True)
class ArmStatus:
    name: str
    connected: bool
    enabled: bool
    emergency_stopped: bool
    joints: Mapping[str, float]


@dataclass
class PhaseLatencyStats:
    count: int = 0
    last_ms: float = 0.0
    total_ms: float = 0.0
    max_ms: float = 0.0
    samples_ms: list[float] = field(default_factory=list)

    def observe(self, latency_s: float) -> None:
        latency_ms = latency_s * 1000.0
        self.count += 1
        self.last_ms = latency_ms
        self.total_ms += latency_ms
        self.max_ms = max(self.max_ms, latency_ms)
        self.samples_ms.append(latency_ms)

    @property
    def avg_ms(self) -> float:
        if self.count == 0:
            return 0.0
        return self.total_ms / self.count

    @staticmethod
    def percentile_ms(ordered_samples_ms: list[float], percentile: float) -> float:
        if not ordered_samples_ms:
            return 0.0
        index = int(round((percentile / 100.0) * (len(ordered_samples_ms) - 1)))
        return ordered_samples_ms[max(0, min(len(ordered_samples_ms) - 1, index))]

    def to_dict(self) -> dict[str, float | int]:
        ordered = sorted(self.samples_ms)
        return {
            "count": self.count,
            "last_ms": round(self.last_ms, 3),
            "avg_ms": round(self.avg_ms, 3),
            "p50_ms": round(self.percentile_ms(ordered, 50.0), 3),
            "p95_ms": round(self.percentile_ms(ordered, 95.0), 3),
            "max_ms": round(self.max_ms, 3),
        }


@dataclass
class LoopMetrics:
    target_hz: float
    iterations: int = 0
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    last_latency_ms: float = 0.0
    max_latency_ms: float = 0.0
    over_budget_iterations: int = 0
    profile: bool = False
    phase_latency: dict[str, PhaseLatencyStats] = field(default_factory=dict)

    @property
    def elapsed_s(self) -> float:
        ended_at = time.monotonic() if self.finished_at is None else self.finished_at
        return max(ended_at - self.started_at, 1e-9)

    @property
    def observed_hz(self) -> float:
        return self.iterations / self.elapsed_s

    @property
    def budget_ms(self) -> float:
        return 1000.0 / self.target_hz

    def observe_latency(self, latency_s: float) -> None:
        self.last_latency_ms = latency_s * 1000.0
        self.max_latency_ms = max(self.max_latency_ms, self.last_latency_ms)
        if self.last_latency_ms > self.budget_ms:
            self.over_budget_iterations += 1

    def observe_phase(self, name: str, latency_s: float) -> None:
        if not self.profile:
            return
        self.phase_latency.setdefault(name, PhaseLatencyStats()).observe(latency_s)

    def finish(self, ended_at: float | None = None) -> None:
        self.finished_at = time.monotonic() if ended_at is None else ended_at

    def phase_summary(self) -> dict[str, dict[str, float | int]]:
        return {name: stats.to_dict() for name, stats in self.phase_latency.items()}

    def to_dict(self, *, include_profile: bool = False) -> dict[str, object]:
        payload: dict[str, object] = {
            "iterations": self.iterations,
            "target_hz": self.target_hz,
            "observed_hz": round(self.observed_hz, 3),
            "last_latency_ms": round(self.last_latency_ms, 3),
            "max_latency_ms": round(self.max_latency_ms, 3),
            "elapsed_s": round(self.elapsed_s, 3),
        }
        if include_profile:
            payload.update(
                {
                    "budget_ms": round(self.budget_ms, 3),
                    "over_budget_iterations": self.over_budget_iterations,
                    "phase_latency_ms": self.phase_summary(),
                }
            )
        return payload


class RuntimeState(StrEnum):
    DISCONNECTED = "DISCONNECTED"
    BOUND = "BOUND"
    CONNECTED = "CONNECTED"
    STATUS_OK = "STATUS_OK"
    CALIBRATED = "CALIBRATED"
    TELEOP_READY = "TELEOP_READY"
    TELEOP_RUNNING = "TELEOP_RUNNING"
    RECORDING = "RECORDING"
    SAVED = "SAVED"
    ERROR = "ERROR"
    E_STOP = "E_STOP"


@dataclass(frozen=True)
class HardwareBinding:
    role: str
    kind: str
    device: str | int | None = None
    config: str | None = None
    vid: str | None = None
    pid: str | None = None
    serial_number: str | None = None
    location: str | None = None
    expected_ids: tuple[int, ...] = ()
    last_verified: str | None = None
    ok: bool = True
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreflightCheck:
    name: str
    ok: bool
    detail: str
    severity: str = "error"


@dataclass(frozen=True)
class PreflightReport:
    ok: bool
    checks: tuple[PreflightCheck, ...]
    state: RuntimeState = RuntimeState.DISCONNECTED

    @property
    def errors(self) -> list[str]:
        return [check.detail for check in self.checks if not check.ok and check.severity == "error"]

    @property
    def warnings(self) -> list[str]:
        return [check.detail for check in self.checks if not check.ok and check.severity == "warning"]


@dataclass(frozen=True)
class CameraSyncMetric:
    camera: str
    ok: bool
    timestamp: float | None
    monotonic_time_ns: int | None
    read_latency_ms: float
    frame_age_ms: float | None = None
    width: int | None = None
    height: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class ControlSample:
    frame_index: int
    monotonic_time_ns: int
    leader: JointSample
    follower_before: JointSample
    action: dict[str, float]
    follower_after: JointSample | None
    camera_frames: dict[str, CameraFrame]
    camera_metrics: dict[str, CameraSyncMetric]
    latency_ms: float

    @property
    def state(self) -> dict[str, float]:
        return self.follower_before.positions


@dataclass(frozen=True)
class RecordingQuality:
    frames: int
    dropped_camera_frames: int = 0
    stale_camera_frames: int = 0
    max_loop_latency_ms: float = 0.0
    max_camera_latency_ms: float = 0.0
    max_camera_age_ms: float = 0.0
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class EpisodeMetadata:
    episode_index: int
    task: str
    frames: int
    started_at: str
    duration_s: float
    quality: RecordingQuality
