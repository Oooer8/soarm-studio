from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .config import load_config_mapping, save_config_mapping
from .hardware.cameras import CameraDeviceInfo, detect_camera_devices


DEFAULT_SESSION_CONFIG = "configs/session.yaml"
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
    if leader_port is not None and follower_port is not None and leader_port == follower_port:
        raise ValueError("Leader and follower ports must be different")

    session_path = Path(session_config)
    session = _load_session_for_assignment(session_path)
    written_arm_configs: dict[str, str] = {}
    warnings: list[str] = []
    copied_from_base = False

    if leader_port is not None:
        path = Path(leader_arm_config)
        data, used_base, source_path = _load_arm_template(
            path=path,
            base_arm_config=base_arm_config,
        )
        _write_arm_config(
            path=path,
            port=leader_port,
            arm_name="soarm-leader",
            data=data,
            source_path=source_path,
            base_arm_config=base_arm_config,
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
        data, used_base, source_path = _load_arm_template(
            path=path,
            base_arm_config=base_arm_config,
        )
        _write_arm_config(
            path=path,
            port=follower_port,
            arm_name="soarm-follower",
            data=data,
            source_path=source_path,
            base_arm_config=base_arm_config,
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
    source_path: Path,
    base_arm_config: str | Path | None,
) -> None:
    _rewrite_soarm_include_paths(
        data,
        target_path=path,
        source_path=source_path,
        fallback_base=_resolve_base_arm_config(base_arm_config),
    )
    arm = dict(data.get("arm") or {})
    arm["name"] = arm_name
    arm["port"] = port
    data["arm"] = arm
    save_config_mapping(path, data)


def _load_arm_template(
    *,
    path: Path,
    base_arm_config: str | Path | None,
) -> tuple[dict[str, Any], bool, Path]:
    if path.exists():
        return load_config_mapping(path), False, path
    base = _resolve_base_arm_config(base_arm_config)
    if base is None:
        raise ValueError(
            f"{path} does not exist; pass --base-arm-config to create it from a calibrated "
            "or template SOARM config"
        )
    return load_config_mapping(base), True, base


def _resolve_base_arm_config(base_arm_config: str | Path | None) -> Path | None:
    if base_arm_config is not None:
        return Path(base_arm_config)
    default = Path(DEFAULT_SOARM_BASE_CONFIG)
    if default.exists():
        return default
    return None


def _rewrite_soarm_include_paths(
    data: dict[str, Any],
    *,
    target_path: Path,
    source_path: Path,
    fallback_base: Path | None,
) -> None:
    includes = data.get("includes")
    if not isinstance(includes, dict):
        return
    rewritten: dict[str, Any] = {}
    for name, ref in includes.items():
        if not isinstance(ref, str) or not ref:
            rewritten[name] = ref
            continue
        resolved = _resolve_include_path(
            ref,
            source_dir=source_path.parent,
            target_dir=target_path.parent,
            fallback_dir=None if fallback_base is None else fallback_base.parent,
        )
        rewritten[name] = _relative_path(resolved, start=target_path.parent) if resolved else ref
    data["includes"] = rewritten


def _resolve_include_path(
    ref: str,
    *,
    source_dir: Path,
    target_dir: Path,
    fallback_dir: Path | None,
) -> Path | None:
    ref_path = Path(ref)
    if ref_path.is_absolute():
        return ref_path if ref_path.exists() else None
    candidates = [target_dir / ref, source_dir / ref]
    if fallback_dir is not None:
        candidates.append(fallback_dir / ref)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _relative_path(path: Path, *, start: Path) -> str:
    return os.path.relpath(path.resolve(), start=start.resolve())


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
