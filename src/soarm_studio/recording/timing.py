from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field, replace
from typing import Protocol

from soarm_studio.types import CameraFrame, CameraSyncMetric
from soarm_studio.teleop import ControlSample

from .quality import RecordingQualityTracker


class EpisodeFrameWriter(Protocol):
    def add_frame(
        self,
        *,
        state: dict[str, float],
        action: dict[str, float],
        images: dict[str, CameraFrame],
        timestamp: float,
    ) -> None: ...


@dataclass(frozen=True)
class RecordingTimingCalibration:
    joint_read_lead_ns: int = 0
    camera_receive_to_exposure_shift_ns: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class RecordingPhaseAlignment:
    target_tick_ns: int
    phase_wait_ns: int
    expected_camera_offset_ns: dict[str, int]
    camera_period_ns: dict[str, int]


@dataclass(frozen=True)
class _CameraHistory:
    frames: list[CameraFrame]
    receive_timestamps_ns: list[int]
    estimated_exposure_timestamps_ns: list[int]
    receive_to_exposure_shift_ns: int = 0


@dataclass(frozen=True)
class _CameraPhaseModel:
    name: str
    period_ns: int
    last_estimated_exposure_ns: int


_WARMUP_TIMING_BUCKET_NS = 1_000_000


def write_episode_samples(
    episode: EpisodeFrameWriter,
    samples: list[ControlSample],
    quality: RecordingQualityTracker,
    frame_histories: dict[str, list[CameraFrame]] | None = None,
    timing_calibration: RecordingTimingCalibration | None = None,
) -> dict:
    timing_calibration = timing_calibration or RecordingTimingCalibration()
    histories = {
        name: _camera_history(
            frames,
            receive_to_exposure_shift_ns=timing_calibration.camera_receive_to_exposure_shift_ns.get(name),
        )
        for name, frames in (frame_histories or {}).items()
    }
    samples_to_write, tail_trim = _trim_trailing_samples_to_camera_coverage(samples, histories)
    first_sample_ns: int | None = None
    for sample in samples_to_write:
        sample_time_ns = sample.monotonic_time_ns
        if first_sample_ns is None:
            first_sample_ns = sample_time_ns
        timestamp = (sample_time_ns - first_sample_ns) / 1_000_000_000.0
        matched_sample = _sample_with_matched_camera_frames(sample, histories)
        episode.add_frame(
            state=matched_sample.follower_before.positions,
            action=matched_sample.action,
            images=matched_sample.camera_frames,
            timestamp=timestamp,
        )
        quality.observe(matched_sample)
    return _camera_timing_payload(
        samples_to_write,
        histories,
        first_sample_ns,
        timing_calibration=timing_calibration,
        original_sample_count=len(samples),
        tail_trim=tail_trim,
    )


def timing_calibration_from_warmup(
    samples: list[ControlSample],
    frame_histories: dict[str, list[CameraFrame]],
) -> RecordingTimingCalibration:
    camera_shifts = {}
    for name, frames in frame_histories.items():
        if len(frames) < 2:
            continue
        camera_shifts[name] = _camera_receive_to_exposure_shift_ns(
            [frame.monotonic_time_ns for frame in frames]
        )
    return RecordingTimingCalibration(
        joint_read_lead_ns=_joint_read_lead_from_warmup(samples),
        camera_receive_to_exposure_shift_ns=camera_shifts,
    )


def merge_timing_calibration(
    previous: RecordingTimingCalibration,
    update: RecordingTimingCalibration,
) -> RecordingTimingCalibration:
    camera_shifts = dict(previous.camera_receive_to_exposure_shift_ns)
    camera_shifts.update(update.camera_receive_to_exposure_shift_ns)
    return RecordingTimingCalibration(
        joint_read_lead_ns=update.joint_read_lead_ns or previous.joint_read_lead_ns,
        camera_receive_to_exposure_shift_ns=camera_shifts,
    )


