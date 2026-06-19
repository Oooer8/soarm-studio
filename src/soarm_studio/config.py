from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .types import DEFAULT_JOINT_NAMES


@dataclass(frozen=True)
class ArmEndpointConfig:
    name: str
    config: str | None = None
    mock: bool = False
    scripted: bool = False
    max_relative_target: float | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any], *, default_name: str) -> "ArmEndpointConfig":
        return cls(
            name=str(data.get("name", default_name)),
            config=data.get("config"),
            mock=bool(data.get("mock", False)),
            scripted=bool(data.get("scripted", False)),
            max_relative_target=(
                None
                if data.get("max_relative_target") is None
                else float(data["max_relative_target"])
            ),
        )


@dataclass(frozen=True)
class CameraConfig:
    name: str
    enabled: bool = True
    kind: str = "mock"
    width: int = 640
    height: int = 480
    fps: int = 30
    device: int | str | None = None
    backend: str = "auto"
    match: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, name: str, data: Mapping[str, Any]) -> "CameraConfig":
        return cls(
            name=name,
            enabled=bool(data.get("enabled", True)),
            kind=str(data.get("kind", "mock")),
            width=int(data.get("width", 640)),
            height=int(data.get("height", 480)),
            fps=int(data.get("fps", 30)),
            device=data.get("device"),
            backend=str(data.get("backend", "auto")),
            match=dict(data.get("match") or {}),
        )


@dataclass(frozen=True)
class DatasetConfig:
    root: str = "datasets/soarm"
    repo_id: str = "local/soarm"
    fps: int = 30
    robot_type: str = "soarm"

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "DatasetConfig":
        data = data or {}
        return cls(
            root=str(data.get("root", "datasets/soarm")),
            repo_id=str(data.get("repo_id", "local/soarm")),
            fps=int(data.get("fps", 30)),
            robot_type=str(data.get("robot_type", "soarm")),
        )


@dataclass(frozen=True)
class SessionConfig:
    name: str
    loop_hz: int
    joints: list[str]
    leader: ArmEndpointConfig
    follower: ArmEndpointConfig
    cameras: dict[str, CameraConfig] = field(default_factory=dict)
    dataset: DatasetConfig = field(default_factory=DatasetConfig)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "SessionConfig":
        joints = [str(item) for item in data.get("joints", DEFAULT_JOINT_NAMES)]
        cameras = {
            name: CameraConfig.from_mapping(name, value)
            for name, value in (data.get("cameras") or {}).items()
        }
        return cls(
            name=str(data.get("name", "soarm-session")),
            loop_hz=int(data.get("loop_hz", 30)),
            joints=joints,
            leader=ArmEndpointConfig.from_mapping(data.get("leader") or {}, default_name="leader"),
            follower=ArmEndpointConfig.from_mapping(
                data.get("follower") or {}, default_name="follower"
            ),
            cameras=cameras,
            dataset=DatasetConfig.from_mapping(data.get("dataset")),
        )


def load_session_config(path: str | Path) -> SessionConfig:
    path = Path(path)
    data = _load_mapping(path)
    return SessionConfig.from_mapping(data)


def load_config_mapping(path: str | Path) -> dict[str, Any]:
    return _load_mapping(Path(path))


def save_config_mapping(path: str | Path, data: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(data), indent=2, sort_keys=False) + "\n")


def _load_mapping(path: Path) -> dict[str, Any]:
    text = path.read_text()
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError:
        yaml = None

    if yaml is not None:
        loaded = yaml.safe_load(text) or {}
    else:
        loaded = json.loads(text)

    if not isinstance(loaded, Mapping):
        raise ValueError(f"{path} must contain a mapping")
    return dict(loaded)
