"""Validate buyer inputs and produce independently reproducible evidence packs.

This is a deterministic report executor, not a live trading system. Untrusted
strategy source, URLs and credentials are never executed or fetched.
"""

from __future__ import annotations

import csv
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import html
import io
import json
import math
from pathlib import Path
import re
import shutil
import tempfile
import time
import zipfile

from trading_lab.backtest import backtest
from trading_lab.models import BacktestSettings, Candle
from trading_lab.strategies import generate_signals

from . import __version__

MAX_CSV_BYTES = 8 * 1024 * 1024
MAX_ROWS = 25_000
MAX_SPEC_BYTES = 16_384
MAX_REQUEST_BYTES = 25 * 1024 * 1024
MAX_PACKAGE_BYTES = 12 * 1024 * 1024
COLUMNS = ("timestamp_ms", "open", "high", "low", "close", "volume")
DEFAULTS = {
    "flat": {},
    "buy_hold": {},
    "sma_cross": {"fast": 10, "slow": 30},
    "breakout": {"lookback": 20, "exit_lookback": 10},
    "rsi_reversion": {"period": 14, "entry": 30.0, "exit": 55.0},
    "supplied_signals": {},
}
SOURCE_FILES = (
    "evidence_service/__init__.py", "evidence_service/runner.py",
    "trading_lab/backtest.py", "trading_lab/models.py", "trading_lab/strategies.py",
)


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("utf-8")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_json(content: bytes, limit: int = MAX_SPEC_BYTES):
    if len(content) > limit:
        raise ValueError(f"JSON exceeds {limit} bytes")
    try:
        return json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(
                              ValueError("Non-finite JSON number")))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("Invalid UTF-8 JSON") from None


def _number(value, name, lower, upper):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        value = float(value)
    except OverflowError:
        raise ValueError(f"{name} must be a finite number") from None
    if not math.isfinite(value) or not lower <= value <= upper:
        raise ValueError(f"{name} must be between {lower} and {upper}")
    return value


def normalize_spec(spec: object) -> dict:
    allowed = {"label", "data_kind", "interval_ms", "strategy", "params",
               "initial_cash", "fee_rate", "slippage_bps"}
    if not isinstance(spec, dict) or spec.keys() - allowed:
        raise ValueError("Spec must be an object containing only documented fields")
    if not {"label", "data_kind", "interval_ms", "strategy"} <= spec.keys():
        raise ValueError("Spec requires label, data_kind, interval_ms and strategy")
    label = spec["label"]
    if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9 .,:()/_-]{1,80}", label):
        raise ValueError("label must be 1-80 plain ASCII letters, numbers or punctuation")
    if spec["data_kind"] not in ("synthetic", "buyer_supplied"):
        raise ValueError("data_kind must be synthetic or buyer_supplied")
    interval = spec["interval_ms"]
    if type(interval) is not int or not 1000 <= interval <= 604_800_000:
        raise ValueError("interval_ms must be an integer from 1000 to 604800000")
    strategy = spec["strategy"]
    if not isinstance(strategy, str) or strategy not in DEFAULTS:
        raise ValueError("Unsupported strategy; arbitrary strategy code is not accepted")
    params = spec.get("params", {})
    if not isinstance(params, dict) or params.keys() - DEFAULTS[strategy].keys():
        raise ValueError("Unknown strategy parameters")
    params = DEFAULTS[strategy] | params
    for key in ("fast", "slow", "lookback", "exit_lookback", "period"):
        if key in params and (type(params[key]) is not int or not 1 <= params[key] <= 1000):
            raise ValueError(f"{key} must be an integer from 1 to 1000")
    if strategy != "supplied_signals":
        generate_signals([], strategy, params)  # Full engine parameter contract.
    return {
        "label": label, "data_kind": spec["data_kind"], "interval_ms": interval,
        "strategy": strategy, "params": params,
        "initial_cash": _number(spec.get("initial_cash", 1000), "initial_cash", 1, 1_000_000),
        "fee_rate": _number(spec.get("fee_rate", 0.001), "fee_rate", 0, 0.05),
        "slippage_bps": _number(spec.get("slippage_bps", 5), "slippage_bps", 0, 500),
    }


