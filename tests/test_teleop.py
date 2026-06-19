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


def test_teleop_loop_clips_targets_to_follower_joint_limits() -> None:
    joints = ["a", "b"]
    leader = MockArm("leader", joints)
    follower = MockArm(
        "follower",
        joints,
        joint_limits={"a": (-0.25, 0.5), "b": (-0.5, 0.25)},
    )
    leader.connect()
    follower.connect()
    leader.send_joints({"a": 1.0, "b": -1.0})
    follower.send_joints({"a": 0.0, "b": 0.0})

    loop = TeleopLoop(
        leader=leader,
        follower=follower,
        joint_names=joints,
        hz=30,
    )
    sample = loop.step()

    assert sample.action == {"a": 0.5, "b": -0.5}
    assert follower.read_joints().positions == {"a": 0.5, "b": -0.5}


def test_teleop_run_sync_start_clips_to_follower_joint_limits() -> None:
    joints = ["a", "b"]
    leader = MockArm("leader", joints)
    follower = MockArm(
        "follower",
        joints,
        joint_limits={"a": (-0.25, 0.5), "b": (-0.5, 0.25)},
    )
    leader.connect()
    follower.connect()
    leader.send_joints({"a": 1.0, "b": -1.0})
    follower.send_joints({"a": 0.0, "b": 0.0})

    loop = TeleopLoop(
        leader=leader,
        follower=follower,
        joint_names=joints,
        hz=30,
    )
    loop.run(steps=0, sleep=False)

    assert follower.read_joints().positions == {"a": 0.5, "b": -0.5}


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


def test_teleop_run_syncs_start_and_stops_stream() -> None:
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
    metrics = loop.run(steps=1, sleep=False)

    assert metrics.iterations == 1
    assert follower.read_joints().positions == {"a": 1.0, "b": -1.0}
    assert loop._stream is None


def test_teleop_loop_uses_direct_stream_by_default() -> None:
    joints = ["a", "b"]
    leader = MockArm("leader", joints)
    follower = MockArm("follower", joints)
    leader.connect()
    follower.connect()

    loop = TeleopLoop(
        leader=leader,
        follower=follower,
        joint_names=joints,
        hz=30,
    )
    loop.step()

    assert follower.last_stream_options is not None
    assert follower.last_stream_options["mode"] == "direct"