def timing_calibration_to_dict(calibration: RecordingTimingCalibration) -> dict:
    return {
        "joint_read_lead_ms": round(calibration.joint_read_lead_ns / 1_000_000.0, 6),
        "camera_receive_to_estimated_exposure_ms": {
            name: round(shift_ns / 1_000_000.0, 6)
            for name, shift_ns in calibration.camera_receive_to_exposure_shift_ns.items()
        },
    }


def camera_phase_alignment_from_warmup(
    frame_histories: dict[str, list[CameraFrame]],
    timing_calibration: RecordingTimingCalibration,
    *,
    earliest_target_ns: int,
    max_wait_ns: int | None = None,
) -> RecordingPhaseAlignment | None:
    models = _camera_phase_models(frame_histories, timing_calibration)
    if not models:
        return None
    max_period_ns = max(model.period_ns for model in models)
    if max_wait_ns is None:
        max_wait_ns = max_period_ns

    candidates = _phase_alignment_candidates(
        models,
        earliest_target_ns=earliest_target_ns,
        max_wait_ns=max_wait_ns,
    )
    if not candidates:
        return None

    def score(candidate_ns: int) -> tuple[int, float, int]:
        offsets = [
            abs(_nearest_exposure_offset_ns(model, candidate_ns))
            for model in models
        ]
        return (max(offsets), sum(offsets) / len(offsets), candidate_ns)

    target_tick_ns = min(candidates, key=score)
    return RecordingPhaseAlignment(
        target_tick_ns=target_tick_ns,
        phase_wait_ns=max(0, target_tick_ns - earliest_target_ns),
        expected_camera_offset_ns={
            model.name: _nearest_exposure_offset_ns(model, target_tick_ns)
            for model in models
        },
        camera_period_ns={model.name: model.period_ns for model in models},
    )


def phase_alignment_to_dict(alignment: RecordingPhaseAlignment) -> dict:
    return {
        "phase_wait_ms": round(alignment.phase_wait_ns / 1_000_000.0, 6),
        "expected_camera_offset_ms": {
            name: round(offset_ns / 1_000_000.0, 6)
            for name, offset_ns in alignment.expected_camera_offset_ns.items()
        },
        "camera_period_ms": {
            name: round(period_ns / 1_000_000.0, 6)
            for name, period_ns in alignment.camera_period_ns.items()
        },
    }


def _camera_history(
    frames: list[CameraFrame],
    *,
    receive_to_exposure_shift_ns: int | None = None,
) -> _CameraHistory:
    sorted_frames = sorted(frames, key=lambda frame: frame.monotonic_time_ns)
    receive_timestamps_ns = [frame.monotonic_time_ns for frame in sorted_frames]
    if receive_to_exposure_shift_ns is None:
        receive_to_exposure_shift_ns = _camera_receive_to_exposure_shift_ns(receive_timestamps_ns)
    return _CameraHistory(
        frames=sorted_frames,
        receive_timestamps_ns=receive_timestamps_ns,
        estimated_exposure_timestamps_ns=[
            timestamp_ns + receive_to_exposure_shift_ns
            for timestamp_ns in receive_timestamps_ns
        ],
        receive_to_exposure_shift_ns=receive_to_exposure_shift_ns,
    )


def _joint_read_lead_from_warmup(samples: list[ControlSample]) -> int:
    offsets_ns = []
    for sample in samples:
        estimated_sample_ns = sample.follower_before.estimated_sample_monotonic_time_ns
        if estimated_sample_ns is None:
            continue
        offsets_ns.append(estimated_sample_ns - sample.monotonic_time_ns)
    if not offsets_ns:
        return 0
    lead_ns = _dominant_timing_ns(offsets_ns)
    return max(0, lead_ns)


