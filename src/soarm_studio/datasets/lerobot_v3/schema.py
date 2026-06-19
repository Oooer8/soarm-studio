from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CODEBASE_VERSION = "v3.0"

DEFAULT_CHUNK_SIZE = 1000
DEFAULT_DATA_FILE_SIZE_IN_MB = 100
DEFAULT_VIDEO_FILE_SIZE_IN_MB = 200

INFO_PATH = "meta/info.json"
STATS_PATH = "meta/stats.json"
TASKS_PATH = "meta/tasks.parquet"
EPISODES_PATH = "meta/episodes/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet"
DATA_PATH = "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet"
VIDEO_PATH = "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4"

DEFAULT_FEATURES = {
    "timestamp": {"dtype": "float32", "shape": (1,), "names": None},
    "frame_index": {"dtype": "int64", "shape": (1,), "names": None},
    "episode_index": {"dtype": "int64", "shape": (1,), "names": None},
    "index": {"dtype": "int64", "shape": (1,), "names": None},
    "task_index": {"dtype": "int64", "shape": (1,), "names": None},
}


@dataclass
class DatasetInfo:
    codebase_version: str
    fps: int
    features: dict[str, dict[str, Any]]
    total_episodes: int = 0
    total_frames: int = 0
    total_tasks: int = 0
    chunks_size: int = DEFAULT_CHUNK_SIZE
    data_files_size_in_mb: int = DEFAULT_DATA_FILE_SIZE_IN_MB
    video_files_size_in_mb: int = DEFAULT_VIDEO_FILE_SIZE_IN_MB
    data_path: str = DATA_PATH
    video_path: str | None = VIDEO_PATH
    robot_type: str | None = "soarm"
    splits: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        for feature in data["features"].values():
            if isinstance(feature.get("shape"), tuple):
                feature["shape"] = list(feature["shape"])
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DatasetInfo":
        features = data.get("features") or {}
        for feature in features.values():
            if isinstance(feature.get("shape"), list):
                feature["shape"] = tuple(feature["shape"])
        known = {field.name for field in dataclasses.fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})


def make_soarm_features(
    joint_names: list[str],
    cameras: dict[str, tuple[int, int, int]] | None = None,
) -> dict[str, dict[str, Any]]:
    features: dict[str, dict[str, Any]] = {
        "observation.state": {
            "dtype": "float32",
            "shape": (len(joint_names),),
            "names": joint_names,
        },
        "action": {
            "dtype": "float32",
            "shape": (len(joint_names),),
            "names": joint_names,
        },
    }
    for name, shape in (cameras or {}).items():
        features[f"observation.images.{name}"] = {
            "dtype": "video",
            "shape": tuple(shape),
            "names": ["height", "width", "channel"],
            "info": {},
        }
    return {**features, **DEFAULT_FEATURES}


def write_info(info: DatasetInfo, root: Path) -> None:
    write_json(info.to_dict(), root / INFO_PATH)


def read_info(root: Path) -> DatasetInfo:
    with (root / INFO_PATH).open() as handle:
        return DatasetInfo.from_dict(json.load(handle))


def write_json(data: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        json.dump(_jsonable(data), handle, indent=2, sort_keys=True)
        handle.write("\n")


def _jsonable(value):
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value
