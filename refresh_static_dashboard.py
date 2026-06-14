#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent
DEFAULT_POOL_TARGET = 50


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    return Path(raw) if raw else None


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def load_builders(tau_src: Path):
    sys.path.insert(0, str(tau_src))
    from r2 import build_dashboard_home_payload, build_dashboard_summary_payload

    return build_dashboard_home_payload, build_dashboard_summary_payload


def read_payload(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"dashboard payload must be an object: {path}")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def task_file_count(pool_dir: Path) -> int:
    if not pool_dir.is_dir():
        return 0
    return sum(1 for path in pool_dir.iterdir() if path.is_file() and path.suffix == ".json")


def pool_rebuild_entry(label: str, pool_dir: Path, target: int) -> dict[str, Any]:
    count = task_file_count(pool_dir)
    needed = max(target - count, 0)
    progress = min(count / target, 1.0) if target > 0 else 1.0
    return {
        "label": label,
        "count": count,
        "target": target,
        "needed": needed,
        "ready": needed == 0,
        "progress": progress,
        "path": str(pool_dir),
    }


def pool_rebuild_status(validate_root: Path, target: int = DEFAULT_POOL_TARGET) -> dict[str, Any]:
    pools = [
        pool_rebuild_entry("primary", validate_root / "task-pool", target),
        pool_rebuild_entry("retest", validate_root / "task-pool-retest", target),
    ]
    total_count = sum(int(pool["count"]) for pool in pools)
    total_target = sum(int(pool["target"]) for pool in pools)
    total_needed = sum(int(pool["needed"]) for pool in pools)
    return {
        "updated_at": utc_now_iso(),
        "mode": "static",
        "target_per_pool": target,
        "count": total_count,
        "target": total_target,
        "needed": total_needed,
        "ready": total_needed == 0,
        "progress": min(total_count / total_target, 1.0) if total_target > 0 else 1.0,
        "pools": pools,
    }


def with_pool_rebuild_status(payload: dict[str, Any], source: Path) -> dict[str, Any]:
    return {
        **payload,
        "pool_rebuild": pool_rebuild_status(source.parent),
    }


def walk_json_objects(value: Any) -> Any:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json_objects(child)


def mini_swe_usage_from_outputs(outputs_dir: Path) -> dict[str, Any] | None:
    if not outputs_dir.is_dir():
        return None
    totals: dict[str, Any] = {
        "cost": 0.0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "trajectory_count": 0,
    }
    for trajectory_path in sorted(outputs_dir.glob("*/trajectory.json")):
        try:
            payload = read_payload(trajectory_path)
        except Exception:
            continue
        totals["trajectory_count"] += 1
        for item in walk_json_objects(payload):
            usage = item.get("usage") if isinstance(item, dict) else None
            if not isinstance(usage, dict):
                continue
            cost = usage.get("cost")
            if isinstance(cost, (int, float)):
                totals["cost"] += float(cost)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                value = usage.get(key)
                if isinstance(value, (int, float)):
                    totals[key] += int(value)
    if not totals["trajectory_count"]:
        return None
    return {**totals, "cost_available": totals["cost"] > 0}


def mini_swe_usage_from_comparison(benchmark_root: Path, comparison: dict[str, Any]) -> dict[str, Any] | None:
    king_sha = str(comparison.get("king_commit_sha") or "")
    if king_sha:
        local_usage = mini_swe_usage_from_outputs(benchmark_root / king_sha / "mini-swe-agent" / "mini_outputs")
        if local_usage is not None:
            return local_usage
    scores = comparison.get("scores") if isinstance(comparison.get("scores"), dict) else {}
    baseline_score = scores.get("baseline") if isinstance(scores.get("baseline"), dict) else scores.get("pi")
    report_path = Path(str((baseline_score or {}).get("report_path") or ""))
    if report_path.parent.name != "official_scoring":
        return None
    return mini_swe_usage_from_outputs(report_path.parent.parent / "mini_outputs")


