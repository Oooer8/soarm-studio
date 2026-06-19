from __future__ import annotations

from soarm_studio.hardware.arms import MockArm
from soarm_studio.teleop import TeleopLoop


def test_teleop_loop_clips_relative_targets() -> None:
    joints = ["a", "b"]
    leader = MockArm("leader", joints)
    follower = MockArm("follower", joints)
    leader.connect()
    follower.connect()
    leader.send_joints({"a": 1.0, "b": -1.0})
    follower.send_joints({"a": 0.0, "b": 0.0})

    loop = TeleopLoop(
        leader=leader,
        follower=follower,
        joint_names=joints,
        hz=30,
        max_relative_target=0.1,
    )
    loop.step()

    positions = follower.read_joints().positions
    assert positions == {"a": 0.1, "b": -0.1}


def test_teleop_step_returns_control_sample_with_pre_action_state() -> None:
    joints = ["a", "b"]
    leader = MockArm("leader", joints)
    follower = MockArm("follower", joints)
    leader.connect()
    follower.connect()
    leader.send_joints({"a": 0.5, "b": -0.5})
    follower.send_joints({"a": 0.0, "b": 0.0})

    loop = TeleopLoop(
        leader=leader,
        follower=follower,
        joint_names=joints,
        hz=30,
        max_relative_target=0.2,
    )
    sample = loop.step()

    assert sample.frame_index == 0
    assert sample.follower_before.positions == {"a": 0.0, "b": 0.0}
    assert sample.action == {"a": 0.2, "b": -0.2}
    assert sample.follower_after is not None
    assert sample.follower_after.positions == {"a": 0.2, "b": -0.2}
