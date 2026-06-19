from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from soarm_studio.config import ArmEndpointConfig, SessionConfig


CalibrationRole = Literal["leader", "follower", "both"]


def calibrate_session(config: SessionConfig, *, role: CalibrationRole) -> dict:
    roles = ["leader", "follower"] if role == "both" else [role]
    results = []
    for item in roles:
        endpoint = config.leader if item == "leader" else config.follower
        results.append(_calibrate_endpoint(item, endpoint))
    return {
        "ok": all(result["ok"] for result in results),
        "role": role,
        "results": results,
    }


def _calibrate_endpoint(role: str, endpoint: ArmEndpointConfig) -> dict:
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

    config_path = Path(endpoint.config)
    report: list[str] = []
    with SOARM.from_config(config_path) as arm:
        pre_status = arm.diagnostics()
        report.extend(["Pre-calibration status:", *pre_status])
        arm.calibrate(
            output_path=config_path,
            prompt=input,
            announce=report.append,
        )
        post_status = arm.diagnostics()
        report.extend(["Post-calibration status:", *post_status])
    return {
        "role": role,
        "ok": True,
        "mock": False,
        "started_at": started_at,
        "config": str(config_path),
        "report": report,
    }
