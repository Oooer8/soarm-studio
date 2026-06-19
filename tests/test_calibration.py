from __future__ import annotations

import sys
import types
from pathlib import Path

from soarm_studio.config import ArmEndpointConfig, SessionConfig
from soarm_studio.hardware.calibration import calibrate_session


def _session(config_path: Path) -> SessionConfig:
    return SessionConfig(
        name="test-calibration",
        loop_hz=30,
        joints=[],
        leader=ArmEndpointConfig(name="leader", config=str(config_path)),
        follower=ArmEndpointConfig(name="follower", mock=True),
    )


def _install_fake_sdk(monkeypatch, diagnostics: list[list[str]]) -> dict:
    calls: dict[str, object] = {
        "calibrated": False,
        "exited": False,
    }

    class FakeArm:
        def __init__(self, path: Path) -> None:
            calls["config_path"] = path
            self._diagnostics = list(diagnostics)

        def __enter__(self):
            calls["entered"] = True
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            calls["exited"] = True

        def diagnostics(self) -> list[str]:
            return self._diagnostics.pop(0)

        def calibrate(self, *, output_path, prompt, announce) -> None:
            calls["calibrated"] = True
            calls["output_path"] = output_path
            announce(f"Calibration step 4/4: saved calibrated config to {output_path}.")

    class FakeSOARM:
        @staticmethod
        def from_config(path):
            return FakeArm(Path(path))

    def calibration_ready_from_report(lines: list[str]) -> bool:
        for line in lines:
            if line.startswith("calibration readiness:"):
                return "[PASS]" in line
        return not any("[FAIL]" in line for line in lines)

    package = types.ModuleType("soarm_sdk")
    package.__path__ = []
    package.SOARM = FakeSOARM
    diagnostics_module = types.ModuleType("soarm_sdk.diagnostics")
    diagnostics_module.calibration_ready_from_report = calibration_ready_from_report

    monkeypatch.setitem(sys.modules, "soarm_sdk", package)
    monkeypatch.setitem(sys.modules, "soarm_sdk.diagnostics", diagnostics_module)
    return calls


def test_calibrate_session_blocks_when_pre_calibration_readiness_fails(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "leader.yaml"
    calls = _install_fake_sdk(
        monkeypatch,
        diagnostics=[
            [
                "SOARM status report",
                "summary: [FAIL] one or more checks failed",
                "calibration readiness: [FAIL] hardware is not ready for calibration",
            ],
        ],
    )

    result = calibrate_session(_session(config_path), role="leader")
    endpoint = result["results"][0]

    assert result["ok"] is False
    assert endpoint["ok"] is False
    assert endpoint["error"] == (
        "hardware is not ready for calibration; fix the pre-calibration status failures first"
    )
    assert calls["calibrated"] is False
    assert calls["exited"] is True
    assert endpoint["report"] == [
        "Pre-calibration status:",
        "SOARM status report",
        "summary: [FAIL] one or more checks failed",
        "calibration readiness: [FAIL] hardware is not ready for calibration",
    ]


def test_calibrate_session_fails_when_post_calibration_diagnostics_fail(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "leader.yaml"
    calls = _install_fake_sdk(
        monkeypatch,
        diagnostics=[
            [
                "SOARM status report",
                "summary: [PASS] all checks passed",
                "calibration readiness: [PASS] all required hardware checks are ready",
            ],
            [
                "SOARM status report",
                "summary: [FAIL] one or more checks failed",
                "summary: low voltage: shoulder_pan=7.70V",
            ],
        ],
    )

    result = calibrate_session(_session(config_path), role="leader")
    endpoint = result["results"][0]

    assert result["ok"] is False
    assert endpoint["ok"] is False
    assert endpoint["error"] == "calibration was saved, but the post-calibration status check failed"
    assert calls["calibrated"] is True
    assert calls["output_path"] == config_path
    assert endpoint["report"][-5:] == [
        "Calibration step 4/4: saved calibrated config to " + str(config_path) + ".",
        "Post-calibration status:",
        "SOARM status report",
        "summary: [FAIL] one or more checks failed",
        "summary: low voltage: shoulder_pan=7.70V",
    ]
