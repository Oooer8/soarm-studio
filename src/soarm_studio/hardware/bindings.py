from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from soarm_studio.config import CameraConfig, SessionConfig, load_config_mapping
from soarm_studio.types import HardwareBinding

from .cameras import detect_camera_devices
from .ports import SerialPortInfo, detect_serial_ports


def session_bindings(config: SessionConfig) -> dict[str, HardwareBinding]:
    bindings: dict[str, HardwareBinding] = {}
    bindings["leader"] = _arm_binding("leader", config.leader.config, mock=config.leader.mock)
    bindings["follower"] = _arm_binding(
        "follower",
        config.follower.config,
        mock=config.follower.mock,
    )
    for name, camera in config.cameras.items():
        if camera.enabled:
            bindings[f"camera:{name}"] = _camera_binding(name, camera)
    return bindings


def verify_session_bindings(config: SessionConfig) -> dict[str, Any]:
    detected_ports = detect_serial_ports(include_system=True)
    port_by_device = {port.device: port for port in detected_ports}
    detected_cameras = detect_camera_devices()
    camera_matches = [_camera_match_key(camera) for camera in detected_cameras]
    verified_at = datetime.now(timezone.utc).isoformat()

    bindings = session_bindings(config)
    results: dict[str, dict[str, Any]] = {}
    for key, binding in bindings.items():
        if binding.kind == "mock":
            verified = HardwareBinding(
                **{
                    **asdict(binding),
                    "last_verified": verified_at,
                    "ok": True,
                    "notes": ("mock hardware binding",),
                }
            )
        elif binding.kind == "arm":
            port = port_by_device.get(str(binding.device))
            verified = _verify_arm_binding(binding, port, verified_at)
        elif binding.kind == "camera":
            match_key = _camera_binding_match_key(binding)
            ok = match_key is None or match_key in camera_matches
            notes = () if ok else ("configured camera metadata was not detected",)
            verified = HardwareBinding(
                **{
                    **asdict(binding),
                    "last_verified": verified_at,
                    "ok": ok,
                    "notes": notes,
                }
            )
        else:
            verified = binding
        results[key] = asdict(verified)
    return {
        "ok": all(result["ok"] for result in results.values()),
        "verified_at": verified_at,
        "bindings": results,
    }


def arm_config_port(config_path: str | Path | None) -> str | None:
    if config_path is None:
        return None
    path = Path(config_path)
    if not path.exists():
        return None
    data = load_config_mapping(path)
    arm = data.get("arm") or {}
    port = arm.get("port")
    return None if port is None else str(port)


def arm_config_servo_ids(config_path: str | Path | None) -> tuple[int, ...]:
    if config_path is None:
        return ()
    path = Path(config_path)
    if not path.exists():
        return ()
    data = load_config_mapping(path)
    joints = data.get("joints") or {}
    ids: list[int] = []
    if isinstance(joints, dict):
        for value in joints.values():
            if isinstance(value, dict) and value.get("id") is not None:
                ids.append(int(value["id"]))
    return tuple(ids)


def _arm_binding(role: str, config_path: str | None, *, mock: bool) -> HardwareBinding:
    if mock:
        return HardwareBinding(role=role, kind="mock", config=config_path)
    port = arm_config_port(config_path)
    return HardwareBinding(
        role=role,
        kind="arm",
        device=port,
        config=config_path,
        expected_ids=arm_config_servo_ids(config_path),
        ok=port is not None,
        notes=() if port is not None else ("arm config has no serial port",),
    )


def _camera_binding(role: str, camera: CameraConfig) -> HardwareBinding:
    match = camera.match or {}
    return HardwareBinding(
        role=role,
        kind="camera",
        device=camera.device,
        vid=match.get("vid"),
        pid=match.get("pid"),
        location=match.get("location_id"),
        ok=True,
    )


def _verify_arm_binding(
    binding: HardwareBinding,
    port: SerialPortInfo | None,
    verified_at: str,
) -> HardwareBinding:
    if port is None:
        return HardwareBinding(
            **{
                **asdict(binding),
                "last_verified": verified_at,
                "ok": False,
                "notes": (f"configured port {binding.device!r} was not detected",),
            }
        )
    notes = []
    if not port.preferred_for_connection:
        notes.append("configured serial device is not the preferred callout port")
    return HardwareBinding(
        **{
            **asdict(binding),
            "vid": port.vid,
            "pid": port.pid,
            "serial_number": port.serial_number,
            "location": port.location,
            "last_verified": verified_at,
            "ok": True,
            "notes": tuple(notes),
        }
    )


def _camera_match_key(camera) -> tuple[str | None, str | None, str | None]:
    return (camera.vid, camera.pid, camera.location_id)


def _camera_binding_match_key(binding: HardwareBinding) -> tuple[str | None, str | None, str | None] | None:
    if binding.vid is None and binding.pid is None and binding.location is None:
        return None
    return (binding.vid, binding.pid, binding.location)
