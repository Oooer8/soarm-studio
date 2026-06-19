from __future__ import annotations

import time
from dataclasses import dataclass, field
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

    def ordered(self, joint_names: list[str] | tuple[str, ...]) -> list[float]:
        return [float(self.positions[name]) for name in joint_names]


@dataclass(frozen=True)
class CameraFrame:
    name: str
    width: int
    height: int
    rgb: bytes
    timestamp: float = field(default_factory=time.time)


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
