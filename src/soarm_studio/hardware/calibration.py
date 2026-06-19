from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

from soarm_studio.config import ArmEndpointConfig, SessionConfig


CalibrationRole = Literal["leader", "follower", "both"]
CalibrationAnnounce = Callable[[str, str], None]


def calibrate_session(
    config: SessionConfig,
    *,
    role: CalibrationRole,
    announce: CalibrationAnnounce | None = None,
) -> dict:
    roles = ["leader", "follower"] if role == "both" else [role]
    results = []
    for item in roles:
        endpoint = config.leader if item == "leader" else config.follower
        results.append(_calibrate_endpoint(item, endpoint, announce=announce))
    return {
        "ok": all(result["ok"] for result in results),
        "role": role,
        "results": results,
    }


def _calibrate_endpoint(
    role: str,
    endpoint: ArmEndpointConfig,
    *,
    announce: CalibrationAnnounce | None,
) -> dict:
    started_at = datetime.now(timezone.utc).isoformat()
    if endpoint.mock:
        return {
            "role": role,
            "ok": True,
            "mock": True,
            "started_at": started_at,
            "message": "mock calibration completed",
        }
    if endpoint.config is None:
        return {
            "role": role,
            "ok": False,
            "mock": False,
            "started_at": started_at,
            "error": f"{role} requires a config path unless mock=true",
        }

    try:
        from soarm_sdk import SOARM
    except ModuleNotFoundError as exc:
        return {
            "role": role,
            "ok": False,
            "mock": False,
            "started_at": started_at,
            "error": f"Cannot import SDK package 'soarm_sdk' from soarm-sdk: {exc}",
        }
    try:
        from soarm_sdk.diagnostics import calibration_ready_from_report
    except (ImportError, ModuleNotFoundError):
        calibration_ready_from_report = _calibration_ready_from_report

    config_path = Path(endpoint.config)
    report: list[str] = []

    def record(message: str) -> None:
        report.append(message)
        if announce is not None:
            announce(role, message)

    with SOARM.from_config(config_path) as arm:
        pre_status = arm.diagnostics()
        report.extend(["Pre-calibration status:", *pre_status])
        if not calibration_ready_from_report(pre_status):
            return {
                "role": role,
                "ok": False,
                "mock": False,
                "started_at": started_at,
                "config": str(config_path),
                "error": "hardware is not ready for calibration; fix the pre-calibration status failures first",
                "report": report,
            }
        arm.calibrate(
            output_path=config_path,
            prompt=input,
            announce=record,
        )
        post_status = arm.diagnostics()
        report.extend(["Post-calibration status:", *post_status])
    post_ok = _diagnostics_passed(post_status)
    result = {
        "role": role,
        "ok": post_ok,
        "mock": False,
        "started_at": started_at,
        "config": str(config_path),
        "report": report,
    }
    if not post_ok:
        result["error"] = "calibration was saved, but the post-calibration status check failed"
    return result


def _diagnostics_passed(lines: list[str]) -> bool:
    return not any("[FAIL]" in line for line in lines)


def _calibration_ready_from_report(lines: list[str]) -> bool:
    for line in lines:
        clean = _strip_ansi(line)
        if clean.startswith("calibration readiness:"):
            return "[PASS]" in clean
    return _diagnostics_passed(lines)


def _strip_ansi(line: str) -> str:
    for code in ("\033[32m", "\033[31m", "\033[0m"):
        line = line.replace(code, "")
    return line
