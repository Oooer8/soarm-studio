from __future__ import annotations

from dataclasses import asdict

from soarm_studio.types import ControlSample, RecordingQuality


class RecordingQualityTracker:
    def __init__(self) -> None:
        self.frames = 0
        self.dropped_camera_frames = 0
        self.stale_camera_frames = 0
        self.max_loop_latency_ms = 0.0
        self.max_camera_latency_ms = 0.0
        self.max_camera_age_ms = 0.0
        self.warnings: list[str] = []

    def observe(self, sample: ControlSample) -> None:
        self.frames += 1
        self.max_loop_latency_ms = max(self.max_loop_latency_ms, sample.latency_ms)
        for metric in sample.camera_metrics.values():
            self.max_camera_latency_ms = max(self.max_camera_latency_ms, metric.read_latency_ms)
            if metric.frame_age_ms is not None:
                self.max_camera_age_ms = max(self.max_camera_age_ms, metric.frame_age_ms)
            if not metric.ok:
                self.dropped_camera_frames += 1
                if metric.error == "stale camera frame":
                    self.stale_camera_frames += 1
                warning = metric.error or f"camera {metric.camera} was not OK"
                self.warnings.append(f"frame {sample.frame_index}: {warning}")

    def quality(self) -> RecordingQuality:
        return RecordingQuality(
            frames=self.frames,
            dropped_camera_frames=self.dropped_camera_frames,
            stale_camera_frames=self.stale_camera_frames,
            max_loop_latency_ms=self.max_loop_latency_ms,
            max_camera_latency_ms=self.max_camera_latency_ms,
            max_camera_age_ms=self.max_camera_age_ms,
            warnings=tuple(self.warnings),
        )

    def to_dict(self) -> dict:
        return asdict(self.quality())
