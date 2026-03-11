"""
Regression Runner — Re-run all studies and compare against saved baseline.

Captures stdout from each study, parses key metrics, saves to JSON.
After data expansion, re-run to diff results and flag what changed.

Usage:
    # Capture baseline (run BEFORE data expansion)
    python3 -m alert_overlay.regression_runner --save-baseline

    # Re-run and compare against baseline (run AFTER data expansion)
    python3 -m alert_overlay.regression_runner --compare

    # Run a single study
    python3 -m alert_overlay.regression_runner --study validated_combined_system

    # List all studies
    python3 -m alert_overlay.regression_runner --list
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

# ── Paths ────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
BASELINE_FILE = BASE_DIR / "regression_baseline.json"
RESULTS_DIR = BASE_DIR / "regression_results"

# ── Study Registry ───────────────────────────────────────────────────
# Ordered by importance: foundational → system → research
# Each entry: (module_name, description, tier)
#   tier 1 = Core system validation (Portfolio D, filters, backtest)
#   tier 2 = Key architectural studies (regime, tape, shorts)
#   tier 3 = Research studies (feature audit, v2, vwap)

STUDIES = [
    # Tier 1 — Core system
    ("validated_combined_system",
     "Portfolio D frozen system — long + short combined",
     1),
    ("backtest",
     "Full backtest harness (signal engine over bar data)",
     1),

    # Tier 2 — Architecture
    ("tape_research",
     "Tape model research — threshold sensitivity, regime gating",
     2),
    ("tape_threshold_study",
     "Tape permission threshold study — cross-regime analysis",
     2),
    ("recalibration_study",
     "Layered regime recalibration — L2 threshold variants",
     2),
    ("layered_regime_study",
     "3-layer hierarchical regime framework study",
     2),
    ("breakdown_retest_study",
     "BDR short — feature study + variant testing",
     2),
    ("r_baseline_and_red_trend_study",
     "R-normalized baseline + RED+TREND short identification",
     2),
    ("red_trend_short_study",
     "RED+TREND short — R-normalized performance",
     2),

    # Tier 3 — Research / diagnostics
    ("feature_audit",
     "Regime/tape ingredient quality — all 7+10 features",
     3),
    ("v2_ingredient_study",
     "Tape model v2 — component substitution variants",
     3),
    ("vwap_chop_study",
     "VWAP scoring audit + chop additive test",
     3),
    ("candle_anatomy_study",
     "Candle anatomy features for setup quality",
     3),
    ("tradability_study",
     "Graded directional tradability scoring model",
     3),
    ("sc_feature_study",
     "Second Chance setup feature diagnostics",
     3),
    ("short_research",
     "Native short-side research — RED day shorts",
     3),
    ("fb_feature_study",
     "Failed Bounce short feature study",
     3),
    ("fb_variant_study",
     "Failed Bounce structural redesign",
     3),
    ("fb_bounce_quality_study",
     "Failed Bounce — bounce quality measurement",
     3),
]


# ── Metric Parser ────────────────────────────────────────────────────

def parse_metrics(output: str) -> Dict:
    """
    Parse key metrics from study stdout.

    Extracts structured metrics like N=, PF=, Exp=, WR=, MaxDD=,
    Total R, effect sizes, and portfolio comparisons.
    Returns a dict of all found metrics.
    """
    metrics = {}

    # Count total lines and data bars
    lines = output.split("\n")
    metrics["_output_lines"] = len(lines)

    # ── Portfolio-level metrics ──────────────────────────────────
    # Pattern: N=42  WR=71.4%  PF=4.50  Exp=+0.675R  MaxDD=-1.23R
    pf_pattern = re.compile(
        r"N=\s*(\d+)\s+WR=\s*([\d.]+)%\s+PF=\s*([\d.inf]+)\s+"
        r"Exp=\s*([+\-]?[\d.]+)R?\s+MaxDD=\s*([+\-]?[\d.]+)"
    )
    for match in pf_pattern.finditer(output):
        # Find the label preceding this line
        pos = match.start()
        line_start = output.rfind("\n", 0, pos) + 1
        label_line = output[line_start:pos].strip()
        key = _clean_label(label_line) or f"portfolio_{len(metrics)}"
        metrics[key] = {
            "N": int(match.group(1)),
            "WR": float(match.group(2)),
            "PF": match.group(3),
            "Exp": float(match.group(4)),
            "MaxDD": float(match.group(5)),
        }

    # ── Simple N= patterns ──────────────────────────────────────
    n_pattern = re.compile(r"N=\s*(\d+)")
    n_matches = n_pattern.findall(output)
    if n_matches:
        metrics["_all_N_values"] = [int(n) for n in n_matches]
        metrics["_max_N"] = max(int(n) for n in n_matches)

    # ── Total R / PnL ───────────────────────────────────────────
    total_r = re.findall(r"Total[_ ]?R[=:]\s*([+\-]?[\d.]+)", output)
    if total_r:
        metrics["_total_R_values"] = [float(r) for r in total_r]

    pnl = re.findall(r"PnL[=:]\s*([+\-]?[\d.]+)", output)
    if pnl:
        metrics["_pnl_values"] = [float(p) for p in pnl]

    # ── Effect sizes ────────────────────────────────────────────
    effect_pattern = re.compile(r"effect[=:]\s*([+\-]?[\d.]+)R?", re.IGNORECASE)
    effects = effect_pattern.findall(output)
    if effects:
        metrics["_effect_sizes"] = [float(e) for e in effects]

    # ── Separation values ───────────────────────────────────────
    sep_pattern = re.compile(r"separation[=:]\s*([+\-]?[\d.]+)", re.IGNORECASE)
    seps = sep_pattern.findall(output)
    if seps:
        metrics["_separations"] = [float(s) for s in seps]

    # ── Win rate patterns ───────────────────────────────────────
    wr_pattern = re.compile(r"WR[=:]\s*([\d.]+)%")
    wrs = wr_pattern.findall(output)
    if wrs:
        metrics["_win_rates"] = [float(w) for w in wrs]

    # ── Profit factor patterns ──────────────────────────────────
    pf_vals = re.findall(r"PF[=:]\s*([\d.inf]+)", output)
    if pf_vals:
        metrics["_profit_factors"] = pf_vals

    # ── Trade count from data ───────────────────────────────────
    bar_count = re.findall(r"(\d+)\s+(?:bars|trades|signals|records)", output, re.IGNORECASE)
    if bar_count:
        metrics["_counts"] = [int(b) for b in bar_count]

    # ── Date range ──────────────────────────────────────────────
    date_pattern = re.compile(r"(\d{4}-\d{2}-\d{2})")
    dates = date_pattern.findall(output)
    if dates:
        metrics["_date_range"] = [min(dates), max(dates)]
        metrics["_unique_dates"] = len(set(dates))

    # ── Regime distribution ─────────────────────────────────────
    regime_pattern = re.compile(r"(GREEN|RED|FLAT|CHOPPY|TREND)\S*\s.*?N=\s*(\d+)", re.IGNORECASE)
    regimes = regime_pattern.findall(output)
    if regimes:
        metrics["_regime_counts"] = {r[0]: int(r[1]) for r in regimes}

    # ── VWAP diagnostic (specific to vwap_chop_study) ───────────
    vwap_pct = re.search(r"([\d.]+)%\s*(?:within|of trades)", output)
    if vwap_pct:
        metrics["_vwap_cluster_pct"] = float(vwap_pct.group(1))

    return metrics


def _clean_label(text: str) -> str:
    """Clean a label string for use as dict key."""
    text = re.sub(r"[^\w\s]", "", text)
    text = text.strip().replace(" ", "_").lower()
    return text[:60] if text else ""


# ── Study Runner ─────────────────────────────────────────────────────

def run_study(module_name: str, timeout: int = 300) -> Dict:
    """
    Run a study module and capture output + metrics.

    Returns dict with:
        stdout: raw output
        stderr: error output
        returncode: exit code
        elapsed_sec: wall time
        metrics: parsed metrics dict
    """
    cmd = [sys.executable, "-m", f"alert_overlay.{module_name}"]
    start = time.time()

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(BASE_DIR.parent),  # Run from parent of alert_overlay
        )
        elapsed = time.time() - start

        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
            "elapsed_sec": round(elapsed, 1),
            "metrics": parse_metrics(result.stdout) if result.returncode == 0 else {},
        }
    except subprocess.TimeoutExpired:
        return {
            "stdout": "",
            "stderr": f"TIMEOUT after {timeout}s",
            "returncode": -1,
            "elapsed_sec": timeout,
            "metrics": {},
        }
    except Exception as e:
        return {
            "stdout": "",
            "stderr": str(e),
            "returncode": -2,
            "elapsed_sec": time.time() - start,
            "metrics": {},
        }


# ── Comparison Engine ────────────────────────────────────────────────

def compare_metrics(baseline: Dict, current: Dict) -> List[str]:
    """
    Compare two metric dicts and return list of change descriptions.
    Flags changes above threshold as significant.
    """
    changes = []

    all_keys = set(list(baseline.keys()) + list(current.keys()))

    for key in sorted(all_keys):
        if key.startswith("_output_lines"):
            continue

        b_val = baseline.get(key)
        c_val = current.get(key)

        if b_val is None and c_val is not None:
            changes.append(f"  NEW  {key}: {_fmt_val(c_val)}")
            continue
        if b_val is not None and c_val is None:
            changes.append(f"  GONE {key}: was {_fmt_val(b_val)}")
            continue

        if isinstance(b_val, dict) and isinstance(c_val, dict):
            # Compare nested portfolio metrics
            for mk in set(list(b_val.keys()) + list(c_val.keys())):
                bm = b_val.get(mk)
                cm = c_val.get(mk)
                if bm != cm:
                    delta = _compute_delta(bm, cm)
                    severity = _severity(mk, bm, cm)
                    changes.append(f"  {severity} {key}.{mk}: {bm} → {cm}{delta}")
        elif isinstance(b_val, list) and isinstance(c_val, list):
            if b_val != c_val:
                if key == "_all_N_values" and len(b_val) != len(c_val):
                    changes.append(f"  DIFF {key}: {len(b_val)} entries → {len(c_val)} entries")
                elif key == "_date_range":
                    changes.append(f"  DIFF {key}: {b_val} → {c_val}")
                else:
                    b_summary = _list_summary(b_val)
                    c_summary = _list_summary(c_val)
                    if b_summary != c_summary:
                        changes.append(f"  DIFF {key}: {b_summary} → {c_summary}")
        else:
            if b_val != c_val:
                delta = _compute_delta(b_val, c_val)
                changes.append(f"  DIFF {key}: {b_val} → {c_val}{delta}")

    return changes


def _fmt_val(v):
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v)


def _compute_delta(old, new):
    try:
        old_f = float(old)
        new_f = float(new)
        if old_f != 0:
            pct = ((new_f - old_f) / abs(old_f)) * 100
            return f" ({pct:+.1f}%)"
        return f" (Δ{new_f - old_f:+.3f})"
    except (ValueError, TypeError):
        return ""


def _severity(metric_key: str, old, new) -> str:
    """Flag large changes in critical metrics."""
    try:
        old_f = float(old)
        new_f = float(new)
        pct_change = abs((new_f - old_f) / max(abs(old_f), 0.001)) * 100
    except (ValueError, TypeError):
        return "DIFF"

    # Critical metrics: Exp, PF, WR, MaxDD, N
    if metric_key in ("Exp", "PF", "WR", "MaxDD", "N"):
        if pct_change > 30:
            return "⚠ BIG"
        elif pct_change > 15:
            return "△ MED"
    return "DIFF"


def _list_summary(lst):
    """Summarize a numeric list."""
    try:
        nums = [float(x) for x in lst]
        return f"[n={len(nums)}, mean={sum(nums)/len(nums):.3f}, min={min(nums):.3f}, max={max(nums):.3f}]"
    except (ValueError, TypeError):
        return f"[n={len(lst)}]"


# ── Report Generator ─────────────────────────────────────────────────

def generate_comparison_report(
    baseline_data: Dict,
    current_data: Dict,
) -> str:
    """Generate a full comparison report."""
    lines = []
    lines.append("=" * 70)
    lines.append("REGRESSION COMPARISON REPORT")
    lines.append("=" * 70)
    lines.append(f"Baseline captured: {baseline_data.get('_meta', {}).get('timestamp', '?')}")
    lines.append(f"Baseline data range: {baseline_data.get('_meta', {}).get('date_range', '?')}")
    lines.append(f"Current run: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    # Data growth summary
    b_spy = baseline_data.get("_meta", {}).get("spy_bars", 0)
    c_spy_path = DATA_DIR / "SPY_5min.csv"
    c_spy = 0
    if c_spy_path.exists():
        with open(c_spy_path) as f:
            c_spy = sum(1 for _ in f) - 1
    if b_spy and c_spy:
        lines.append(f"SPY bars: {b_spy} → {c_spy} (+{c_spy - b_spy})")
        lines.append("")

    total_changes = 0
    critical_changes = 0

    for module_name, desc, tier in STUDIES:
        b_study = baseline_data.get(module_name, {})
        c_study = current_data.get(module_name, {})

        b_metrics = b_study.get("metrics", {})
        c_metrics = c_study.get("metrics", {})

        changes = compare_metrics(b_metrics, c_metrics)
        total_changes += len(changes)
        critical_changes += sum(1 for c in changes if "⚠ BIG" in c)

        # Status
        b_rc = b_study.get("returncode", -99)
        c_rc = c_study.get("returncode", -99)

        if c_rc != 0:
            status = "FAIL"
        elif not changes:
            status = "SAME"
        elif any("⚠ BIG" in c for c in changes):
            status = "⚠ CHANGED"
        else:
            status = "~ changed"

        tier_label = {1: "CORE", 2: "ARCH", 3: "RESEARCH"}[tier]
        lines.append(f"[{tier_label}] {module_name:35s} {status}")

        if changes:
            for ch in changes:
                lines.append(f"       {ch}")
            lines.append("")

    lines.append("=" * 70)
    lines.append(f"SUMMARY: {total_changes} changes across {len(STUDIES)} studies")
    if critical_changes:
        lines.append(f"         ⚠ {critical_changes} CRITICAL changes in core metrics")
    else:
        lines.append(f"         No critical changes detected")
    lines.append("=" * 70)

    return "\n".join(lines)


# ── Main ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Regression runner for all studies")
    parser.add_argument("--save-baseline", action="store_true",
                        help="Run all studies and save as baseline")
    parser.add_argument("--compare", action="store_true",
                        help="Run all studies and compare against baseline")
    parser.add_argument("--study", type=str, default=None,
                        help="Run a single study by name")
    parser.add_argument("--list", action="store_true",
                        help="List all registered studies")
    parser.add_argument("--tier", type=int, default=None,
                        help="Only run studies at this tier (1=core, 2=arch, 3=research)")
    parser.add_argument("--timeout", type=int, default=300,
                        help="Timeout per study in seconds (default: 300)")
    args = parser.parse_args()

    if args.list:
        print(f"\n{'='*70}")
        print(f"REGISTERED STUDIES ({len(STUDIES)})")
        print(f"{'='*70}\n")
        for module, desc, tier in STUDIES:
            tier_label = {1: "CORE", 2: "ARCH", 3: "RESEARCH"}[tier]
            print(f"  [{tier_label}] {module:35s} {desc}")
        print()
        return

    # Determine which studies to run
    if args.study:
        studies = [(m, d, t) for m, d, t in STUDIES if m == args.study]
        if not studies:
            print(f"Unknown study: {args.study}")
            print("Use --list to see available studies")
            return
    elif args.tier:
        studies = [(m, d, t) for m, d, t in STUDIES if t == args.tier]
    else:
        studies = STUDIES

    # Get SPY bar count for metadata
    spy_bars = 0
    spy_path = DATA_DIR / "SPY_5min.csv"
    if spy_path.exists():
        with open(spy_path) as f:
            spy_bars = sum(1 for _ in f) - 1

    # Get date range from SPY
    date_range = "unknown"
    if spy_path.exists():
        import csv
        with open(spy_path) as f:
            reader = csv.reader(f)
            next(reader)  # skip header
            rows = list(reader)
            if rows:
                date_range = f"{rows[0][0][:10]} → {rows[-1][0][:10]}"

    # Run studies
    print(f"\n{'='*70}")
    print(f"RUNNING {len(studies)} STUDIES")
    print(f"{'='*70}")
    print(f"Data: {spy_bars} SPY bars, range {date_range}")
    print(f"Timeout: {args.timeout}s per study\n")

    results = {}
    results["_meta"] = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "spy_bars": spy_bars,
        "date_range": date_range,
        "num_data_files": len(list(DATA_DIR.glob("*_5min.csv"))),
    }

    total_start = time.time()
    passed = 0
    failed = 0

    for i, (module, desc, tier) in enumerate(studies):
        tier_label = {1: "CORE", 2: "ARCH", 3: "RESEARCH"}[tier]
        print(f"[{i+1}/{len(studies)}] [{tier_label}] {module} ... ", end="", flush=True)

        result = run_study(module, timeout=args.timeout)

        if result["returncode"] == 0:
            n_metrics = len([k for k in result["metrics"] if not k.startswith("_output")])
            print(f"OK ({result['elapsed_sec']}s, {n_metrics} metrics)")
            passed += 1
        else:
            err_preview = result["stderr"][:100].replace("\n", " ")
            print(f"FAIL ({err_preview})")
            failed += 1

        # Store results (without full stdout to keep JSON manageable)
        results[module] = {
            "returncode": result["returncode"],
            "elapsed_sec": result["elapsed_sec"],
            "metrics": result["metrics"],
            "error": result["stderr"][:500] if result["returncode"] != 0 else "",
        }

    total_elapsed = time.time() - total_start

    print(f"\n{'='*70}")
    print(f"COMPLETE: {passed} passed, {failed} failed, {total_elapsed/60:.1f} min total")
    print(f"{'='*70}")

    # Save or compare
    if args.save_baseline:
        BASELINE_FILE.write_text(json.dumps(results, indent=2, default=str))
        print(f"\nBaseline saved to {BASELINE_FILE}")
        print(f"Run --compare after data expansion to see changes.\n")

    elif args.compare:
        if not BASELINE_FILE.exists():
            print(f"\nNo baseline found at {BASELINE_FILE}")
            print(f"Run --save-baseline first.\n")
            return

        baseline = json.loads(BASELINE_FILE.read_text())
        report = generate_comparison_report(baseline, results)
        print(f"\n{report}")

        # Save report to file
        RESULTS_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = RESULTS_DIR / f"regression_{ts}.txt"
        report_path.write_text(report)
        print(f"\nReport saved to {report_path}")

        # Also save current results
        current_path = RESULTS_DIR / f"results_{ts}.json"
        current_path.write_text(json.dumps(results, indent=2, default=str))
        print(f"Results saved to {current_path}\n")

    else:
        # Just print summary of metrics found
        print(f"\nMetric summary:")
        for module, desc, tier in studies:
            r = results.get(module, {})
            m = r.get("metrics", {})
            portfolio_keys = [k for k in m if not k.startswith("_")]
            if portfolio_keys:
                for pk in portfolio_keys[:3]:
                    pm = m[pk]
                    print(f"  {module}.{pk}: N={pm.get('N')} PF={pm.get('PF')} Exp={pm.get('Exp')}")
        print(f"\nUse --save-baseline to capture these results as the baseline.\n")


if __name__ == "__main__":
    main()