def read_candles(content: bytes, spec: dict) -> tuple[list[Candle], list[float] | None]:
    if len(content) > MAX_CSV_BYTES:
        raise ValueError("Candle CSV exceeds 8 MiB")
    try:
        reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig"), newline=""))
        expected = list(COLUMNS) + (["signal"] if spec["strategy"] == "supplied_signals" else [])
        if reader.fieldnames != expected:
            raise ValueError("CSV header must be exactly " + ",".join(expected))
        candles, signals = [], []
        for row in reader:
            if len(candles) >= MAX_ROWS:
                raise ValueError("Candle CSV exceeds 25000 rows")
            if None in row or any(value is None for value in row.values()):
                raise ValueError("CSV row has extra or missing fields")
            ts = row["timestamp_ms"]
            if not re.fullmatch(r"[0-9]{1,13}", ts):
                raise ValueError("timestamp_ms must contain nonnegative integer milliseconds")
            candle = Candle(int(ts), *(float(row[field]) for field in COLUMNS[1:]))
            # datetime exports below are deliberately bounded to years 1970-2100.
            if candle.timestamp_ms > 4_102_444_800_000:
                raise ValueError("timestamp_ms must be no later than 2100-01-01 UTC")
            if candles and candle.timestamp_ms - candles[-1].timestamp_ms != spec["interval_ms"]:
                raise ValueError("Duplicate, out-of-order or missing candle interval")
            candles.append(candle)
            if "signal" in row:
                signals.append(float(row["signal"]))
        if len(candles) < 2:
            raise ValueError("At least two candles are required")
        # Validates prices, volume, OHLC relationships and finite values.
        generate_signals(candles, "flat")
        if signals and any(not math.isfinite(value) or not 0 <= value <= 1 for value in signals):
            raise ValueError("Signals must be finite exposures between 0 and 1")
        return candles, signals if signals else None
    except (UnicodeDecodeError, csv.Error, OverflowError):
        raise ValueError("Invalid UTF-8 candle CSV") from None


