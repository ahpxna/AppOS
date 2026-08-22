#!/usr/bin/env python3
"""Fit time-varying pointer drift/diffusion from a consented CSV trace.

No global average and no cursor playback are present.  The JSON output keeps a
time-ordered collection of local regimes, including each window's raw drift
samples, so any research analysis can preserve diverging behaviour.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.common.pointer_dynamics import Point, fit_regimes


def main() -> int:
    parser = argparse.ArgumentParser(description="Robust local pointer regime estimator")
    parser.add_argument("trace_csv")
    parser.add_argument("--output", required=True)
    parser.add_argument("--window", type=int, default=80)
    parser.add_argument("--stride", type=int, default=40)
    parser.add_argument("--min-dt", type=float, default=0.001)
    args = parser.parse_args()
    with Path(args.trace_csv).open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        points = [Point(float(row["t_seconds"]), float(row["x"]), float(row["y"])) for row in reader]
    regimes = fit_regimes(points, window=args.window, stride=args.stride, min_dt=args.min_dt)
    payload = {
        "method": "rolling_theil_sen_drift_and_mad_diffusion",
        "global_average_used": False,
        "playback_supported": False,
        "regimes": regimes,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {len(regimes)} local regimes to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
