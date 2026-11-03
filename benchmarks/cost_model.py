#!/usr/bin/env python3
"""Cost model for the courtroom transliteration runtime (FINAL_DESIGN §5.3).

Central cost is storage + egress + one small VM and is independent of keystroke
volume. Egress dominates; the court mirror is the lever.

All prices are *assumptions to verify*, not quotes. Edit the CLI flags.

Usage::

    python -m benchmarks.cost_model
    python -m benchmarks.cost_model --machines 5000 --courts 200 --json results/cost_model.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Assumptions:
    machines: int = 5000
    courts: int = 200
    base_mb: float = 138.3
    rescore_mb: float = 358.1
    releases_per_month: float = 1.0
    cdn_usd_per_gb: float = 0.085
    central_vm_usd_per_month: float = 30.0
    object_storage_usd_per_month: float = 1.0
    gpu_instance_usd_per_month: float = 380.0  # g4dn.xlarge-class, always-on
    tier_b_box_usd_capex: float = 400.0
    tier_b_fraction: float = 0.15  # share of machines that fail the hardware gate
    amortize_months: int = 36


def gb(mb: float) -> float:
    return mb / 1024.0


def scenario_egress_gb(a: Assumptions, kind: str) -> float:
    full = gb(a.base_mb + a.rescore_mb)
    base_only = gb(a.base_mb)
    if kind == "direct_full":
        return a.machines * full * a.releases_per_month
    if kind == "direct_weights_only":
        return a.machines * base_only * a.releases_per_month
    if kind == "court_mirrors_full":
        return a.courts * full * a.releases_per_month
    if kind == "court_mirrors_weights_only":
        return a.courts * base_only * a.releases_per_month
    raise ValueError(kind)


def usd(egress_gb: float, a: Assumptions) -> dict:
    egress_cost = egress_gb * a.cdn_usd_per_gb
    total = egress_cost + a.central_vm_usd_per_month + a.object_storage_usd_per_month
    return {
        "egress_gb": round(egress_gb, 2),
        "egress_usd": round(egress_cost, 2),
        "vm_usd": a.central_vm_usd_per_month,
        "storage_usd": a.object_storage_usd_per_month,
        "total_usd_per_month": round(total, 2),
        "usd_per_1000_users": round(total / (a.machines / 1000.0), 2),
    }


def build_report(a: Assumptions) -> dict:
    scenarios = {
        "direct_full": "Every machine pulls base+rescore from central",
        "direct_weights_only": "Weights-only release; dict reused via sha256 hard-link",
        "court_mirrors_full": "Court mirrors pull once per court (base+rescore)",
        "court_mirrors_weights_only": "Court mirrors, weights-only release",
    }
    rows = {}
    for key, label in scenarios.items():
        rows[key] = {"label": label, **usd(scenario_egress_gb(a, key), a)}

    tier_b_boxes = max(1, int(round(a.courts * min(1.0, a.tier_b_fraction * 2))))  # rough: not every court
    tier_b_monthly = (tier_b_boxes * a.tier_b_box_usd_capex) / a.amortize_months

    chosen = rows["court_mirrors_full"]
    gpu = {
        "label": "Always-on GPU inference API (rejected comparison line)",
        "instances_for_500_concurrent_note": (
            "One g4dn.xlarge-class instance (~$380/mo) already consumes most of a "
            "$500 ceiling before redundancy, cannot serve 500 concurrent typists "
            "with headroom, and does not fix WAN latency or blackout."
        ),
        "usd_per_month_one_instance": a.gpu_instance_usd_per_month,
        "usd_per_1000_users_one_instance": round(a.gpu_instance_usd_per_month / (a.machines / 1000.0), 2),
    }

    return {
        "assumptions": asdict(a),
        "scenarios": rows,
        "chosen_design": {
            "name": "Tier A local inference + central registry + court mirrors",
            "monthly_central_usd": chosen["total_usd_per_month"],
            "usd_per_1000_users": chosen["usd_per_1000_users"],
            "tier_b_capex_amortized_usd_per_month": round(tier_b_monthly, 2),
            "tier_b_boxes_modeled": tier_b_boxes,
            "note": (
                "Keystroke path never hits central. Central bill is egress+VM. "
                "Tier B boxes are capex where the hardware gate fails."
            ),
        },
        "rejected_gpu_line": gpu,
        "scale_10x": {
            "machines": a.machines * 10,
            "court_mirrors_full_usd": usd(scenario_egress_gb(
                Assumptions(**{**asdict(a), "machines": a.machines * 10, "courts": a.courts * 10}),
                "court_mirrors_full",
            ), Assumptions(**{**asdict(a), "machines": a.machines * 10, "courts": a.courts * 10})),
            "what_breaks": [
                "Release-day egress grows with courts, not typists — mirrors still win.",
                "Central poll load ~80 req/s of mostly 304s — still trivial.",
                "Tier B multi-worker capacity and on-site ops become the hard part if hardware-gate fail rate is high.",
                "Signed manifests + cohort rollout needed before fleet size makes a bad publish expensive.",
            ],
        },
    }


def render_text(report: dict) -> str:
    a = report["assumptions"]
    lines = [
        "IndicXlit courtroom runtime — cost model",
        f"  machines={a['machines']}  courts={a['courts']}  releases/mo={a['releases_per_month']}",
        f"  base={a['base_mb']} MB  rescore={a['rescore_mb']} MB  CDN=${a['cdn_usd_per_gb']}/GB",
        "",
        f"{'scenario':<28} {'egress_GB':>10} {'$/mo':>8} {'$/1k users':>12}",
        "-" * 62,
    ]
    for key, row in report["scenarios"].items():
        lines.append(
            f"{key:<28} {row['egress_gb']:>10.1f} {row['total_usd_per_month']:>8.2f} {row['usd_per_1000_users']:>12.2f}"
        )
    c = report["chosen_design"]
    g = report["rejected_gpu_line"]
    lines += [
        "",
        f"Chosen design central ≈ ${c['monthly_central_usd']}/mo "
        f"(${c['usd_per_1000_users']}/1k users); "
        f"Tier B amortized ≈ ${c['tier_b_capex_amortized_usd_per_month']}/mo",
        f"GPU comparison (1× always-on): ${g['usd_per_month_one_instance']}/mo — {g['instances_for_500_concurrent_note']}",
        "",
        "Assumptions are estimates to verify, not quotes.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Courtroom xlit cost model")
    p.add_argument("--machines", type=int, default=5000)
    p.add_argument("--courts", type=int, default=200)
    p.add_argument("--base-mb", type=float, default=138.3)
    p.add_argument("--rescore-mb", type=float, default=358.1)
    p.add_argument("--releases-per-month", type=float, default=1.0)
    p.add_argument("--cdn-usd-per-gb", type=float, default=0.085)
    p.add_argument("--central-vm-usd", type=float, default=30.0)
    p.add_argument("--gpu-usd", type=float, default=380.0)
    p.add_argument("--json", type=Path, default=None)
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    a = Assumptions(
        machines=args.machines,
        courts=args.courts,
        base_mb=args.base_mb,
        rescore_mb=args.rescore_mb,
        releases_per_month=args.releases_per_month,
        cdn_usd_per_gb=args.cdn_usd_per_gb,
        central_vm_usd_per_month=args.central_vm_usd,
        gpu_instance_usd_per_month=args.gpu_usd,
    )
    report = build_report(a)
    if not args.quiet:
        sys.stdout.write(render_text(report))
    out = args.json or (ROOT / "results" / "cost_model.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n")
    if not args.quiet:
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
