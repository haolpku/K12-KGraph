#!/usr/bin/env python3
"""Verify unavailable-backend HTTP, audit-log, and metrics semantics in isolation."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _request(
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 5.0,
) -> tuple[int, str]:
    if urlparse(url).scheme not in {"http", "https"}:
        raise ValueError("failure E2E URL must use http or https")
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - scheme is allowlisted above
        url,
        data=data,
        headers=headers or {},
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _wait_for_server(base_url: str, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("isolated API process exited before readiness")
        try:
            status, _body = _request(f"{base_url}/openapi.json", timeout=0.5)
            if status == 200:
                return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.1)
    raise TimeoutError("isolated API process did not become ready")


def _audit_records(log_text: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in log_text.splitlines():
        start = line.find("{")
        if start < 0:
            continue
        try:
            value = json.loads(line[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "request_id" in value:
            records.append(value)
    return records


def run_check() -> dict[str, Any]:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    password_marker = f"db-password-{uuid.uuid4()}"
    bearer_marker = f"bearer-{uuid.uuid4()}"
    question_marker = f"解释不存在知识点-{uuid.uuid4()}"
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "NEO4J_URI": "bolt://127.0.0.1:1",
            "NEO4J_USER": "neo4j",
            "NEO4J_PASSWORD": password_marker,
            "NEO4J_READONLY_USER": "readonly-test",
            "NEO4J_READONLY_PASSWORD": password_marker,
            "K12_ENV": "development",
            "K12_AUTH_MODE": "development",
            "K12_ROUTER_MODE": "cascade",
            "K12_ROUTER_API_KEY": "",
            "DEEPSEEK_API_KEY": "",
            "K12_RETRIEVAL_EMBEDDING_PROVIDER": "hash",
            "K12_RETRIEVAL_HYBRID_BACKEND": "local",
            "K12_RETRIEVAL_TIMEOUT_SECONDS": "0.5",
            "K12_AUDIT_HMAC_KEY": "failure-semantics-test",
        }
    )
    process = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-m",
            "uvicorn",
            "retrieval.api:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "info",
        ],
        cwd=REPO_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    failures: list[str] = []
    status = 0
    reason_code: str | None = None
    metrics_status = 0
    metrics_text = ""
    try:
        _wait_for_server(base_url, process)
        status, response_text = _request(
            f"{base_url}/v1/retrieval/search",
            payload={"question": question_marker, "top_k": 5},
            headers={
                "Authorization": f"Bearer {bearer_marker}",
                "Content-Type": "application/json",
            },
        )
        response_body = json.loads(response_text)
        detail = response_body.get("detail", {})
        reason_code = detail.get("reason_code") if isinstance(detail, dict) else None
        metrics_status, metrics_text = _request(f"{base_url}/metrics")
    finally:
        process.terminate()
        try:
            log_text, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            log_text, _ = process.communicate(timeout=5)

    if status != 503:
        failures.append(f"status={status}, expected=503")
    if reason_code != "GRAPH_BACKEND_UNAVAILABLE":
        failures.append(
            f"reason_code={reason_code!r}, expected='GRAPH_BACKEND_UNAVAILABLE'"
        )
    for marker_name, marker in (
        ("raw_question", question_marker),
        ("database_password", password_marker),
        ("authorization", bearer_marker),
    ):
        if marker in log_text:
            failures.append(f"log_leak={marker_name}")
    audit_records = _audit_records(log_text)
    backend_records = [
        record
        for record in audit_records
        if record.get("reason_code") == "GRAPH_BACKEND_UNAVAILABLE"
    ]
    if not backend_records:
        failures.append("missing_backend_unavailable_audit_record")
    else:
        required = {"request_id", "route", "reason_code", "status", "latency_ms"}
        missing = required - set(backend_records[-1])
        if missing:
            failures.append(f"audit_fields_missing={sorted(missing)!r}")
    if metrics_status != 200:
        failures.append(f"metrics_status={metrics_status}, expected=200")
    if "k12_retrieval_requests_total" not in metrics_text:
        failures.append("request_metric_missing")
    return {
        "isolated_api": True,
        "neo4j_target": "invalid_loopback_port",
        "status": status,
        "reason_code": reason_code,
        "audit_record_count": len(backend_records),
        "metrics_status": metrics_status,
        "credential_or_query_log_leaks": 0
        if not any(item.startswith("log_leak=") for item in failures)
        else 1,
        "failures": failures,
        "passed": not failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    summary = run_check()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
