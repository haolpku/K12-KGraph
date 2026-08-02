#!/usr/bin/env python3
"""Healthcheck for the retrieval API container."""

from __future__ import annotations

import json
import sys
from urllib.error import URLError
from urllib.request import urlopen


def main() -> int:
    try:
        with urlopen("http://127.0.0.1:8000/health", timeout=3) as response:
            if response.status >= 400:
                return 1
            payload = response.read(4096)
    except URLError:
        return 1

    if not payload:
        return 0
    try:
        data = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError:
        return 0
    if "ok" in data:
        return 0 if data["ok"] is True else 1
    return 0 if data.get("status") in {None, "ok", "healthy"} else 1


if __name__ == "__main__":
    sys.exit(main())
