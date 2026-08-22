"""Robust local estimates for user-consented pointer telemetry.

For every rolling window this module uses a Theil--Sen median slope for drift
and median absolute deviation (MAD) of Brownian innovations for diffusion.
It intentionally never produces one global arithmetic-average parameter for a
whole recording.  The output is a sequence of local regimes, preserving the
session's variation/divergence.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Iterable
import math


@dataclass(frozen=True)
class Point:
    t_seconds: float
    x: float
    y: float


def _median(values: list[float]) -> float:
    """Use a robust centre; this deliberately replaces arithmetic averaging."""
    if not values:
        raise ValueError("at least one value is required")
    return float(median(values))


def theil_sen_slope(times: list[float], values: list[float]) -> float:
    """Robust velocity estimate based on the median of pairwise slopes."""
    if len(times) != len(values) or len(times) < 2:
        raise ValueError("need at least two equally-sized time/value samples")
    slopes = [
        (values[j] - values[i]) / (times[j] - times[i])
        for i in range(len(times) - 1)
        for j in range(i + 1, len(times))
        if times[j] > times[i]
    ]
    return _median(slopes)


def mad_scale(values: list[float]) -> float:
    """Normal-consistent robust scale, using median rather than mean/variance."""
    centre = _median(values)
    return 1.4826 * _median([abs(value - centre) for value in values])


def fit_regimes(points: Iterable[Point], *, window: int = 80,
                stride: int = 40, min_dt: float = 0.001) -> list[dict]:
    """Return a time-ordered, local drift/diffusion schedule.

    ``drift_samples`` is retained for every valid increment, while each
    ``diffusion`` value describes only its own window.  Consumers should sample
    a regime by timestamp/window, never collapse them into a session average.
    
    Implementation note: every window stays independent. Retaining regimes and
    per-increment drift samples preserves changing/diverging characteristics
    for analysis; this module has no cursor-control or replay path.
    """
    data = sorted(points, key=lambda point: point.t_seconds)
    if window < 4:
        raise ValueError("window must contain at least four points")
    if stride < 1:
        raise ValueError("stride must be positive")
    if len(data) < window:
        raise ValueError("trace is shorter than the requested window")

    regimes: list[dict] = []
    for start in range(0, len(data) - window + 1, stride):
        segment = data[start:start + window]
        times = [point.t_seconds for point in segment]
        drift_x = theil_sen_slope(times, [point.x for point in segment])
        drift_y = theil_sen_slope(times, [point.y for point in segment])
        innovations_x: list[float] = []
        innovations_y: list[float] = []
        drift_samples: list[dict] = []
        for left, right in zip(segment, segment[1:]):
            dt = right.t_seconds - left.t_seconds
            if dt < min_dt:
                continue
            dx, dy = right.x - left.x, right.y - left.y
            drift_samples.append({"t_seconds": right.t_seconds, "x": dx / dt, "y": dy / dt})
            root_dt = math.sqrt(dt)
            innovations_x.append((dx - drift_x * dt) / root_dt)
            innovations_y.append((dy - drift_y * dt) / root_dt)
        if len(innovations_x) < 3:
            continue
        regimes.append({
            "start_t_seconds": segment[0].t_seconds,
            "end_t_seconds": segment[-1].t_seconds,
            "sample_count": len(drift_samples),
            "drift": {"x": drift_x, "y": drift_y, "estimator": "theil_sen_median_pairwise_slope"},
            "diffusion": {"x": mad_scale(innovations_x), "y": mad_scale(innovations_y),
                          "estimator": "mad_of_detrended_brownian_innovations"},
            "drift_samples": drift_samples,
        })
    if not regimes:
        raise ValueError("no usable regimes; record longer or lower --min-dt")
    return regimes
