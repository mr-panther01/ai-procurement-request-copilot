from __future__ import annotations

import argparse
import csv
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=False)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.run_public_evals import evaluate
from src.contracts import ProcurementDecision
from src.solution import handle_request


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _start_mock_api() -> tuple[subprocess.Popen, str]:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "mock_api.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "error",
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Mock vendor-risk API exited with code {process.returncode}")
        try:
            if requests.get(f"{base_url}/health", timeout=0.3).ok:
                return process, base_url
        except requests.RequestException:
            time.sleep(0.1)
    process.terminate()
    process.wait(timeout=5)
    raise RuntimeError("Mock vendor-risk API did not become ready within 10 seconds")


def _evaluate_case(case: dict, architecture: str) -> dict:
    started = time.perf_counter()
    try:
        raw = handle_request(case["request_id"], architecture=architecture)
        decision = raw if isinstance(raw, ProcurementDecision) else ProcurementDecision.model_validate(raw)
        latency = (time.perf_counter() - started) * 1000
        failures = evaluate(decision, case["expectations"])
        telemetry = decision.telemetry
        row = {
            "case_id": case["case_id"],
            "request_id": case["request_id"],
            "architecture": architecture,
            "passed_minimum_checks": not failures,
            "latency_ms": round(latency, 1),
            "llm_calls": telemetry.llm_calls if telemetry else "",
            "tool_calls": telemetry.tool_calls if telemetry else "",
            "failures": " | ".join(failures),
        }
        status = "PASS" if not failures else "FAIL"
        print(f"{status} {architecture:6} {case['case_id']} {latency:.0f}ms")
        for failure in failures:
            print(f"  - {failure}")
        return row
    except Exception as exc:
        latency = (time.perf_counter() - started) * 1000
        print(f"ERROR {architecture:6} {case['case_id']} {type(exc).__name__}: {exc}")
        return {
            "case_id": case["case_id"],
            "request_id": case["request_id"],
            "architecture": architecture,
            "passed_minimum_checks": False,
            "latency_ms": round(latency, 1),
            "llm_calls": "",
            "tool_calls": "",
            "failures": f"ERROR: {type(exc).__name__}: {exc}",
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the same public cases against both architectures.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "evals",
        help="Directory for per-case CSVs and comparison summary.",
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    cases = json.loads((ROOT / "evals" / "public_cases.json").read_text(encoding="utf-8"))
    process, base_url = _start_mock_api()
    previous_base_url = os.environ.get("VENDOR_RISK_BASE_URL")
    os.environ["VENDOR_RISK_BASE_URL"] = base_url

    try:
        results = {"single": [], "staged": []}
        for index, case in enumerate(cases):
            order = ("single", "staged") if index % 2 == 0 else ("staged", "single")
            for architecture in order:
                results[architecture].append(_evaluate_case(case, architecture))
    finally:
        if previous_base_url is None:
            os.environ.pop("VENDOR_RISK_BASE_URL", None)
        else:
            os.environ["VENDOR_RISK_BASE_URL"] = previous_base_url
        process.terminate()
        process.wait(timeout=5)

    for architecture, rows in results.items():
        target = args.output_dir / f"results_{architecture}.csv"
        with target.open("w", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    summary = []
    for architecture, rows in results.items():
        summary.append(
            {
                "architecture": architecture,
                "passed_cases": sum(bool(row["passed_minimum_checks"]) for row in rows),
                "total_cases": len(rows),
                "avg_latency_ms": round(sum(row["latency_ms"] for row in rows) / len(rows), 1),
                "avg_llm_calls": round(
                    sum(float(row["llm_calls"] or 0) for row in rows) / len(rows), 2
                ),
                "avg_tool_calls": round(
                    sum(float(row["tool_calls"] or 0) for row in rows) / len(rows), 2
                ),
            }
        )
    summary_path = args.output_dir / "architecture_comparison.csv"
    with summary_path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=list(summary[0]))
        writer.writeheader()
        writer.writerows(summary)

    lines = [
        "# Architecture comparison",
        "",
        "Generated by `python evals/run_comparison.py` using the same public cases and a local mock vendor-risk API. Architecture order alternates per case to reduce fixed-order startup bias.",
        "",
        "| Architecture | Minimum checks | Avg latency (ms) | Avg LLM calls | Avg tool calls |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in summary:
        lines.append(
            f"| {row['architecture']} | {row['passed_cases']}/{row['total_cases']} | "
            f"{row['avg_latency_ms']} | {row['avg_llm_calls']} | {row['avg_tool_calls']} |"
        )
    lines.extend(
        [
            "",
            "Latency is a local single-run measurement and varies by machine/load. "
            "The implementation uses zero remote LLM calls; policy decisions are deterministic.",
            "",
        ]
    )
    (args.output_dir / "architecture_comparison.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWrote comparison results under {args.output_dir}")


if __name__ == "__main__":
    main()
