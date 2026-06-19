from __future__ import annotations

from dataclasses import asdict
from typing import Callable

from soarm_studio.config import SessionConfig
from soarm_studio.teleop import ControlSample, TeleopLoop
from soarm_studio.types import ArmStatus, PreflightCheck, PreflightReport, RuntimeState

from .arms import Arm, create_arm
from .bindings import arm_config_port, session_bindings, verify_session_bindings
from .cameras import Camera, create_cameras
from .preflight import PreflightBuilder, static_preflight_checks


class HardwareSession:
    _owned_ports: set[str] = set()

    def __init__(self, config: SessionConfig) -> None:
        self.config = config
        self.state = RuntimeState.BOUND
        self.leader: Arm | None = None
        self.follower: Arm | None = None
        self.cameras: dict[str, Camera] = {}
        self.latest_status: dict[str, ArmStatus] = {}
        self.latest_preflight: PreflightReport | None = None
        self._owned: set[str] = set()

    def __enter__(self) -> "HardwareSession":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.disconnect()

    @property
    def connected(self) -> bool:
        return self.state not in {RuntimeState.DISCONNECTED, RuntimeState.BOUND}

    def bindings(self) -> dict:
        return {key: asdict(value) for key, value in session_bindings(self.config).items()}

    def verify_bindings(self) -> dict:
        return verify_session_bindings(self.config)

    def connect(self) -> None:
        if self.connected:
            return
        self._claim_ports()
        try:
            self.leader = create_arm(self.config.leader, self.config.joints, role="leader")
            self.follower = create_arm(self.config.follower, self.config.joints, role="follower")
            self.cameras = create_cameras(self.config.cameras)
            self.leader.connect()
            self.follower.connect()
            for camera in self.cameras.values():
                camera.connect()
            self.state = RuntimeState.CONNECTED
        except Exception:
            self.state = RuntimeState.ERROR
            self._release_ports()
            raise

    def disconnect(self) -> None:
        for camera in self.cameras.values():
            try:
                camera.disconnect()
            except Exception:
                pass
        if self.follower is not None:
            try:
                self.follower.disconnect()
            except Exception:
                pass
        if self.leader is not None:
            try:
                self.leader.disconnect()
            except Exception:
                pass
        self.cameras = {}
        self.leader = None
        self.follower = None
        self._release_ports()
        self.state = RuntimeState.DISCONNECTED

    def read_status(self) -> dict:
        self._require_connected()
        assert self.leader is not None
        assert self.follower is not None
        leader_sample = self.leader.read_joints()
        follower_sample = self.follower.read_joints()
        leader_status = self.leader.status()
        follower_status = self.follower.status()
        self.latest_status = {
            "leader": leader_status,
            "follower": follower_status,
        }
        return {
            "state": self.state.value,
            "session": self.config.name,
            "leader": {
                "status": asdict(leader_status),
                "positions": leader_sample.positions,
                "timestamp": leader_sample.timestamp,
            },
            "follower": {
                "status": asdict(follower_status),
                "positions": follower_sample.positions,
                "timestamp": follower_sample.timestamp,
            },
            "cameras": {
                name: {
                    "name": camera.name,
                    "width": camera.width,
                    "height": camera.height,
                }
                for name, camera in self.cameras.items()
            },
        }

    def preflight(self, *, dataset_overwrite: bool = False) -> PreflightReport:
        static = static_preflight_checks(
            self.config,
            dataset_overwrite=dataset_overwrite,
            state=self.state,
        )
        builder = PreflightBuilder(state=self.state)
        for check in static.checks:
            builder._checks.append(check)
        if not self.connected:
            try:
                self.connect()
            except Exception as exc:
                builder.fail("connect", f"failed to connect hardware: {exc}")
                self.latest_preflight = builder.report()
                return self.latest_preflight

        self._append_runtime_checks(builder)
        report = builder.report()
        if report.ok:
            self.state = RuntimeState.STATUS_OK
            builder.state = self.state
            report = builder.report()
        self.latest_preflight = report
        return report

    def create_loop(
        self,
        *,
        on_sample: Callable[[ControlSample], None] | None = None,
    ) -> TeleopLoop:
        self._require_connected()
        assert self.leader is not None
        assert self.follower is not None
        self.state = RuntimeState.TELEOP_READY
        return TeleopLoop(
            leader=self.leader,
            follower=self.follower,
            joint_names=self.config.joints,
            hz=self.config.loop_hz,
            max_relative_target=self.config.follower.max_relative_target,
            cameras=self.cameras,
            on_sample=on_sample,
            slow_camera_ms=self.config.sync.slow_camera_ms,
        )

    def run_teleop(self, *, seconds: float, on_sample=None) -> dict:
        loop = self.create_loop(on_sample=on_sample)
        self.state = RuntimeState.TELEOP_RUNNING
        try:
            metrics = loop.run(seconds=seconds)
            return {
                "iterations": metrics.iterations,
                "target_hz": metrics.target_hz,
                "observed_hz": round(metrics.observed_hz, 3),
                "last_latency_ms": round(metrics.last_latency_ms, 3),
                "max_latency_ms": round(metrics.max_latency_ms, 3),
                "elapsed_s": round(metrics.elapsed_s, 3),
            }
        finally:
            if self.state != RuntimeState.E_STOP:
                self.state = RuntimeState.TELEOP_READY

    def emergency_stop(self) -> None:
        if self.follower is not None:
            self.follower.emergency_stop()
        self.state = RuntimeState.E_STOP

    def _append_runtime_checks(self, builder: PreflightBuilder) -> None:
        try:
            status = self.read_status()
        except Exception as exc:
            builder.fail("status", f"failed to read arm status: {exc}")
            return
        leader_positions = status["leader"]["positions"]
        follower_positions = status["follower"]["positions"]
        missing_leader = [name for name in self.config.joints if name not in leader_positions]
        missing_follower = [name for name in self.config.joints if name not in follower_positions]
        if missing_leader:
            builder.fail("leader_joints", "leader missing joints: " + ", ".join(missing_leader))
        else:
            builder.pass_("leader_joints", "leader joint order matches session")
        if missing_follower:
            builder.fail("follower_joints", "follower missing joints: " + ", ".join(missing_follower))
        else:
            builder.pass_("follower_joints", "follower joint order matches session")

        for name, camera in self.cameras.items():
            try:
                frame = camera.read()
            except Exception as exc:
                builder.fail(f"camera:{name}:read", f"failed to read camera {name!r}: {exc}")
            else:
                builder.pass_(
                    f"camera:{name}:read",
                    f"read {frame.width}x{frame.height} frame from {name}",
                )

    def _claim_ports(self) -> None:
        ports = [
            arm_config_port(self.config.leader.config) if not self.config.leader.mock else None,
            arm_config_port(self.config.follower.config) if not self.config.follower.mock else None,
        ]
        real_ports = [port for port in ports if port is not None]
        duplicates = sorted({port for port in real_ports if real_ports.count(port) > 1})
        if duplicates:
            raise RuntimeError("leader and follower cannot share serial ports: " + ", ".join(duplicates))
        busy = [port for port in real_ports if port in self._owned_ports]
        if busy:
            raise RuntimeError("serial port already owned by this process: " + ", ".join(busy))
        self._owned_ports.update(real_ports)
        self._owned.update(real_ports)

    def _release_ports(self) -> None:
        for port in self._owned:
            self._owned_ports.discard(port)
        self._owned.clear()

    def _require_connected(self) -> None:
        if not self.connected or self.leader is None or self.follower is None:
            raise RuntimeError("hardware session is not connected")


def preflight_report_to_dict(report: PreflightReport) -> dict:
    return {
        "ok": report.ok,
        "state": report.state.value,
        "checks": [asdict(check) for check in report.checks],
        "errors": report.errors,
        "warnings": report.warnings,
    }


def check_to_dict(check: PreflightCheck) -> dict:
    return asdict(check)
