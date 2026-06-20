from __future__ import annotations

from soarm_studio.config import ArmEndpointConfig, DatasetConfig, SessionConfig
from soarm_studio.hardware.runtime import HardwareSession, preflight_report_to_dict


def _mock_session(tmp_path) -> SessionConfig:
    return SessionConfig(
        name="test-runtime",
        loop_hz=30,
        joints=["a", "b"],
        leader=ArmEndpointConfig(name="leader", mock=True),
        follower=ArmEndpointConfig(name="follower", mock=True, max_relative_target=0.1),
        cameras={},
        dataset=DatasetConfig(root=str(tmp_path / "dataset"), repo_id="local/test", fps=30),
    )


def test_hardware_session_preflight_passes_for_mock_session(tmp_path) -> None:
    with HardwareSession(_mock_session(tmp_path)) as hardware:
        report = hardware.preflight()

    payload = preflight_report_to_dict(report)
    assert payload["ok"] is True
    assert any(check["name"] == "leader_joints" for check in payload["checks"])
    assert any(check["name"] == "follower_joints" for check in payload["checks"])


def test_hardware_session_reports_mock_bindings(tmp_path) -> None:
    hardware = HardwareSession(_mock_session(tmp_path))

    verification = hardware.verify_bindings()

    assert verification["ok"] is True
    assert verification["bindings"]["leader"]["kind"] == "mock"
    assert verification["bindings"]["follower"]["kind"] == "mock"


def test_hardware_session_teleop_profile_includes_phase_latency(tmp_path) -> None:
    with HardwareSession(_mock_session(tmp_path)) as hardware:
        metrics = hardware.run_teleop(
            seconds=0.01,
            profile=True,
        )

    assert "phase_latency_ms" in metrics
    assert "leader_read" in metrics["phase_latency_ms"]
    assert "follower_after_read" not in metrics["phase_latency_ms"]
    assert metrics["budget_ms"] == 33.333
