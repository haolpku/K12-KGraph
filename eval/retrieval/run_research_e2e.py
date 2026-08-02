#!/usr/bin/env python3
"""Run the synthetic research E2E contract suite against a live HTTP API."""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def load_cases(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def request_json(
    url: str, payload: dict[str, Any], *, timeout: float
) -> tuple[int, dict[str, Any], float]:
    if urlparse(url).scheme not in {"http", "https"}:
        raise ValueError("E2E URL must use http or https")
    started = time.perf_counter()
    request = urllib.request.Request(  # noqa: S310 - scheme is allowlisted above
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 - scheme is allowlisted above
            request, timeout=timeout
        ) as response:
            status = response.status
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = json.loads(exc.read().decode("utf-8"))
    return status, body, (time.perf_counter() - started) * 1000


def healthcheck(url: str, *, timeout: float) -> dict[str, Any]:
    parsed = urlparse(url)
    health_url = f"{parsed.scheme}://{parsed.netloc}/health"
    request = urllib.request.Request(  # noqa: S310 - scheme is inherited from an allowlisted URL
        health_url,
        method="GET",
    )
    with urllib.request.urlopen(  # noqa: S310 - scheme is inherited from an allowlisted URL
        request, timeout=timeout
    ) as response:
        body = json.loads(response.read().decode("utf-8"))
        if response.status != 200 or body.get("ok") is not True:
            raise RuntimeError(f"health preflight failed with HTTP {response.status}")
        return body


def forbidden_fields(value: Any, forbidden: set[str]) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in forbidden or key.endswith("_search_text"):
                found.add(key)
            found.update(forbidden_fields(item, forbidden))
    elif isinstance(value, list):
        for item in value:
            found.update(forbidden_fields(item, forbidden))
    return found


def evaluate_case(
    case: dict[str, Any], status: int, body: dict[str, Any]
) -> list[str]:
    failures: list[str] = []
    if status != case["expected_status"]:
        failures.append(f"status={status}, expected={case['expected_status']}")
    expected_intents = case.get("expected_intents")
    if status == 200 and expected_intents:
        if body.get("intents") != expected_intents:
            failures.append(
                f"intents={body.get('intents')!r}, expected={expected_intents!r}"
            )
    expected_reason = case.get("expected_reason_code")
    if status == 200 and expected_reason and body.get("reason_code") != expected_reason:
        failures.append(
            f"reason_code={body.get('reason_code')!r}, expected={expected_reason!r}"
        )
    expected_error_reason = case.get("expected_error_reason_code")
    if expected_error_reason:
        detail = body.get("detail") if isinstance(body.get("detail"), dict) else {}
        actual_error_reason = detail.get("reason_code")
        if actual_error_reason != expected_error_reason:
            failures.append(
                f"error_reason_code={actual_error_reason!r}, "
                f"expected={expected_error_reason!r}"
            )
    minimum_evidence = int(case.get("expected_min_evidence", 0))
    evidence_count = len(body.get("evidence_nodes") or [])
    evidence_count += len(body.get("relation_paths") or [])
    evidence_count += len(body.get("textbook_locations") or [])
    if status == 200 and evidence_count < minimum_evidence:
        failures.append(
            f"evidence_count={evidence_count}, expected>={minimum_evidence}"
        )
    warnings = body.get("warnings") or []
    if any("backend unavailable" in str(item).lower() for item in warnings):
        failures.append("backend_unavailable_warning")
    forbidden = set(case.get("forbidden_response_fields", []))
    leaked = forbidden_fields(body, forbidden)
    if leaked:
        failures.append(f"forbidden_fields={sorted(leaked)!r}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cases",
        type=Path,
        default=Path("eval/retrieval/research_e2e_cases.jsonl"),
    )
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:18000/v1/retrieval/search",
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("eval/retrieval/research_e2e_results.json"),
    )
    args = parser.parse_args()
    healthcheck(args.url, timeout=args.timeout)

    results = []
    for case in load_cases(args.cases):
        payload = dict(case.get("request") or {"question": case["question"]})
        status, body, latency_ms = request_json(
            args.url, payload, timeout=args.timeout
        )
        failures = evaluate_case(case, status, body)
        results.append(
            {
                "case_id": case["case_id"],
                "category": case["category"],
                "passed": not failures,
                "failures": failures,
                "status": status,
                "latency_ms": round(latency_ms, 3),
            }
        )

    latencies = sorted(item["latency_ms"] for item in results)
    passed = sum(item["passed"] for item in results)
    summary = {
        "source": "synthetic_not_sme_reviewed",
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "pass_rate": passed / len(results) if results else 0.0,
        "average_latency_ms": (
            sum(latencies) / len(latencies) if latencies else 0.0
        ),
        "p95_latency_ms": (
            latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
            if latencies
            else 0.0
        ),
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"E2E: {passed}/{len(results)} passed; "
        f"avg={summary['average_latency_ms']:.1f}ms; "
        f"p95={summary['p95_latency_ms']:.1f}ms"
    )
    for item in results:
        if not item["passed"]:
            print(f"{item['case_id']}: {'; '.join(item['failures'])}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