def _camera_receive_to_exposure_shift_ns(timestamps_ns: list[int]) -> int:
    if len(timestamps_ns) < 2:
        return 0
    ordered = sorted(timestamps_ns)
    intervals_ns = _positive_intervals_ns(ordered)
    if not intervals_ns:
        return 0
    return -int(_dominant_timing_ns(intervals_ns) / 2.0)


def _camera_phase_models(
    frame_histories: dict[str, list[CameraFrame]],
    timing_calibration: RecordingTimingCalibration,
) -> list[_CameraPhaseModel]:
    models: list[_CameraPhaseModel] = []
    for name, frames in frame_histories.items():
        receive_timestamps_ns = sorted(frame.monotonic_time_ns for frame in frames)
        intervals_ns = _positive_intervals_ns(receive_timestamps_ns)
        if not intervals_ns:
            continue
        period_ns = _dominant_timing_ns(intervals_ns)
        if period_ns <= 0:
            continue
        shift_ns = timing_calibration.camera_receive_to_exposure_shift_ns.get(name)
        if shift_ns is None:
            shift_ns = _camera_receive_to_exposure_shift_ns(receive_timestamps_ns)
        models.append(
            _CameraPhaseModel(
                name=name,
                period_ns=period_ns,
                last_estimated_exposure_ns=receive_timestamps_ns[-1] + shift_ns,
            )
        )
    return models


def _phase_alignment_candidates(
    models: list[_CameraPhaseModel],
    *,
    earliest_target_ns: int,
    max_wait_ns: int,
) -> set[int]:
    latest_target_ns = earliest_target_ns + max(0, max_wait_ns)
    candidates: set[int] = set()
    for model in models:
        next_exposure_ns = _next_exposure_at_or_after(model, earliest_target_ns)
        for candidate_ns in (next_exposure_ns, next_exposure_ns + model.period_ns):
            if earliest_target_ns <= candidate_ns <= latest_target_ns:
                candidates.add(candidate_ns)

    aggregate_candidates = set(candidates)
    for candidate_ns in candidates:
        nearest_exposures = [
            candidate_ns + _nearest_exposure_offset_ns(model, candidate_ns)
            for model in models
        ]
        mean_exposure_ns = int(round(sum(nearest_exposures) / len(nearest_exposures)))
        if earliest_target_ns <= mean_exposure_ns <= latest_target_ns:
            aggregate_candidates.add(mean_exposure_ns)
    return aggregate_candidates


def _next_exposure_at_or_after(model: _CameraPhaseModel, target_ns: int) -> int:
    if target_ns <= model.last_estimated_exposure_ns:
        return model.last_estimated_exposure_ns
    periods_ahead = (target_ns - model.last_estimated_exposure_ns + model.period_ns - 1) // model.period_ns
    return model.last_estimated_exposure_ns + periods_ahead * model.period_ns


def _nearest_exposure_offset_ns(model: _CameraPhaseModel, target_ns: int) -> int:
    next_exposure_ns = _next_exposure_at_or_after(model, target_ns)
    previous_exposure_ns = next_exposure_ns - model.period_ns
    before_offset_ns = previous_exposure_ns - target_ns
    after_offset_ns = next_exposure_ns - target_ns
    return before_offset_ns if abs(before_offset_ns) <= abs(after_offset_ns) else after_offset_ns


def _positive_intervals_ns(timestamps_ns: list[int]) -> list[int]:
    return [
        timestamps_ns[index] - timestamps_ns[index - 1]
        for index in range(1, len(timestamps_ns))
        if timestamps_ns[index] > timestamps_ns[index - 1]
    ]


