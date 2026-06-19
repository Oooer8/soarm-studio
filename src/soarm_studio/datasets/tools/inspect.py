from __future__ import annotations

from pathlib import Path

from soarm_studio.datasets.lerobot_v3.deps import require_pyarrow
from soarm_studio.datasets.lerobot_v3.schema import (
    DATA_PATH,
    EPISODES_PATH,
    INFO_PATH,
    TASKS_PATH,
    VIDEO_PATH,
    read_info,
)


def inspect_dataset(root: str | Path) -> dict:
    root = Path(root)
    info = read_info(root)
    summary = {
        "root": str(root),
        "codebase_version": info.codebase_version,
        "fps": info.fps,
        "robot_type": info.robot_type,
        "total_episodes": info.total_episodes,
        "total_frames": info.total_frames,
        "features": list(info.features),
    }
    return summary


def validate_dataset(root: str | Path) -> list[str]:
    root = Path(root)
    errors: list[str] = []
    for relpath in [INFO_PATH, TASKS_PATH]:
        if not (root / relpath).exists():
            errors.append(f"Missing {relpath}")
    try:
        info = read_info(root)
    except Exception as exc:
        return [f"Failed to read {INFO_PATH}: {exc}"]

    _, pq = require_pyarrow(purpose="dataset inspection")
    data_path = root / DATA_PATH.format(chunk_index=0, file_index=0)
    if not data_path.exists():
        errors.append(f"Missing {data_path.relative_to(root)}")
    else:
        try:
            data_rows = pq.read_table(data_path).num_rows
            if data_rows != info.total_frames:
                errors.append(f"Expected {info.total_frames} data rows, found {data_rows}")
        except Exception as exc:
            errors.append(f"Failed to read {data_path.relative_to(root)}: {exc}")

    episodes_path = root / EPISODES_PATH.format(chunk_index=0, file_index=0)
    if not episodes_path.exists():
        errors.append(f"Missing {episodes_path.relative_to(root)}")
        return errors

    try:
        episodes = pq.read_table(episodes_path).to_pylist()
        if len(episodes) != info.total_episodes:
            errors.append(f"Expected {info.total_episodes} episodes, found {len(episodes)}")
    except Exception as exc:
        errors.append(f"Failed to read {episodes_path.relative_to(root)}: {exc}")
        return errors

    video_keys = [key for key, feature in info.features.items() if feature.get("dtype") == "video"]
    for episode in episodes:
        for video_key in video_keys:
            chunk_key = f"videos/{video_key}/chunk_index"
            file_key = f"videos/{video_key}/file_index"
            if chunk_key not in episode or file_key not in episode:
                errors.append(f"Episode {episode.get('episode_index')} missing video metadata for {video_key}")
                continue
            video_path = root / VIDEO_PATH.format(
                video_key=video_key,
                chunk_index=episode[chunk_key],
                file_index=episode[file_key],
            )
            if not video_path.exists():
                errors.append(f"Missing {video_path.relative_to(root)}")

    return errors