def evaluate(csv_content: bytes, job_spec: object) -> dict:
    spec = normalize_spec(job_spec)
    candles, supplied = read_candles(csv_content, spec)
    if supplied is not None:
        signals = supplied
        causality = {"status": "unverified", "prefix_checks": [],
                     "explanation": "Buyer-supplied signals may contain future information; their generation was not audited."}
    else:
        signals = generate_signals(candles, spec["strategy"], spec["params"])
        prefixes = sorted({max(1, len(candles) // 4), max(1, len(candles) // 2), len(candles) - 1})
        if any(generate_signals(candles[:end], spec["strategy"], spec["params"]) != signals[:end]
               for end in prefixes):
            raise ValueError("Supported rule failed prefix consistency check")
        causality = {"status": "supported_rule_prefix_checks_passed", "prefix_checks": prefixes,
                     "explanation": f"{len(prefixes)} sampled prefix checks plus the separately tested causal rule contract; not proof for arbitrary code."}
    assumptions = BacktestSettings(
        initial_cash=spec["initial_cash"], fee_rate=spec["fee_rate"],
        slippage_bps=spec["slippage_bps"], allocation=1, min_notional=0,
        step_size=0.00000001, max_drawdown_stop=1, annual_periods=1,
    )
    from dataclasses import replace
    scenarios = {
        "gross": replace(assumptions, fee_rate=0, slippage_bps=0),
        "base": assumptions,
        "stress": replace(assumptions, fee_rate=spec["fee_rate"] * 2,
                          slippage_bps=spec["slippage_bps"] * 3),
    }
    benchmark = generate_signals(candles, "buy_hold")
    results = {}
    for name, settings in scenarios.items():
        results[f"strategy_{name}"] = backtest(candles, signals, settings)
        results[f"buy_hold_{name}"] = backtest(candles, benchmark, settings)
    return {
        "spec": spec, "rows": len(candles), "start_ms": candles[0].timestamp_ms,
        "end_ms": candles[-1].timestamp_ms, "causality": causality,
        "assumptions": asdict(assumptions), "results": results,
        "input_sha256": digest(csv_content),
        "spec_sha256": digest(canonical(spec)),
    }


def _utc(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).isoformat()


def render_markdown(evidence: dict) -> str:
    spec = evidence["spec"]
    lines = ["# Backtest Evidence Pack", "", f"Dataset: **{spec['label']}**",
             f"Data type: **{spec['data_kind']}**. Simulation only.",
             f"UTC candle-open window: {_utc(evidence['start_ms'])} to {_utc(evidence['end_ms'])}.",
             f"Rows: {evidence['rows']:,}; interval: {spec['interval_ms']} ms; duplicates/gaps: none.",
             f"Rule: `{spec['strategy']}`; parameters: `{json.dumps(spec['params'], sort_keys=True)}`.",
             f"Hypothetical starting balance: {spec['initial_cash']:,.2f} quote-currency units.", "",
             "| Simulation | Return | Maximum drawdown | Fees paid | Fills | Closed round trips | Ending value |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for name, result in evidence["results"].items():
        m = result["metrics"]
        lines.append(f"| {name} | {m['total_return']:.2%} | {m['max_drawdown']:.2%} | "
                     f"{m['fees_paid']:.4f} | {m['trade_count']} | {m['closed_trades']} | {m['final_equity']:.4f} |")
    base = evidence["results"]["strategy_base"]
    relative = base["metrics"]["total_return"] - evidence["results"]["buy_hold_base"]["metrics"]["total_return"]
    stress_delta = evidence["results"]["strategy_stress"]["metrics"]["total_return"] - base["metrics"]["total_return"]
    lines += ["", f"Base return difference versus buy-and-hold: {relative * 100:+.2f} percentage points.",
              f"Stress return change versus base: {stress_delta * 100:+.2f} percentage points.", "",
              "## Execution contract", "",
              f"Base fee per fill: {spec['fee_rate']:.4%}; adverse slippage per fill: {spec['slippage_bps']:g} bps.",
              "Stress doubles the fee rate and triples slippage. Gross removes both.",
              "A close-time exposure request is filled at the NEXT candle open. The final signal has no later fill.",
              "Long-only, unlevered, one position. Entry spends exposure x available cash, including the buy fee.",
              "Positive requests do not resize an existing position; zero closes it on the next open.",
              "Buy-and-hold uses identical costs and first enters at the second candle open.",
              "Quantity step: 0.00000001; minimum notional: 0. These are simulation assumptions, not exchange rules.",
              "No practical drawdown stop (threshold 100%). Drawdowns are observed at candle closes; intrabar risk is not measured.",
              "A final open position is marked at the final close without a fabricated liquidation or exit fee.",
              f"Base ending open quantity: {base['position']['quantity']:.8f}; marked value: {base['position']['market_value']:.4f}.",
              "Funding, borrowing, spread beyond specified slippage, latency, market impact and tax are excluded.", "",
              "## Evidence and limits", "",
              f"Signal causality status: `{evidence['causality']['status']}`.",
              evidence["causality"]["explanation"],
              "Input cadence and OHLC consistency are checked. Data authenticity, completeness outside the supplied window and licensing are not verified.",
              "No parameter search, train/test selection or untouched holdout is performed. This checks the submitted setup on one supplied window.",
              "A short window or few closed trades cannot establish a repeatable trading edge. Losing results are retained.",
              "This is a technical simulation report, not a trading recommendation, profit guarantee or live-execution certification.",
              "Synthetic demo prices are invented and must not be presented as historical market performance.", "",
              "## Reproduction", "",
              "The ZIP includes the exact normalized specification, candle CSV, result JSON, fill/equity CSVs and executor source.",
              "With Python 3.10+ from the extracted directory:", "",
              "```text", "python -m evidence_service report --candles candles.csv --spec spec.json --out reproduced", "```", "",
              f"Input SHA-256: `{evidence['input_sha256']}`.",
              f"Normalized spec SHA-256: `{evidence['spec_sha256']}`.",
              "manifest.json hashes each delivery file except itself and the final ZIP. It records the exact executor source hashes.", ""]
    return "\n".join(lines)


def _csv_bytes(rows: list[dict], fields: list[str]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _html_report(markdown: str) -> bytes:
    body, in_table, in_code = [], False, False
    for line in markdown.splitlines():
        if line.startswith("```"):
            body.append("</pre>" if in_code else "<pre>")
            in_code = not in_code
            continue
        if in_code:
            body.append(html.escape(line) + "\n")
            continue
        if line.startswith("| "):
            cells = [part.strip() for part in line.strip("|").split("|")]
            if all(re.fullmatch(r"[-: ]+", cell) for cell in cells):
                continue
            if not in_table:
                body.append('<div class="table-scroll"><table>')
                in_table, cell_tag = True, "th"
            else:
                cell_tag = "td"
            body.append("<tr>" + "".join(f"<{cell_tag}>{html.escape(cell)}</{cell_tag}>" for cell in cells) + "</tr>")
            continue
        if in_table:
            body.append("</table></div>")
            in_table = False
        if line.startswith("# "):
            body.append("<h1>" + html.escape(line[2:]) + "</h1>")
        elif line.startswith("## "):
            body.append("<h2>" + html.escape(line[3:]) + "</h2>")
        elif line:
            # Formatting is deliberately small and applies after escaping.
            escaped = html.escape(line)
            escaped = re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", escaped)
            escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
            body.append("<p>" + escaped + "</p>")
    if in_table:
        body.append("</table></div>")
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Backtest Evidence Pack</title><style>body{max-width:1080px;margin:36px auto;padding:0 24px;'
        'font-family:system-ui,sans-serif;color:#162334;background:#f8fafc;line-height:1.55}'
        'h1{font-size:34px;letter-spacing:-1px}h2{margin-top:36px;font-size:22px}'
        'table{border-collapse:collapse;width:100%;font-size:14px;background:white}'
        'th,td{padding:12px;text-align:right;border-bottom:1px solid #dce4ec;white-space:nowrap}'
        'th:first-child,td:first-child{text-align:left}th{background:#162334;color:white}'
        '.table-scroll{overflow-x:auto;margin:24px 0}code,pre{font-family:ui-monospace,monospace;overflow-wrap:anywhere}'
        'pre{white-space:pre-wrap;background:#e8eef5;padding:18px}p{margin:10px 0}'
        '</style><main>' + "\n".join(body) + '</main></html>'
    ).encode("utf-8")


def _report_files(csv_content: bytes, spec: object) -> dict[str, bytes]:
    evidence = evaluate(csv_content, spec)
    markdown = render_markdown(evidence)
    files = {"report.md": markdown.encode("utf-8"), "candles.csv": csv_content,
             "spec.json": canonical(evidence["spec"]) + b"\n",
             "results.json": canonical(evidence) + b"\n"}
    files["report.html"] = _html_report(markdown)
    for name, result in evidence["results"].items():
        files[f"{name}-fills.csv"] = _csv_bytes(
            result["trades"], ["timestamp_ms", "side", "quantity", "price", "fee", "notional"])
        files[f"{name}-equity.csv"] = _csv_bytes(
            result["equity_curve"], ["timestamp_ms", "equity", "drawdown"])
    root = Path(__file__).resolve().parents[1]
    for relative in SOURCE_FILES:
        files[relative] = (root / relative).read_bytes()
    # Bundle neutral defaults. Every simulation explicitly supplies the
    # normalized job settings, so reproduction preserves all calculations.
    model_source = files["trading_lab/models.py"]
    files["trading_lab/models.py"] = re.sub(
        rb"initial_cash: float = [0-9.]+", b"initial_cash: float = 1000.0", model_source,
        count=1,
    )
    files["evidence_service/__main__.py"] = (root / "evidence_service/__main__.py").read_bytes()
    files["evidence_service/marketplace.py"] = (root / "evidence_service/marketplace.py").read_bytes()
    files["trading_lab/__init__.py"] = (root / "trading_lab/__init__.py").read_bytes()
    return files


def _finish(files: dict[str, bytes]) -> dict[str, bytes]:
    manifest = {"format": "backtest-evidence-1", "executor_version": __version__,
                "files_sha256": {name: digest(value) for name, value in sorted(files.items())},
                "external_api_calls": 0, "financial_transactions": 0}
    files["manifest.json"] = canonical(manifest) + b"\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, content in sorted(files.items()):
            # Fixed timestamp makes the same input/source produce the same package.
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    package = buffer.getvalue()
    if len(package) > MAX_PACKAGE_BYTES:
        raise ValueError("Delivery ZIP exceeds 12 MiB; use fewer candles")
    files["evidence-pack.zip"] = package
    return files


def _write_new_directory(files: dict[str, bytes], destination: Path) -> dict:
    destination = destination.resolve()
    if destination.exists():
        raise ValueError("Output directory already exists; choose a new directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".evidence-", dir=destination.parent))
    try:
        for name, content in files.items():
            target = temporary / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        # Recheck after preparation; do not replace an existing delivery.
        if destination.exists():
            raise ValueError("Output directory appeared during delivery")
        temporary.rename(destination)
    except BaseException:
        if temporary.parent.resolve() != destination.parent or not temporary.name.startswith(".evidence-"):
            raise RuntimeError("Refusing cleanup outside the new temporary delivery directory")
        shutil.rmtree(temporary)
        raise
    return {"status": "delivered_locally", "files": len(files),
            "package_bytes": len(files["evidence-pack.zip"]),
            "package_sha256": digest(files["evidence-pack.zip"]),
            "external_api_calls": 0, "financial_transactions": 0,
            "receipt_usd": 0, "output_directory": str(destination.resolve())}


def deliver(csv_content: bytes, spec: object, destination: Path) -> dict:
    start = time.perf_counter()
    result = _write_new_directory(_finish(_report_files(csv_content, spec)), destination)
    result["elapsed_seconds"] = round(time.perf_counter() - start, 6)
    return result


def deliver_batch(request: object, destination: Path) -> dict:
    """Execute one to three independent standardized reports as one repeat order.

    Buyer JSON contains data, not local paths, code, credentials or remote URLs.
    The caller controls only the output directory; no network worker is implied.
    """
    start = time.perf_counter()
    if not isinstance(request, dict) or set(request) != {"reports"}:
        raise ValueError("Batch request must contain only reports")
    reports = request["reports"]
    if not isinstance(reports, list) or not 1 <= len(reports) <= 3:
        raise ValueError("Batch requires one to three reports")
    files, summary = {}, []
    for index, item in enumerate(reports, 1):
        if not isinstance(item, dict) or set(item) != {"candles_csv", "spec"}:
            raise ValueError("Each report requires only candles_csv and spec")
        if not isinstance(item["candles_csv"], str):
            raise ValueError("candles_csv must be UTF-8 text")
        if len(canonical(item["spec"])) > MAX_SPEC_BYTES:
            raise ValueError("Spec exceeds 16 KiB")
        report_files = _report_files(item["candles_csv"].encode("utf-8"), item["spec"])
        files.update({f"report-{index}/{name}": value for name, value in report_files.items()})
        summary.append({"report": index, "spec_sha256": digest(report_files["spec.json"]),
                        "input_sha256": digest(report_files["candles.csv"])})
    files["batch.json"] = canonical({"reports": summary, "count": len(reports)}) + b"\n"
    result = _write_new_directory(_finish(files), destination)
    result["elapsed_seconds"] = round(time.perf_counter() - start, 6)
    result["report_count"] = len(reports)
    return result
