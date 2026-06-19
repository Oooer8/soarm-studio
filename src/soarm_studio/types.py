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
class LoopMetrics:
    target_hz: float
    iterations: int = 0
    started_at: float = field(default_factory=time.monotonic)
    last_latency_ms: float = 0.0
    max_latency_ms: float = 0.0

    @property
    def elapsed_s(self) -> float:
        return max(time.monotonic() - self.started_at, 1e-9)

    @property
    def observed_hz(self) -> float:
        return self.iterations / self.elapsed_s

    def observe_latency(self, latency_s: float) -> None:
        self.last_latency_ms = latency_s * 1000.0
        self.max_latency_ms = max(self.max_latency_ms, self.last_latency_ms)


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
    max_loop_latency_ms: float = 0.0
    max_camera_latency_ms: float = 0.0
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class EpisodeMetadata:
    episode_index: int
    task: str
    frames: int
    started_at: str
    duration_s: float
    quality: RecordingQuality
