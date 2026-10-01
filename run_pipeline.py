#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Single entry point for the whole K12-KGraph pipeline.

Stages
------
``kg``   PDF -> markdown -> sections -> text graph   (``src/kg/run_pipeline.py``)
``mm``   figures -> figure nodes and relations       (``src/mm/run_pipeline.py``)
``qa``   per-book graphs -> VQA training data        (``src/qa/run_pipeline.py``)

Examples
--------
::

    python run_pipeline.py kg --filter-prefix math_7a_rjb
    python run_pipeline.py mm --filter-prefix math_7a_rjb
    python run_pipeline.py qa --filter-prefix math_7a_rjb
    python run_pipeline.py all --filter-prefix math_7a_rjb
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import List


REPO_ROOT = Path(__file__).resolve().parent

STAGE_SCRIPTS = {
    "kg": REPO_ROOT / "src" / "kg" / "run_pipeline.py",
    "mm": REPO_ROOT / "src" / "mm" / "run_pipeline.py",
    "qa": REPO_ROOT / "src" / "qa" / "run_pipeline.py",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="K12-KGraph 统一入口：kg / mm / qa")
    parser.add_argument("stage", choices=["kg", "mm", "qa", "all"], help="Which stage to run")
    parser.add_argument("--config", default=None, help="Pipeline config path; defaults to config/default.yaml")
    parser.add_argument("--filter-prefix", action="append", default=None, help="Only process these book prefixes")
    parser.add_argument("--limit", type=int, default=None, help="Process at most N books")
    parser.add_argument("--model", default=None, help="Override the model name (mm / qa)")
    parser.add_argument("--dry-run", action="store_true", help="Skip model calls (mm)")
    parser.add_argument("--no-overwrite", action="store_true", help="Reuse existing outputs where possible")
    parser.add_argument("--python", default=None, help="Override the Python interpreter")
    return parser.parse_args()


def stage_cmd(stage: str, args: argparse.Namespace, py: str) -> List[str]:
    cmd = [py, str(STAGE_SCRIPTS[stage])]
    if args.config:
        cmd += ["--config", str(args.config)]
    for prefix in args.filter_prefix or []:
        cmd += ["--filter-prefix", prefix]
    if args.limit is not None:
        cmd += ["--limit", str(int(args.limit))]
    if stage == "mm":
        if args.model:
            cmd += ["--model", str(args.model)]
        if args.dry_run:
            cmd += ["--dry-run"]
        if args.no_overwrite:
            cmd += ["--no-overwrite"]
    return cmd


def main() -> None:
    args = parse_args()
    py = args.python or sys.executable

    stages = list(STAGE_SCRIPTS) if args.stage == "all" else [args.stage]
    for stage in stages:
        script = STAGE_SCRIPTS[stage]
        if not script.exists():
            raise SystemExit(f"Stage script not found: {script}")
        cmd = stage_cmd(stage, args, py)
        print(f"\n########## stage: {stage} ##########", flush=True)
        print("[cmd]", " ".join(cmd), flush=True)
        subprocess.run(cmd, check=True)


if __name__ == "__main__":
    main()