def _trim_trailing_samples_to_camera_coverage(
    samples: list[ControlSample],
    frame_histories: dict[str, _CameraHistory],
) -> tuple[list[ControlSample], dict]:
    if len(samples) <= 1 or not frame_histories:
        return samples, {}
    camera_limits = _camera_tail_coverage_limits(frame_histories)
    if not camera_limits:
        return samples, {}

    stop = len(samples)
    while stop > 1:
        target_tick_ns = samples[stop - 1].monotonic_time_ns
        blocking_cameras = [
            name
            for name, limit_ns in camera_limits.items()
            if target_tick_ns > limit_ns
        ]
        if not blocking_cameras:
            break
        stop -= 1

    trimmed_count = len(samples) - stop
    if trimmed_count <= 0:
        return samples, {}

    return samples[:stop], {
        "original_sample_count": len(samples),
        "trimmed_sample_count": trimmed_count,
        "trimmed_frame_indices": [
            sample.frame_index
            for sample in samples[stop:]
        ],
        "reason": "trailing sample target tick exceeded camera coverage",
        "camera_tail_coverage_limit_s": {
            name: round(limit_ns / 1_000_000_000.0, 9)
            for name, limit_ns in camera_limits.items()
        },
    }


def _camera_tail_coverage_limits(
    frame_histories: dict[str, _CameraHistory],
) -> dict[str, int]:
    limits: dict[str, int] = {}
    for name, history in frame_histories.items():
        intervals_ns = _positive_intervals_ns(history.estimated_exposure_timestamps_ns)
        if not intervals_ns:
            continue
        period_ns = _dominant_timing_ns(intervals_ns)
        if period_ns <= 0:
            continue
        limits[name] = history.estimated_exposure_timestamps_ns[-1] + period_ns // 2
    return limits


def _sample_with_matched_camera_frames(
    sample: ControlSample,
    frame_histories: dict[str, _CameraHistory],
) -> ControlSample:
    if not frame_histories:
        return sample

    sample_time_ns = sample.monotonic_time_ns
    frames = dict(sample.camera_frames)
    metrics = dict(sample.camera_metrics)
    for name, history in frame_histories.items():
        matched = _nearest_frame(history, sample_time_ns)
        if matched is None:
            metrics[name] = CameraSyncMetric(
                camera=name,
                ok=False,
                timestamp=None,
                monotonic_time_ns=None,
                read_latency_ms=0.0,
                error="missing camera history",
            )
            continue
        frames[name] = matched
        matched_index = _nearest_frame_index(history, sample_time_ns)
        assert matched_index is not None
        estimated_exposure_time_ns = history.estimated_exposure_timestamps_ns[matched_index]
        offset_ms = abs(estimated_exposure_time_ns - sample_time_ns) / 1_000_000.0
        metrics[name] = CameraSyncMetric(
            camera=name,
            ok=True,
            timestamp=matched.timestamp,
            monotonic_time_ns=matched.monotonic_time_ns,
            read_latency_ms=0.0,
            frame_age_ms=offset_ms,
            width=matched.width,
            height=matched.height,
        )
    return replace(sample, camera_frames=frames, camera_metrics=metrics)


def _nearest_frame(history: _CameraHistory, monotonic_time_ns: int) -> CameraFrame | None:
    index = _nearest_frame_index(history, monotonic_time_ns)
    return None if index is None else history.frames[index]


def _nearest_frame_index(history: _CameraHistory, monotonic_time_ns: int) -> int | None:
    if not history.frames:
        return None
    index = bisect_left(history.estimated_exposure_timestamps_ns, monotonic_time_ns)
    if index <= 0:
        return 0
    if index >= len(history.frames):
        return len(history.frames) - 1
    before_time_ns = history.estimated_exposure_timestamps_ns[index - 1]
    after_time_ns = history.estimated_exposure_timestamps_ns[index]
    before_delta = abs(monotonic_time_ns - before_time_ns)
    after_delta = abs(after_time_ns - monotonic_time_ns)
    return index - 1 if before_delta <= after_delta else index


