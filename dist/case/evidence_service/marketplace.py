"""Agent Souk-sized offline delivery adapter. No signup, wallet or network access.

The published job input/output limit is 512 KB each. This adapter uses 500,000
bytes to leave room for platform envelopes. Never accept an order that cannot
be delivered inline. Larger offline reports need another delivery channel.
"""

import base64
import json
import time

from .runner import _finish, _report_files, canonical, digest

TRANSPORT_LIMIT = 500_000
TIERS = {
    "single": {"reports": 1, "rows": 1000, "price_usdc": 49},
    "comparison": {"reports": 3, "rows": 300, "price_usdc": 129},
    "quick_check": {"reports": 1, "rows": 300, "price_usdc": 29},
}


def fulfill(request: object, tier: str = "single") -> dict:
    start = time.perf_counter()
    if tier not in TIERS:
        raise ValueError("Unknown service tier")
    if len(canonical(request)) > TRANSPORT_LIMIT:
        raise ValueError("Marketplace input exceeds safe inline size")
    if not isinstance(request, dict) or set(request) != {"reports"}:
        raise ValueError("Marketplace request requires only reports")
    reports = request["reports"]
    if not isinstance(reports, list) or len(reports) != TIERS[tier]["reports"]:
        raise ValueError(f"Tier requires exactly {TIERS[tier]['reports']} reports")
    files = {}
    for index, item in enumerate(reports, 1):
        if not isinstance(item, dict) or set(item) != {"candles_csv", "spec"}:
            raise ValueError("Each report requires only candles_csv and spec")
        if not isinstance(item["candles_csv"], str):
            raise ValueError("candles_csv must be UTF-8 text")
        report_files = _report_files(item["candles_csv"].encode("utf-8"), item["spec"])
        evidence = json.loads(report_files["results.json"])
        if evidence["rows"] > TIERS[tier]["rows"]:
            raise ValueError(f"Tier limit is {TIERS[tier]['rows']} candles per report")
        files.update({f"report-{index}/{name}": content for name, content in report_files.items()})
    package = _finish(files)["evidence-pack.zip"]
    output = {"format": "backtest-evidence-1", "report_count": len(reports),
              "filename": "evidence-pack.zip", "zip_sha256": digest(package),
              "zip_base64": base64.b64encode(package).decode("ascii"),
              "data_notice": "Simulation report. Synthetic data is invented; supplied signals are not a causality audit."}
    if len(canonical(output)) > TRANSPORT_LIMIT:
        raise ValueError("Delivery exceeds safe inline size; decline before order acceptance")
    return {"output": output, "preview": f"{len(reports)} report(s), fill ledgers, cost scenarios and reproducible source",
            "output_json_bytes": len(canonical(output)),
            "elapsed_seconds": round(time.perf_counter() - start, 6),
            "external_api_calls": 0, "financial_transactions": 0, "receipt_usd": 0}