def compact_swebench_comparison(benchmark_root: Path, comparison: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(comparison, dict):
        return None
    usage = comparison.get("usage") if isinstance(comparison.get("usage"), dict) else {}
    baseline = comparison.get("baseline") if isinstance(comparison.get("baseline"), dict) else comparison.get("pi")
    baseline_usage = usage.get("baseline") if isinstance(usage.get("baseline"), dict) else usage.get("pi")
    mini_usage = mini_swe_usage_from_comparison(benchmark_root, comparison)
    return {
        "status": comparison.get("status"),
        "benchmark": comparison.get("benchmark"),
        "started_at": comparison.get("started_at"),
        "finished_at": comparison.get("finished_at"),
        "king_commit_sha": comparison.get("king_commit_sha"),
        "king": comparison.get("king"),
        "baseline": baseline,
        "model": comparison.get("model"),
        "provider_only": comparison.get("provider_only"),
        "manifest": comparison.get("manifest"),
        "elapsed": comparison.get("elapsed"),
        "scores": comparison.get("scores") if isinstance(comparison.get("scores"), dict) else {},
        "usage": {
            "king": usage.get("king"),
            "baseline": mini_usage or baseline_usage,
            "cost_available": bool(
                usage.get("king", {}).get("cost_available")
                and (mini_usage or baseline_usage or {}).get("cost_available")
            ),
        },
    }


def dashboard_swebench_latest(payload: dict[str, Any]) -> dict[str, Any] | None:
    benchmarks = payload.get("benchmarks") if isinstance(payload.get("benchmarks"), dict) else {}
    swebench = benchmarks.get("swebench_verified") if isinstance(benchmarks.get("swebench_verified"), dict) else {}
    latest = swebench.get("latest") if isinstance(swebench.get("latest"), dict) else None
    if not latest or isinstance(latest.get("scores"), dict):
        return latest
    return {
        **latest,
        "scores": {
            "king": latest.get("king"),
            "baseline": latest.get("baseline") or latest.get("pi"),
            "pi": latest.get("pi"),
            "delta_pass_rate": latest.get("delta_pass_rate"),
        },
    }


def artifact_swebench_latest(benchmark_root: Path | None) -> dict[str, Any] | None:
    if benchmark_root is None or not benchmark_root.is_dir():
        return None
    latest_path = benchmark_root / "latest.json"
    if not latest_path.exists():
        return None
    return compact_swebench_comparison(benchmark_root, read_payload(latest_path))


def swebench_payload(payload: dict[str, Any], benchmark_root: Path | None) -> dict[str, Any]:
    latest = dashboard_swebench_latest(payload)
    if latest is None and benchmark_root is not None:
        latest = artifact_swebench_latest(benchmark_root)
    return {"latest": latest, "active": latest}


def refresh_static_dashboard(
    source: Path,
    output_dir: Path,
    tau_src: Path,
    benchmark_root: Path | None,
) -> None:
    build_home, build_summary = load_builders(tau_src)
    payload = read_payload(source)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "dashboard-home.json", with_pool_rebuild_status(build_home(payload), source))
    write_json(output_dir / "dashboard-summary.json", with_pool_rebuild_status(build_summary(payload), source))
    write_json(output_dir / "swebench-local.json", swebench_payload(payload, benchmark_root))


def main() -> int:
    default_source = _env_path("DASHBOARD_DATA_PATH") or (DEFAULT_OUTPUT_DIR / "dashboard_data.json")
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else default_source
    output_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUTPUT_DIR
    benchmark_root = Path(sys.argv[3]) if len(sys.argv) > 3 else _env_path("SWEBENCH_BENCHMARK_ROOT")
    tau_src = Path(sys.argv[4]) if len(sys.argv) > 4 else _env_path("TAU_SRC")
    if tau_src is None or not tau_src.is_dir():
        raise SystemExit(
            "TAU_SRC must point at the tau/src checkout (set env TAU_SRC or pass argv[4])"
        )
    refresh_static_dashboard(
        source=source,
        output_dir=output_dir,
        tau_src=tau_src,
        benchmark_root=benchmark_root,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