def _camera_timing_payload(
    samples: list[ControlSample],
    histories: dict[str, _CameraHistory],
    first_sample_ns: int | None,
    *,
    timing_calibration: RecordingTimingCalibration | None = None,
    original_sample_count: int | None = None,
    tail_trim: dict | None = None,
) -> dict:
    timing_calibration = timing_calibration or RecordingTimingCalibration()
    payload = {
        "sample_count": len(samples),
        "timing_model": timing_calibration_to_dict(timing_calibration),
        "joints": {
            "leader": _joint_timing_for_samples(samples, "leader", first_sample_ns),
            "follower_before": _joint_timing_for_samples(
                samples,
                "follower_before",
                first_sample_ns,
            ),
        },
        "cameras": {
            name: _camera_timing_for_history(
                name,
                history,
                samples,
                first_sample_ns,
            )
            for name, history in histories.items()
        },
    }
    if original_sample_count is not None and original_sample_count != len(samples):
        payload["original_sample_count"] = original_sample_count
    if tail_trim:
        payload["tail_trim"] = tail_trim
    return payload


def _joint_timing_for_samples(
    samples: list[ControlSample],
    attribute: str,
    first_sample_ns: int | None,
) -> dict:
    matched_samples: list[dict] = []
    offsets: list[float] = []
    latencies: list[float] = []
    for sample in samples:
        joint_sample = getattr(sample, attribute)
        target_tick_ns = sample.monotonic_time_ns
        estimated_sample_ns = joint_sample.estimated_sample_monotonic_time_ns
        request_start_ns = joint_sample.request_start_monotonic_time_ns
        receive_ns = joint_sample.receive_monotonic_time_ns or joint_sample.monotonic_time_ns
        offset_ms = None
        if estimated_sample_ns is not None:
            offset_ms = (estimated_sample_ns - target_tick_ns) / 1_000_000.0
            offsets.append(abs(offset_ms))
        latency_ms = None
        if request_start_ns is not None:
            latency_ms = (receive_ns - request_start_ns) / 1_000_000.0
            latencies.append(latency_ms)
        matched_samples.append(
            {
                "sample_frame_index": sample.frame_index,
                "target_tick_s": _relative_time_s(target_tick_ns, first_sample_ns),
                "request_start_s": (
                    None
                    if request_start_ns is None
                    else _relative_time_s(request_start_ns, first_sample_ns)
                ),
                "receive_s": _relative_time_s(receive_ns, first_sample_ns),
                "estimated_sample_s": (
                    None
                    if estimated_sample_ns is None
                    else _relative_time_s(estimated_sample_ns, first_sample_ns)
                ),
                "offset_ms": None if offset_ms is None else round(offset_ms, 6),
                "read_latency_ms": None if latency_ms is None else round(latency_ms, 6),
            }
        )
    return {
        "sensor": attribute,
        "matched_samples": matched_samples,
        "estimated_sample_offset_stats_ms": _stats(offsets),
        "read_latency_stats_ms": _stats(latencies),
    }


