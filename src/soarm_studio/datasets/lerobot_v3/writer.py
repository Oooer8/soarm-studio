from __future__ import annotations

import json
import math
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from soarm_studio.types import CameraFrame

from .deps import require_pyarrow
from .schema import (
    CODEBASE_VERSION,
    DATA_PATH,
    EPISODES_PATH,
    STATS_PATH,
    TASKS_PATH,
    VIDEO_PATH,
    DatasetInfo,
    make_soarm_features,
    write_info,
    write_json,
)


@dataclass
class EpisodeWriter:
    writer: LeRobotV3Writer
    task: str

    def add_frame(
        self,
        *,
        state: Mapping[str, float] | Sequence[float],
        action: Mapping[str, float] | Sequence[float],
        images: dict[str, CameraFrame] | None = None,
        timestamp: float | None = None,
    ) -> None:
        self.writer.add_frame(state=state, action=action, images=images, timestamp=timestamp)

    def save(self) -> int:
        return self.writer.save_episode(self.task)

    def discard(self) -> None:
        self.writer.discard_episode()


class LeRobotV3Writer:
    def __init__(
        self,
        *,
        root: str | Path,
        repo_id: str,
        fps: int,
        joint_names: list[str],
        cameras: dict[str, tuple[int, int, int]] | None = None,
        robot_type: str = "soarm",
        overwrite: bool = False,
    ) -> None:
        self.root = Path(root)
        if overwrite and self.root.exists():
            shutil.rmtree(self.root)
        if self.root.exists() and any(self.root.iterdir()):
            raise FileExistsError(f"Dataset root already exists and is not empty: {self.root}")
        self.root.mkdir(parents=True, exist_ok=True)

        self.repo_id = repo_id
        self.fps = int(fps)
        self.joint_names = list(joint_names)
        self.camera_shapes = cameras or {}
        self.features = make_soarm_features(self.joint_names, self.camera_shapes)
        self.info = DatasetInfo(
            codebase_version=CODEBASE_VERSION,
            fps=self.fps,
            features=self.features,
            robot_type=robot_type,
        )
        write_info(self.info, self.root)

        self._task_to_index: dict[str, int] = {}
        self._episode_buffer: list[dict] = []
        self._episode_video_frames: dict[str, list[CameraFrame]] = {}
        self._episodes: list[dict] = []
        self._data_writer = None
        self._finalized = False

    def __enter__(self) -> "LeRobotV3Writer":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.finalize()

    def start_episode(self, task: str) -> EpisodeWriter:
        if self._finalized:
            raise RuntimeError("Cannot start an episode after finalize")
        if self._episode_buffer:
            raise RuntimeError("Current episode has unsaved frames")
        self._episode_video_frames = {name: [] for name in self.camera_shapes}
        return EpisodeWriter(self, task)

    def add_frame(
        self,
        *,
        state: Mapping[str, float] | Sequence[float],
        action: Mapping[str, float] | Sequence[float],
        images: dict[str, CameraFrame] | None = None,
        timestamp: float | None = None,
    ) -> None:
        frame_index = len(self._episode_buffer)
        episode_index = self.info.total_episodes
        row = {
            "observation.state": self._ordered_vector(state),
            "action": self._ordered_vector(action),
            "timestamp": float(frame_index / self.fps if timestamp is None else timestamp),
            "frame_index": frame_index,
            "episode_index": episode_index,
            "index": self.info.total_frames + frame_index,
        }
        self._episode_buffer.append(row)

        images = images or {}
        for name in self.camera_shapes:
            frame = images.get(name)
            if frame is None:
                raise ValueError(f"Missing frame for camera {name!r}")
            self._episode_video_frames[name].append(frame)

    def save_episode(self, task: str) -> int:
        if not self._episode_buffer:
            raise RuntimeError("Cannot save an empty episode")
        task_index = self._register_task(task)
        for row in self._episode_buffer:
            row["task_index"] = task_index

        episode_index = self.info.total_episodes
        episode_length = len(self._episode_buffer)
        data_metadata = self._write_episode_data(self._episode_buffer)
        video_metadata = self._write_episode_videos(
            episode_index,
            episode_length,
            self._episode_buffer,
        )
        episode_stats = _compute_stats(self._episode_buffer, ["observation.state", "action"])

        episode_row = {
            "episode_index": episode_index,
            "tasks": [task],
            "length": episode_length,
            **data_metadata,
            **video_metadata,
            **_flatten_stats(episode_stats),
        }
        self._episodes.append(episode_row)

        self.info.total_episodes += 1
        self.info.total_frames += episode_length
        self.info.total_tasks = len(self._task_to_index)
        self.info.splits = {"train": f"0:{self.info.total_episodes}"}
        write_info(self.info, self.root)
        self._write_tasks()
        self._write_episodes()
        self._write_stats()

        self._episode_buffer = []
        self._episode_video_frames = {}
        return episode_index

    def discard_episode(self) -> None:
        self._episode_buffer = []
        self._episode_video_frames = {}

    def finalize(self) -> None:
        if self._finalized:
            return
        if self._episode_buffer:
            raise RuntimeError("Cannot finalize while an episode has unsaved frames")
        if self._data_writer is not None:
            self._data_writer.close()
            self._data_writer = None
        write_info(self.info, self.root)
        self._finalized = True

    def _ordered_vector(self, values: Mapping[str, float] | Sequence[float]) -> list[float]:
        if isinstance(values, Mapping):
            return [float(values[name]) for name in self.joint_names]
        vector = [float(item) for item in values]
        if len(vector) != len(self.joint_names):
            raise ValueError(f"Expected {len(self.joint_names)} values, got {len(vector)}")
        return vector

    def _register_task(self, task: str) -> int:
        if task not in self._task_to_index:
            self._task_to_index[task] = len(self._task_to_index)
        return self._task_to_index[task]

    def _write_episode_data(self, rows: list[dict]) -> dict:
        pa, pq = require_pyarrow(purpose="LeRobot v3 dataset writing")
        path = self.root / DATA_PATH.format(chunk_index=0, file_index=0)
        path.parent.mkdir(parents=True, exist_ok=True)

        table = pa.table(
            {
                "observation.state": pa.array(
                    [row["observation.state"] for row in rows],
                    type=pa.list_(pa.float32()),
                ),
                "action": pa.array([row["action"] for row in rows], type=pa.list_(pa.float32())),
                "timestamp": pa.array([row["timestamp"] for row in rows], type=pa.float32()),
                "frame_index": pa.array([row["frame_index"] for row in rows], type=pa.int64()),
                "episode_index": pa.array([row["episode_index"] for row in rows], type=pa.int64()),
                "index": pa.array([row["index"] for row in rows], type=pa.int64()),
                "task_index": pa.array([row["task_index"] for row in rows], type=pa.int64()),
            }
        )
        if self._data_writer is None:
            self._data_writer = pq.ParquetWriter(
                path,
                table.schema,
                compression="snappy",
                use_dictionary=True,
            )
        self._data_writer.write_table(table)
        return {
            "data/chunk_index": 0,
            "data/file_index": 0,
            "dataset_from_index": rows[0]["index"],
            "dataset_to_index": rows[-1]["index"] + 1,
        }

    def _write_episode_videos(
        self,
        episode_index: int,
        episode_length: int,
        rows: list[dict],
    ) -> dict:
        metadata: dict[str, int | float] = {}
        from_timestamp = float(rows[0]["timestamp"]) if rows else 0.0
        to_timestamp = float(rows[-1]["timestamp"]) + (1.0 / self.fps) if rows else 0.0
        for camera_name, frames in self._episode_video_frames.items():
            video_key = f"observation.images.{camera_name}"
            path = self.root / VIDEO_PATH.format(
                video_key=video_key,
                chunk_index=0,
                file_index=episode_index,
            )
            _write_video(path, frames, fps=self.fps)
            self.info.features[video_key]["info"] = {
                "video.fps": self.fps,
                "video.height": frames[0].height if frames else self.camera_shapes[camera_name][0],
                "video.width": frames[0].width if frames else self.camera_shapes[camera_name][1],
                "video.channels": 3,
            }
            metadata[f"videos/{video_key}/chunk_index"] = 0
            metadata[f"videos/{video_key}/file_index"] = episode_index
            metadata[f"videos/{video_key}/from_timestamp"] = from_timestamp
            metadata[f"videos/{video_key}/to_timestamp"] = to_timestamp
        return metadata

    def _write_tasks(self) -> None:
        pa, pq = require_pyarrow(purpose="LeRobot v3 dataset writing")
        path = self.root / TASKS_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        tasks = sorted(self._task_to_index.items(), key=lambda item: item[1])
        table = pa.table(
            {
                "task_index": pa.array([index for _, index in tasks], type=pa.int64()),
                "task": pa.array([task for task, _ in tasks], type=pa.string()),
            }
        )
        table = table.replace_schema_metadata(_pandas_task_index_metadata())
        pq.write_table(table, path, compression="snappy")

    def _write_episodes(self) -> None:
        pa, pq = require_pyarrow(purpose="LeRobot v3 dataset writing")
        if not self._episodes:
            return
        keys = sorted({key for row in self._episodes for key in row})
        columns = {key: [row.get(key) for row in self._episodes] for key in keys}
        path = self.root / EPISODES_PATH.format(chunk_index=0, file_index=0)
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.table(columns), path, compression="snappy")

    def _write_stats(self) -> None:
        stats = _compute_stats_from_episodes(self._episodes)
        write_json(stats, self.root / STATS_PATH)

    def write_session_metadata(self, metadata: dict) -> None:
        write_json(metadata, self.root / "meta" / "soarm_session.json")

    def write_sync_quality(self, quality: dict) -> None:
        write_json(quality, self.root / "meta" / "sync_quality.json")

    def write_episode_quality(self, episode_index: int, quality: dict) -> None:
        path = self.root / "episodes" / f"episode_{episode_index:06d}" / "quality.json"
        write_json(quality, path)

    def write_episode_camera_timing(self, episode_index: int, timing: dict) -> None:
        path = self.root / "episodes" / f"episode_{episode_index:06d}" / "camera_timing.json"
        write_json(timing, path)


