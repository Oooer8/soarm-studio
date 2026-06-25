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
    fourcc: str | None = None
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
            fourcc=None if data.get("fourcc") is None else str(data["fourcc"]),
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
class RuntimeConfig:
    preflight_required: bool = True

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "RuntimeConfig":
        data = data or {}
        return cls(preflight_required=bool(data.get("preflight_required", True)))


@dataclass(frozen=True)
class RecordingConfig:
    default_seconds: float = 2.0
    warmup: float = 0.0
    episodes: int = 1

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "RecordingConfig":
        data = data or {}
        return cls(
            default_seconds=float(data.get("default_seconds", 2.0)),
            warmup=float(data.get("warmup", 0.0)),
            episodes=int(data.get("episodes", 1)),
        )


@dataclass(frozen=True)
class SyncConfig:
    slow_camera_ms: float = 100.0

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "SyncConfig":
        data = data or {}
        return cls(slow_camera_ms=float(data.get("slow_camera_ms", 100.0)))


@dataclass(frozen=True)
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 8000

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "WebConfig":
        data = data or {}
        return cls(
            host=str(data.get("host", "127.0.0.1")),
            port=int(data.get("port", 8000)),
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
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    web: WebConfig = field(default_factory=WebConfig)

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
            runtime=RuntimeConfig.from_mapping(data.get("runtime")),
            recording=RecordingConfig.from_mapping(data.get("recording")),
            sync=SyncConfig.from_mapping(data.get("sync")),
            web=WebConfig.from_mapping(data.get("web")),
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
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError as exc:
            if path.suffix.lower() in {".yaml", ".yml"}:
                raise RuntimeError(
                    f"{path} is not JSON-compatible YAML, and PyYAML is not installed. "
                    "Install config support with `python -m pip install -e \".[config]\"` "
                    "or use the `soarm-studio` conda environment."
                ) from exc
            raise

    if not isinstance(loaded, Mapping):
        raise ValueError(f"{path} must contain a mapping")
    return dict(loaded)
