from __future__ import annotations

from pathlib import Path

from soarm_studio.config import SessionConfig
from soarm_studio.types import PreflightCheck, PreflightReport, RuntimeState


class PreflightBuilder:
    def __init__(self, *, state: RuntimeState) -> None:
        self.state = state
        self._checks: list[PreflightCheck] = []

    def pass_(self, name: str, detail: str) -> None:
        self._checks.append(PreflightCheck(name=name, ok=True, detail=detail))

    def fail(self, name: str, detail: str, *, severity: str = "error") -> None:
        self._checks.append(PreflightCheck(name=name, ok=False, detail=detail, severity=severity))

    def report(self) -> PreflightReport:
        ok = not any(not check.ok and check.severity == "error" for check in self._checks)
        return PreflightReport(ok=ok, checks=tuple(self._checks), state=self.state)


def static_preflight_checks(
    config: SessionConfig,
    *,
    dataset_overwrite: bool = False,
    state: RuntimeState = RuntimeState.DISCONNECTED,
) -> PreflightReport:
    builder = PreflightBuilder(state=state)
    if config.loop_hz <= 0:
        builder.fail("loop_hz", "loop_hz must be positive")
    else:
        builder.pass_("loop_hz", f"loop_hz={config.loop_hz}")

    if not config.joints:
        builder.fail("joints", "session has no configured joints")
    else:
        builder.pass_("joints", f"{len(config.joints)} joints configured")

    if not config.leader.mock and config.leader.config is None:
        builder.fail("leader_config", "leader requires a config path unless mock=true")
    else:
        builder.pass_("leader_config", "leader endpoint is configured")

    if not config.follower.mock and config.follower.config is None:
        builder.fail("follower_config", "follower requires a config path unless mock=true")
    else:
        builder.pass_("follower_config", "follower endpoint is configured")

    for name, camera in config.cameras.items():
        if not camera.enabled:
            continue
        if camera.width <= 0 or camera.height <= 0:
            builder.fail(f"camera:{name}", f"camera {name!r} has invalid dimensions")
        elif camera.fps <= 0:
            builder.fail(f"camera:{name}", f"camera {name!r} fps must be positive")
        else:
            builder.pass_(
                f"camera:{name}",
                f"{camera.kind} {camera.width}x{camera.height}@{camera.fps}",
            )

    root = Path(config.dataset.root)
    if root.exists() and any(root.iterdir()) and not dataset_overwrite:
        builder.fail(
            "dataset_root",
            f"dataset root already exists and is not empty: {root}",
            severity="warning",
        )
    else:
        builder.pass_("dataset_root", f"dataset root is usable: {root}")

    if config.dataset.fps <= 0:
        builder.fail("dataset_fps", "dataset fps must be positive")
    else:
        builder.pass_("dataset_fps", f"dataset fps={config.dataset.fps}")

    return builder.report()
