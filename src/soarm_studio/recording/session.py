from __future__ import annotations

from soarm_studio.config import SessionConfig
from soarm_studio.datasets.lerobot_v3 import LeRobotV3Writer


def create_lerobot_writer(config: SessionConfig, *, overwrite: bool = False) -> LeRobotV3Writer:
    camera_shapes = {
        name: (camera.height, camera.width, 3)
        for name, camera in config.cameras.items()
        if camera.enabled
    }
    return LeRobotV3Writer(
        root=config.dataset.root,
        repo_id=config.dataset.repo_id,
        fps=config.dataset.fps,
        joint_names=config.joints,
        cameras=camera_shapes,
        robot_type=config.dataset.robot_type,
        overwrite=overwrite,
    )
