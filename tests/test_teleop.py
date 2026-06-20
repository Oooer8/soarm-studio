from __future__ import annotations

import sys
import types

from soarm_studio.config import CameraConfig
from soarm_studio.hardware.arms import MockArm, SOARMArm
from soarm_studio.hardware.cameras import MockCamera
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
        follower_readback_every=1,
    )
    sample = loop.step()

    assert sample.frame_index == 0
    assert sample.follower_before.positions == {"a": 0.0, "b": 0.0}
    assert sample.action == {"a": 0.2, "b": -0.2}
    assert sample.follower_after is not None
    assert sample.follower_after.positions == {"a": 0.2, "b": -0.2}


def test_teleop_step_can_skip_follower_after_readback() -> None:
    joints = ["a", "b"]
    leader = MockArm("leader", joints)
    follower = MockArm("follower", joints)
    leader.connect()
    follower.connect()
    leader.send_joints({"a": 0.5, "b": -0.5})

    loop = TeleopLoop(
        leader=leader,
        follower=follower,
        joint_names=joints,
        hz=30,
        follower_readback_every=0,
    )
    sample = loop.step()

    assert sample.follower_after is None
    assert sample.action == {"a": 0.5, "b": -0.5}
    assert follower.read_joints().positions == {"a": 0.5, "b": -0.5}


def test_teleop_profile_records_phase_latency() -> None:
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
        profile=True,
        sync_start=False,
        follower_readback_every=1,
    )
    metrics = loop.run(steps=2, sleep=False)

    phase_latency = metrics.phase_summary()
    assert phase_latency["leader_read"]["count"] == 2
    assert phase_latency["follower_before_read"]["count"] == 2
    assert phase_latency["stream_update"]["count"] == 2
    assert phase_latency["follower_after_read"]["count"] == 2
    assert set(phase_latency["leader_read"]) == {
        "count",
        "last_ms",
        "avg_ms",
        "p50_ms",
        "p95_ms",
        "max_ms",
    }


def test_teleop_profile_records_camera_frame_age() -> None:
    joints = ["a", "b"]
    leader = MockArm("leader", joints)
    follower = MockArm("follower", joints)
    camera = MockCamera(CameraConfig(name="wrist", width=2, height=2))
    leader.connect()
    follower.connect()
    camera.connect()

    loop = TeleopLoop(
        leader=leader,
        follower=follower,
        joint_names=joints,
        hz=30,
        cameras={"wrist": camera},
        profile=True,
        sync_start=False,
    )
    metrics = loop.run(steps=1, sleep=False)

    phase_latency = metrics.phase_summary()
    assert phase_latency["camera_read:wrist"]["count"] == 1
    assert phase_latency["camera_age:wrist"]["count"] == 1


def test_teleop_default_skips_follower_after_readback() -> None:
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
    sample = loop.step()

    assert sample.follower_after is None


def test_teleop_rejects_negative_follower_readback_every() -> None:
    joints = ["a", "b"]
    leader = MockArm("leader", joints)
    follower = MockArm("follower", joints)

    try:
        TeleopLoop(
            leader=leader,
            follower=follower,
            joint_names=joints,
            hz=30,
            follower_readback_every=-1,
        )
    except ValueError as exc:
        assert "follower_readback_every" in str(exc)
    else:
        raise AssertionError("negative follower_readback_every should fail")


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


def test_teleop_loop_starts_follower_stream_without_mode_surface() -> None:
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
    assert follower.last_stream_options == {
        "output_hz": None,
        "target_timeout_s": 0.15,
        "joint_names": joints,
    }


def test_soarm_arm_pins_sdk_stream_to_direct_mode(monkeypatch, tmp_path) -> None:
    calls: dict[str, object] = {}

    class FakeStream:
        def update_target(self, targets):
            calls["target"] = targets

        def stop(self):
            calls["stopped"] = True

    class FakeArm:
        def start_joint_stream(self, **kwargs):
            calls["kwargs"] = kwargs
            return FakeStream()

    class FakeSOARM:
        @staticmethod
        def from_config(path):
            calls["path"] = path
            return FakeArm()

    package = types.ModuleType("soarm_sdk")
    package.SOARM = FakeSOARM
    monkeypatch.setitem(sys.modules, "soarm_sdk", package)

    arm = SOARMArm("follower", tmp_path / "follower.yaml")
    stream = arm.start_joint_stream(
        output_hz=250,
        target_timeout_s=0.2,
        joint_names=["a"],
    )
    stream.update_target({"a": 0.1})
    stream.stop()

    assert calls["kwargs"] == {
        "output_hz": 250,
        "target_timeout_s": 0.2,
        "joint_names": ["a"],
        "mode": "direct",
    }
    assert calls["target"] == {"a": 0.1}
    assert calls["stopped"] is True
