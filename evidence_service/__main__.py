"""Local fulfillment CLI. Does not connect to a marketplace or accept money."""

import argparse
import json
import math
from pathlib import Path
import random
import sys

from .runner import (COLUMNS, MAX_CSV_BYTES, MAX_REQUEST_BYTES, MAX_SPEC_BYTES,
                     _csv_bytes, deliver, deliver_batch, parse_json)


def bounded_read(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError(f"Input exceeds {limit} bytes")
    return content


def synthetic_csv(rows: int = 720) -> bytes:
    if type(rows) is not int or not 2 <= rows <= 25_000:
        raise ValueError("Demo rows must be an integer from 2 to 25000")
    generator = random.Random(20261007)
    bars, prior = [], 100.0
    for index in range(rows):
        opening = prior * math.exp(generator.gauss(0, 0.0015))
        closing = opening * math.exp(generator.gauss(0, 0.004))
        bars.append({"timestamp_ms": 1_767_225_600_000 + index * 3_600_000,
                     "open": round(opening, 8),
                     "high": round(max(opening, closing) * 1.003, 8),
                     "low": round(min(opening, closing) * 0.997, 8),
                     "close": round(closing, 8), "volume": round(generator.uniform(10, 100), 5)})
        prior = closing
    return _csv_bytes(bars, list(COLUMNS))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report", help="One supplied candle CSV and strict JSON specification")
    report.add_argument("--candles", type=Path, required=True)
    report.add_argument("--spec", type=Path, required=True)
    report.add_argument("--out", type=Path, required=True)
    batch = sub.add_parser("batch", help="One to three reports in a bounded inline-data JSON request")
    batch.add_argument("--request", type=Path, required=True)
    batch.add_argument("--out", type=Path, required=True)
    fulfillment = sub.add_parser("fulfill", help="Prepare inline sealed-delivery JSON locally; no network call")
    fulfillment.add_argument("--request", type=Path, required=True)
    fulfillment.add_argument("--tier", choices=("single", "comparison", "quick_check"), default="single")
    fulfillment.add_argument("--out", type=Path, required=True)
    demo = sub.add_parser("demo", help="Clearly labelled synthetic sample; no historical profit claims")
    demo.add_argument("--out", type=Path, required=True)
    demo.add_argument("--rows", type=int, default=720)
    args = parser.parse_args(argv)
    try:
        if args.command == "report":
            result = deliver(bounded_read(args.candles, MAX_CSV_BYTES),
                             parse_json(bounded_read(args.spec, MAX_SPEC_BYTES)), args.out)
        elif args.command == "batch":
            result = deliver_batch(parse_json(bounded_read(args.request, MAX_REQUEST_BYTES),
                                              MAX_REQUEST_BYTES), args.out)
        elif args.command == "fulfill":
            from .marketplace import TRANSPORT_LIMIT, fulfill
            result = fulfill(parse_json(bounded_read(args.request, TRANSPORT_LIMIT), TRANSPORT_LIMIT), args.tier)
            args.out.parent.mkdir(parents=True, exist_ok=True)
            with args.out.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps({"output": result.pop("output"), "preview": result["preview"]},
                                        sort_keys=True, allow_nan=False))
            result["status"] = "prepared_locally_not_submitted"
        else:
            result = deliver(synthetic_csv(args.rows), {
                "label": "SYNTHETIC DEMO - invented prices", "data_kind": "synthetic",
                "interval_ms": 3_600_000, "strategy": "sma_cross",
                "params": {"fast": 10, "slow": 30}, "initial_cash": 1000,
                "fee_rate": 0.001, "slippage_bps": 5,
            }, args.out)
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0
    except (ValueError, OSError, RecursionError) as error:
        # Invalid requests produce no success receipt and no half-written delivery.
        print(json.dumps({"status": "rejected", "error": str(error)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