def _write_video(path: Path, frames: list[CameraFrame], *, fps: int) -> None:
    if not frames:
        return
    try:
        import cv2  # type: ignore
        import numpy as np
    except ModuleNotFoundError as exc:
        raise RuntimeError("OpenCV and NumPy are required for video writing") from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    first = frames[0]
    writer = cv2.VideoWriter(
        str(path),
        cv2.VideoWriter_fourcc(*"mp4v"),
        float(fps),
        (first.width, first.height),
    )
    if not writer.isOpened():
        raise RuntimeError(f"Failed to open video writer for {path}")

    try:
        for frame in frames:
            rgb = np.frombuffer(frame.rgb, dtype=np.uint8).reshape(frame.height, frame.width, 3)
            bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            writer.write(bgr)
    finally:
        writer.release()


def _compute_stats(rows: list[dict], keys: list[str]) -> dict[str, dict[str, list[float]]]:
    stats: dict[str, dict[str, list[float]]] = {}
    for key in keys:
        values = [row[key] for row in rows]
        if not values:
            continue
        width = len(values[0])
        means: list[float] = []
        mins: list[float] = []
        maxs: list[float] = []
        stds: list[float] = []
        for index in range(width):
            column = [float(value[index]) for value in values]
            mean = sum(column) / len(column)
            variance = sum((value - mean) ** 2 for value in column) / len(column)
            means.append(mean)
            mins.append(min(column))
            maxs.append(max(column))
            stds.append(math.sqrt(variance))
        stats[key] = {"mean": means, "std": stds, "min": mins, "max": maxs}
    return stats


