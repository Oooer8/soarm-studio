from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import load_config_mapping, save_config_mapping
from .hardware.cameras import CameraDeviceInfo, detect_camera_devices


DEFAULT_SESSION_CONFIG = "configs/sessions/local.yaml"
DEFAULT_SESSION_TEMPLATE = "configs/sessions/dual_soarm.example.yaml"
DEFAULT_LEADER_ARM_CONFIG = "configs/arms/leader.yaml"
DEFAULT_FOLLOWER_ARM_CONFIG = "configs/arms/follower.yaml"
DEFAULT_SOARM_BASE_CONFIG = "../soarm/configs/soarm.yaml"


def assign_arm_roles(
    *,
    session_config: str | Path,
    leader_port: str | None,
    follower_port: str | None,
    base_arm_config: str | Path | None = None,
    leader_arm_config: str | Path = DEFAULT_LEADER_ARM_CONFIG,
    follower_arm_config: str | Path = DEFAULT_FOLLOWER_ARM_CONFIG,
    max_relative_target: float | None = None,
) -> dict[str, Any]:
    if leader_port is None and follower_port is None:
        raise ValueError("At least one of leader_port or follower_port is required")

    session_path = Path(session_config)
    session = _load_session_for_assignment(session_path)
    written_arm_configs: dict[str, str] = {}
    warnings: list[str] = []
    copied_from_base = False

    if leader_port is not None:
        path = Path(leader_arm_config)
        data, used_base = _load_arm_template(path=path, base_arm_config=base_arm_config)
        _write_arm_config(
            path=path,
            port=leader_port,
            arm_name="soarm-leader",
            data=data,
        )
        copied_from_base = copied_from_base or used_base
        session["leader"] = {
            **dict(session.get("leader") or {}),
            "name": "leader",
            "config": str(path),
            "mock": False,
        }
        written_arm_configs["leader"] = str(path)

    if follower_port is not None:
        path = Path(follower_arm_config)
        data, used_base = _load_arm_template(path=path, base_arm_config=base_arm_config)
        _write_arm_config(
            path=path,
            port=follower_port,
            arm_name="soarm-follower",
            data=data,
        )
        copied_from_base = copied_from_base or used_base
        follower = {
            **dict(session.get("follower") or {}),
            "name": "follower",
            "config": str(path),
            "mock": False,
        }
        if max_relative_target is not None:
            follower["max_relative_target"] = float(max_relative_target)
        session["follower"] = follower
        written_arm_configs["follower"] = str(path)

    save_config_mapping(session_path, session)
    if copied_from_base:
        warnings.append(
            "Arm configs were copied from a base config; calibrate leader and follower separately "
            "before real motion."
        )
    return {
        "session_config": str(session_path),
        "arm_configs": written_arm_configs,
        "warnings": warnings,
    }


def assign_camera_roles(
    *,
    session_config: str | Path,
    wrist_index: int | None,
    third_person_index: int | None,
    width: int = 640,
    height: int = 480,
    fps: int = 30,
    backend: str = "auto",
    use_detected_match: bool = True,
) -> dict[str, Any]:
    if wrist_index is None and third_person_index is None:
        raise ValueError("At least one of wrist_index or third_person_index is required")

    session_path = Path(session_config)
    session = _load_session_for_assignment(session_path)
    cameras = dict(session.get("cameras") or {})
    detected = detect_camera_devices() if use_detected_match else []

    assigned: dict[str, dict[str, Any]] = {}
    if wrist_index is not None:
        cameras["wrist"] = _camera_role_config(
            index=wrist_index,
            width=width,
            height=height,
            fps=fps,
            backend=backend,
            detected=detected,
        )
        assigned["wrist"] = cameras["wrist"]
    if third_person_index is not None:
        cameras["third_person"] = _camera_role_config(
            index=third_person_index,
            width=width,
            height=height,
            fps=fps,
            backend=backend,
            detected=detected,
        )
        assigned["third_person"] = cameras["third_person"]

    session["cameras"] = cameras
    save_config_mapping(session_path, session)
    return {
        "session_config": str(session_path),
        "assigned": assigned,
        "warnings": [
            "OpenCV indexes can change; confirm roles with preview. USB metadata is saved only "
            "when the mapping is unambiguous."
        ],
    }


def _load_session_for_assignment(path: Path) -> dict[str, Any]:
    if path.exists():
        return load_config_mapping(path)
    template = Path(DEFAULT_SESSION_TEMPLATE)
    if template.exists():
        return load_config_mapping(template)
    raise ValueError(f"{path} does not exist; pass --config pointing at a session config")


def _write_arm_config(
    *,
    path: Path,
    port: str,
    arm_name: str,
    data: dict[str, Any],
) -> None:
    arm = dict(data.get("arm") or {})
    arm["name"] = arm_name
    arm["port"] = port
    data["arm"] = arm
    save_config_mapping(path, data)


def _load_arm_template(
    *,
    path: Path,
    base_arm_config: str | Path | None,
) -> tuple[dict[str, Any], bool]:
    if path.exists():
        return load_config_mapping(path), False
    base = _resolve_base_arm_config(base_arm_config)
    if base is None:
        raise ValueError(
            f"{path} does not exist; pass --base-arm-config to create it from a calibrated "
            "or template SOARM config"
        )
    return load_config_mapping(base), True


def _resolve_base_arm_config(base_arm_config: str | Path | None) -> Path | None:
    if base_arm_config is not None:
        return Path(base_arm_config)
    default = Path(DEFAULT_SOARM_BASE_CONFIG)
    if default.exists():
        return default
    return None


def _camera_role_config(
    *,
    index: int,
    width: int,
    height: int,
    fps: int,
    backend: str,
    detected: list[CameraDeviceInfo],
) -> dict[str, Any]:
    return {
        "enabled": True,
        "kind": "opencv",
        "device": int(index),
        "width": int(width),
        "height": int(height),
        "fps": int(fps),
        "backend": backend,
        "match": _camera_match_for_index(index, detected),
    }


def _camera_match_for_index(index: int, detected: list[CameraDeviceInfo]) -> dict[str, Any]:
    if len(detected) != 1 or index != 0:
        return {}
    camera = detected[index]
    return {
        key: value
        for key, value in {
            "name": camera.name,
            "vid": camera.vid,
            "pid": camera.pid,
            "location_id": camera.location_id,
            "source": camera.source,
        }.items()
        if value is not None
    }