def _camera_timing_for_history(
    name: str,
    history: _CameraHistory,
    samples: list[ControlSample],
    first_sample_ns: int | None,
) -> dict:
    intervals_ms = [
        (history.receive_timestamps_ns[index] - history.receive_timestamps_ns[index - 1])
        / 1_000_000.0
        for index in range(1, len(history.receive_timestamps_ns))
    ]
    matched_samples: list[dict] = []
    for sample in samples:
        target_tick_ns = sample.monotonic_time_ns
        matched_index = _nearest_frame_index(history, target_tick_ns)
        if matched_index is None:
            matched_samples.append(
                {
                    "sample_frame_index": sample.frame_index,
                    "target_tick_s": _relative_time_s(target_tick_ns, first_sample_ns),
                    "camera_frame_index": None,
                    "camera_receive_s": None,
                    "estimated_exposure_s": None,
                    "offset_ms": None,
                    "receive_offset_ms": None,
                }
            )
            continue
        matched = history.frames[matched_index]
        estimated_exposure_time_ns = history.estimated_exposure_timestamps_ns[matched_index]
        offset_ms = (estimated_exposure_time_ns - target_tick_ns) / 1_000_000.0
        receive_offset_ms = (matched.monotonic_time_ns - target_tick_ns) / 1_000_000.0
        matched_samples.append(
            {
                "sample_frame_index": sample.frame_index,
                "target_tick_s": _relative_time_s(target_tick_ns, first_sample_ns),
                "camera_frame_index": matched_index,
                "camera_receive_s": _relative_time_s(
                    matched.monotonic_time_ns,
                    first_sample_ns,
                ),
                "estimated_exposure_s": _relative_time_s(
                    estimated_exposure_time_ns,
                    first_sample_ns,
                ),
                "offset_ms": round(offset_ms, 6),
                "receive_offset_ms": round(receive_offset_ms, 6),
            }
        )
    offsets = [
        abs(item["offset_ms"])
        for item in matched_samples
        if item["offset_ms"] is not None
    ]
    receive_offsets = [
        item["receive_offset_ms"]
        for item in matched_samples
        if item["receive_offset_ms"] is not None
    ]
    return {
        "camera": name,
        "receive_to_estimated_exposure_ms": round(
            history.receive_to_exposure_shift_ns / 1_000_000.0,
            6,
        ),
        "raw_frame_count": len(history.frames),
        "raw_observed_fps": _observed_fps(history.receive_timestamps_ns),
        "raw_receive_timestamps_s": [
            _relative_time_s(timestamp_ns, first_sample_ns)
            for timestamp_ns in history.receive_timestamps_ns
        ],
        "estimated_exposure_timestamps_s": [
            _relative_time_s(timestamp_ns, first_sample_ns)
            for timestamp_ns in history.estimated_exposure_timestamps_ns
        ],
        "raw_intervals_ms": [round(value, 6) for value in intervals_ms],
        "raw_interval_stats_ms": _stats(intervals_ms),
        "matched_samples": matched_samples,
        "matched_offset_stats_ms": _stats(offsets),
        "receive_offset_stats_ms": _stats(receive_offsets),
    }


def _median(values: list[int]) -> float:
    ordered = sorted(values)
    count = len(ordered)
    middle = count // 2
    if count % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _dominant_timing_ns(
    values: list[int],
    *,
    bucket_ns: int = _WARMUP_TIMING_BUCKET_NS,
) -> int:
    if not values:
        return 0
    if bucket_ns <= 0:
        return int(_median(values))
    buckets: dict[int, list[int]] = {}
    for value in values:
        bucket = round(value / bucket_ns)
        buckets.setdefault(bucket, []).append(value)
    global_median = _median(values)
    max_count = max(len(bucket_values) for bucket_values in buckets.values())
    if max_count <= 1:
        return int(global_median)
    selected_bucket_values = min(
        (bucket_values for bucket_values in buckets.values() if len(bucket_values) == max_count),
        key=lambda bucket_values: abs(_median(bucket_values) - global_median),
    )
    return int(_median(selected_bucket_values))


def _observed_fps(timestamps_ns: list[int]) -> float:
    if len(timestamps_ns) < 2:
        return 0.0
    elapsed_s = (timestamps_ns[-1] - timestamps_ns[0]) / 1_000_000_000.0
    if elapsed_s <= 0:
        return 0.0
    return round((len(timestamps_ns) - 1) / elapsed_s, 6)


def _relative_time_s(monotonic_time_ns: int, first_sample_ns: int | None) -> float:
    if first_sample_ns is None:
        return 0.0
    return round((monotonic_time_ns - first_sample_ns) / 1_000_000_000.0, 9)


def _stats(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"count": 0, "avg": 0.0, "min": 0.0, "max": 0.0}
    return {
        "count": len(values),
        "avg": round(sum(values) / len(values), 6),
        "min": round(min(values), 6),
        "max": round(max(values), 6),
    }