def _compute_stats_from_episodes(episodes: list[dict]) -> dict:
    grouped: dict[str, list[tuple[int, dict[str, list[float]]]]] = {}
    for episode in episodes:
        length = int(episode.get("length") or 0)
        if length <= 0:
            continue
        episode_stats: dict[str, dict[str, list[float]]] = {}
        for key, value in episode.items():
            if not key.startswith("stats/"):
                continue
            _, feature, stat_name = key.split("/", 2)
            episode_stats.setdefault(feature, {})[stat_name] = [float(item) for item in value]
        for feature, stats in episode_stats.items():
            if {"mean", "std", "min", "max"} <= set(stats):
                grouped.setdefault(feature, []).append((length, stats))
    return {
        feature: _aggregate_episode_stats(stats)
        for feature, stats in grouped.items()
        if stats
    }


def _aggregate_episode_stats(
    episodes: list[tuple[int, dict[str, list[float]]]],
) -> dict[str, list[float]]:
    total = sum(length for length, _ in episodes)
    width = len(episodes[0][1]["mean"])
    means: list[float] = []
    stds: list[float] = []
    mins: list[float] = []
    maxs: list[float] = []

    for index in range(width):
        mean = sum(length * stats["mean"][index] for length, stats in episodes) / total
        second_moment = (
            sum(
                length * (stats["std"][index] ** 2 + stats["mean"][index] ** 2)
                for length, stats in episodes
            )
            / total
        )
        means.append(mean)
        stds.append(math.sqrt(max(0.0, second_moment - mean**2)))
        mins.append(min(stats["min"][index] for _, stats in episodes))
        maxs.append(max(stats["max"][index] for _, stats in episodes))

    return {"mean": means, "std": stds, "min": mins, "max": maxs}


def _flatten_stats(stats: dict[str, dict[str, list[float]]]) -> dict[str, list[float]]:
    flattened = {}
    for feature, feature_stats in stats.items():
        for stat_name, value in feature_stats.items():
            flattened[f"stats/{feature}/{stat_name}"] = value
    return flattened


def _pandas_task_index_metadata() -> dict[bytes, bytes]:
    metadata = {
        "index_columns": ["task"],
        "column_indexes": [
            {
                "name": None,
                "field_name": None,
                "pandas_type": "unicode",
                "numpy_type": "object",
                "metadata": {"encoding": "UTF-8"},
            }
        ],
        "columns": [
            {
                "name": "task_index",
                "field_name": "task_index",
                "pandas_type": "int64",
                "numpy_type": "int64",
                "metadata": None,
            },
            {
                "name": "task",
                "field_name": "task",
                "pandas_type": "unicode",
                "numpy_type": "object",
                "metadata": None,
            },
        ],
        "creator": {"library": "soarm-studio", "version": "0.1.0"},
        "pandas_version": "2.0.0",
    }
    return {b"pandas": json.dumps(metadata).encode("utf-8")}
