from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Protocol

from soarm_studio.config import ArmEndpointConfig
from soarm_studio.types import ArmStatus, JointSample


class Arm(Protocol):
    name: str

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def read_joints(self) -> JointSample: ...

    def send_joints(self, targets: dict[str, float]) -> None: ...

    def move_joints(self, targets: dict[str, float], *, duration: float | None = None) -> None: ...

    def start_joint_stream(
        self,
        *,
        output_hz: float | None = None,
        target_timeout_s: float = 0.15,
        joint_names: list[str] | None = None,
        mode: str = "arrival",
        tracking_kp: float = 8.0,
        tracking_feedforward: float = 1.0,
    ) -> "JointStream": ...

    def stop(self) -> None: ...

    def emergency_stop(self) -> None: ...

    def status(self) -> ArmStatus: ...


class JointStream(Protocol):
    def update_target(self, targets: dict[str, float]) -> None: ...

    def stop(self) -> None: ...


class MockJointStream:
    def __init__(self, arm: "MockArm") -> None:
        self.arm = arm
        self.stopped = False

    def update_target(self, targets: dict[str, float]) -> None:
        if self.stopped:
            return
        self.arm.send_joints(targets)

    def stop(self) -> None:
        self.stopped = True


class MockArm:
    def __init__(
        self,
        name: str,
        joint_names: list[str],
        *,
        scripted: bool = False,
    ) -> None:
        self.name = name
        self.joint_names = joint_names
        self.scripted = scripted
        self.connected = False
        self.enabled = False
        self.emergency_stopped = False
        self._started_at = time.monotonic()
        self._positions = {name: 0.0 for name in joint_names}
        self.last_stream_options: dict[str, object] | None = None

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False
        self.enabled = False

    def read_joints(self) -> JointSample:
        self._require_connected()
        if self.scripted and not self.emergency_stopped:
            elapsed = time.monotonic() - self._started_at
            for index, name in enumerate(self.joint_names):
                amplitude = 0.25 if name != "gripper" else 0.1
                self._positions[name] = amplitude * math.sin(elapsed * 0.9 + index * 0.45)
        return JointSample(dict(self._positions))

    def send_joints(self, targets: dict[str, float]) -> None:
        self._require_connected()
        if self.emergency_stopped:
            return
        self.enabled = True
        for name, value in targets.items():
            if name in self._positions:
                self._positions[name] = float(value)

    def move_joints(self, targets: dict[str, float], *, duration: float | None = None) -> None:
        self.send_joints(targets)

    def start_joint_stream(
        self,
        *,
        output_hz: float | None = None,
        target_timeout_s: float = 0.15,
        joint_names: list[str] | None = None,
        mode: str = "arrival",
        tracking_kp: float = 8.0,
        tracking_feedforward: float = 1.0,
    ) -> JointStream:
        self._require_connected()
        self.last_stream_options = {
            "output_hz": output_hz,
            "target_timeout_s": target_timeout_s,
            "joint_names": joint_names,
            "mode": mode,
            "tracking_kp": tracking_kp,
            "tracking_feedforward": tracking_feedforward,
        }
        return MockJointStream(self)

    def stop(self) -> None:
        self.enabled = False

    def emergency_stop(self) -> None:
        self.emergency_stopped = True
        self.enabled = False

    def status(self) -> ArmStatus:
        return ArmStatus(
            name=self.name,
            connected=self.connected,
            enabled=self.enabled,
            emergency_stopped=self.emergency_stopped,
            joints=dict(self._positions),
        )

    def _require_connected(self) -> None:
        if not self.connected:
            raise RuntimeError(f"{self.name} is not connected")


class SOARMArm:
    def __init__(self, name: str, config_path: str | Path) -> None:
        try:
            from soarm_sdk import SOARM
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Cannot import SDK package 'soarm_sdk' from soarm-sdk in this Python "
                "environment. Install or activate soarm-sdk, or use a mock session config."
            ) from exc

        self.name = name
        self._arm = SOARM.from_config(config_path)

    def connect(self) -> None:
        self._arm.connect()

    def disconnect(self) -> None:
        self._arm.disconnect()

    def read_joints(self) -> JointSample:
        return JointSample({k: float(v) for k, v in self._arm.motion.read_positions_rad().items()})

    def send_joints(self, targets: dict[str, float]) -> None:
        self._arm.stream_joints(targets)

    def move_joints(self, targets: dict[str, float], *, duration: float | None = None) -> None:
        if duration is None:
            from soarm_sdk.application import recommended_move_duration

            current = self._arm.motion.read_positions_rad()
            duration = recommended_move_duration(
                self._arm.config,
                current=current,
                target=targets,
                minimum=0.5,
            )
        self._arm.move_joints(targets, duration=duration, wait=True)

    def start_joint_stream(
        self,
        *,
        output_hz: float | None = None,
        target_timeout_s: float = 0.15,
        joint_names: list[str] | None = None,
        mode: str = "arrival",
        tracking_kp: float = 8.0,
        tracking_feedforward: float = 1.0,
    ) -> JointStream:
        return SOARMJointStream(
            self._arm.start_joint_stream(
                output_hz=output_hz,
                target_timeout_s=target_timeout_s,
                joint_names=joint_names,
                mode=mode,
                tracking_kp=tracking_kp,
                tracking_feedforward=tracking_feedforward,
            )
        )

    def stop(self) -> None:
        self._arm.stop()

    def emergency_stop(self) -> None:
        self._arm.emergency_stop()

    def status(self) -> ArmStatus:
        state = self._arm.get_arm_state()
        joints = {
            name: float(joint.position_rad)
            for name, joint in state.joints.items()
            if joint.position_rad is not None
        }
        return ArmStatus(
            name=self.name,
            connected=state.connected,
            enabled=state.enabled,
            emergency_stopped=state.emergency_stopped,
            joints=joints,
        )


class SOARMJointStream:
    def __init__(self, stream) -> None:
        self._stream = stream

    def update_target(self, targets: dict[str, float]) -> None:
        self._stream.update_target(targets)

    def stop(self) -> None:
        self._stream.stop()


def create_arm(config: ArmEndpointConfig, joint_names: list[str], *, role: str) -> Arm:
    if config.mock:
        return MockArm(config.name, joint_names, scripted=config.scripted or role == "leader")
    if config.config is None:
        raise ValueError(f"{role} arm requires a config path unless mock=true")
    return SOARMArm(config.name, config.config)
