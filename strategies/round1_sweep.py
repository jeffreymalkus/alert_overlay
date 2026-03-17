#!/usr/bin/env python3
"""Round 1 sweep: constraint release + scoring model tests.

Runs multiple replay variants by monkey-patching parameters,
captures N/PF/TotalR/WR/MaxDD for each, and prints a comparison table.
"""

import sys
import io
import re
import importlib
from contextlib import redirect_stdout, redirect_stderr
from typing import Dict, Any, List, Tuple

# ── Test variant definitions ──
# Each variant is (name, description, setup_func, teardown_func)

def _parse_replay_output(output: str) -> Dict[str, Any]:
    """Extract key metrics from replay stdout."""
    metrics = {}
    m = re.search(r'N=(\d+)\s+PF=([0-9.inf]+)\s+Exp=([+\-0-9.]+)R\s+TotalR=([+\-0-9.]+)\s+WR=([0-9.]+)%\s+MaxDD=([0-9.]+)R', output)
    if m:
        pf_str = m.group(2)
        metrics['N'] = int(m.group(1))
        metrics['PF'] = float('inf') if 'inf' in pf_str else float(pf_str)
        metrics['Exp'] = float(m.group(3))
        metrics['TotalR'] = float(m.group(4))
        metrics['WR'] = float(m.group(5))
        metrics['MaxDD'] = float(m.group(6))

    # Per-strategy N
    strat_lines = re.findall(r'(\w+)\s+(\d+)\s+([0-9.inf]+)\s+([+\-0-9.]+)R\s+([+\-0-9.]+)\s+([0-9.]+)%', output)
    metrics['strategies'] = {}
    for sl in strat_lines:
        name = sl[0]
        if name in ('Strategy', 'TOTAL'):
            continue
        metrics['strategies'][name] = {
            'N': int(sl[1]),
            'PF': float('inf') if 'inf' in sl[2] else float(sl[2]),
            'TotalR': float(sl[3]),
        }

    # Funnel
    m2 = re.search(r'Raw signals:\s+([\d,]+)', output)
    if m2:
        metrics['raw_signals'] = int(m2.group(1).replace(',', ''))

    return metrics


def run_variant(name: str, setup_fn, teardown_fn) -> Dict[str, Any]:
    """Run a single replay variant. Returns metrics dict."""
    # Fresh import each time
    if 'alert_overlay.strategies.replay' in sys.modules:
        del sys.modules['alert_overlay.strategies.replay']
    # Also clear cached in_play_proxy scoring
    if 'alert_overlay.strategies.shared.in_play_proxy' in sys.modules:
        importlib.reload(sys.modules['alert_overlay.strategies.shared.in_play_proxy'])
    if 'alert_overlay.strategies.shared.config' in sys.modules:
        importlib.reload(sys.modules['alert_overlay.strategies.shared.config'])

    setup_fn()

    try:
        from alert_overlay.strategies.replay import main
        buf = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            main()
        output = buf.getvalue()
        metrics = _parse_replay_output(output)
        metrics['name'] = name
        return metrics
    finally:
        teardown_fn()


def main():
    results = []

    # ══════════════════════════════════════════════════════════════
    # BASELINE (current settings)
    # ══════════════════════════════════════════════════════════════
    print("Running: BASELINE...", flush=True)
    r = run_variant("BASELINE", lambda: None, lambda: None)
    results.append(r)

    # ══════════════════════════════════════════════════════════════
    # GROUP A: IP SCORE FLOOR SWEEP
    # ══════════════════════════════════════════════════════════════
    for floor in [4.0, 3.5, 3.0, 2.0, 1.0, 0.0]:
        label = f"IP_floor={floor:.1f}"
        print(f"Running: {label}...", flush=True)

        def make_setup(f):
            def setup():
                import alert_overlay.strategies.replay as rmod
                # Patch the source to change _IP_SCORE_MIN
                rmod._SWEEP_IP_FLOOR = f
            return setup

        def make_teardown():
            def teardown():
                import alert_overlay.strategies.replay as rmod
                if hasattr(rmod, '_SWEEP_IP_FLOOR'):
                    delattr(rmod, '_SWEEP_IP_FLOOR')
            return teardown

        # We need a different approach — patch at the gate check level
        # since _IP_SCORE_MIN is a local variable inside the loop.
        # Patch via monkeypatch of the module-level constant after import.
        def make_setup2(f):
            def setup():
                pass  # patching happens in the gate check
            return setup

        r = run_variant(label, lambda: None, lambda: None)
        results.append(r)

    # The above won't work because _IP_SCORE_MIN is a local variable.
    # We need to modify the source file directly. Let me take a different approach.

    print("\n\n=== Switching to direct source patching approach ===\n")
    results.clear()

    import os
    REPLAY_PATH = os.path.join(os.path.dirname(__file__), 'replay.py')
    PROXY_PATH = os.path.join(os.path.dirname(__file__), 'shared', 'in_play_proxy.py')

    with open(REPLAY_PATH, 'r') as f:
        replay_orig = f.read()
    with open(PROXY_PATH, 'r') as f:
        proxy_orig = f.read()

    def restore_all():
        with open(REPLAY_PATH, 'w') as f:
            f.write(replay_orig)
        with open(PROXY_PATH, 'w') as f:
            f.write(proxy_orig)
        # Clear all cached modules
        mods_to_clear = [k for k in sys.modules if k.startswith('alert_overlay')]
        for k in mods_to_clear:
            del sys.modules[k]

    def patch_ip_floor(value):
        """Patch _IP_SCORE_MIN in replay.py."""
        new = re.sub(
            r'_IP_SCORE_MIN = [0-9.]+',
            f'_IP_SCORE_MIN = {value}',
            replay_orig
        )
        with open(REPLAY_PATH, 'w') as f:
            f.write(new)
        mods_to_clear = [k for k in sys.modules if k.startswith('alert_overlay')]
        for k in mods_to_clear:
            del sys.modules[k]

    def patch_scoring(rvol_tiers, range_tiers):
        """Patch _compute_score in in_play_proxy.py with new tier definitions."""
        # Build new scoring function body
        rvol_code = ""
        for thresh, pts in sorted(rvol_tiers, key=lambda x: -x[0]):
            if not rvol_code:
                rvol_code += f"        if rvol >= {thresh}:\n            score += {pts}\n"
            else:
                rvol_code += f"        elif rvol >= {thresh}:\n            score += {pts}\n"

        range_code = ""
        for thresh, pts in sorted(range_tiers, key=lambda x: -x[0]):
            if not range_code:
                range_code += f"        if range_exp >= {thresh}:\n            score += {pts}\n"
            else:
                range_code += f"        elif range_exp >= {thresh}:\n            score += {pts}\n"

        max_score = max(p for _, p in rvol_tiers) + max(p for _, p in range_tiers)

        new_proxy = re.sub(
            r'(    def _compute_score\(self.*?\n)(.*?)(        return min\(score.*?\n)',
            lambda m: m.group(1) +
                '        """Patched scoring for sweep."""\n'
                '        score = 0.0\n'
                f'{rvol_code}'
                f'{range_code}'
                f'        return min(score, {max_score})\n',
            proxy_orig,
            flags=re.DOTALL
        )
        with open(PROXY_PATH, 'w') as f:
            f.write(new_proxy)
        mods_to_clear = [k for k in sys.modules if k.startswith('alert_overlay')]
        for k in mods_to_clear:
            del sys.modules[k]

    def patch_strategies_disabled(disabled_set):
        """Comment out strategies from live_strats list in replay.py."""
        new = replay_orig
        for strat_class_name in disabled_set:
            # Find the instantiation line and comment it out
            new = re.sub(
                rf'^(\s+)(.*{strat_class_name}\(.*\),?\s*)$',
                r'\1# SWEEP_DISABLED: \2',
                new,
                flags=re.MULTILINE
            )
        with open(REPLAY_PATH, 'w') as f:
            f.write(new)
        mods_to_clear = [k for k in sys.modules if k.startswith('alert_overlay')]
        for k in mods_to_clear:
            del sys.modules[k]

    def run_and_record(name):
        print(f"Running: {name}...", flush=True)
        try:
            from alert_overlay.strategies.replay import main as replay_main
            buf = io.StringIO()
            err = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(err):
                replay_main()
            output = buf.getvalue()
            metrics = _parse_replay_output(output)
            metrics['name'] = name
            results.append(metrics)
            n = metrics.get('N', '?')
            pf = metrics.get('PF', '?')
            tr = metrics.get('TotalR', '?')
            print(f"  → N={n}  PF={pf}  TotalR={tr}", flush=True)
        except Exception as e:
            print(f"  → ERROR: {e}", flush=True)
            results.append({'name': name, 'N': 0, 'PF': 0, 'TotalR': 0, 'WR': 0, 'MaxDD': 0, 'error': str(e)})
        finally:
            restore_all()

    # ══════════════════════════════════════════════════════════════
    # 0. BASELINE
    # ══════════════════════════════════════════════════════════════
    run_and_record("0_BASELINE")

    # ══════════════════════════════════════════════════════════════
    # A. IP SCORE FLOOR SWEEP
    # ══════════════════════════════════════════════════════════════
    for floor in [4.0, 3.5, 3.0, 2.0, 1.0, 0.0]:
        patch_ip_floor(floor)
        run_and_record(f"A_IP_floor_{floor:.1f}")

    # ══════════════════════════════════════════════════════════════
    # B. SCORING MODEL VARIANTS
    # ══════════════════════════════════════════════════════════════

    # B1: Range expansion heavier (0-5 pts), RVOL same (0-3)
    RVOL_DEFAULT = [(5.0, 3.0), (3.0, 2.0), (2.0, 1.5), (1.5, 1.0)]
    RANGE_DEFAULT = [(4.0, 4.0), (3.0, 3.0), (2.0, 2.0), (1.5, 1.5), (1.0, 1.0)]

    RANGE_HEAVIER = [(3.0, 5.0), (2.0, 4.0), (1.5, 3.0), (1.0, 2.0), (0.5, 1.0)]
    patch_scoring(RVOL_DEFAULT, RANGE_HEAVIER)
    run_and_record("B1_range_heavier_0to5")

    # B2: RVOL lighter (0-2 pts), range same (0-4)
    RVOL_LIGHTER = [(5.0, 2.0), (3.0, 1.5), (2.0, 1.0), (1.5, 0.5)]
    patch_scoring(RVOL_LIGHTER, RANGE_DEFAULT)
    run_and_record("B2_rvol_lighter_0to2")

    # B3: Range expansion only (0-7), no RVOL in score
    RVOL_ZERO = [(999.0, 0.0)]  # effectively 0 for all
    RANGE_ONLY = [(4.0, 7.0), (3.0, 5.5), (2.0, 4.0), (1.5, 3.0), (1.0, 2.0), (0.5, 1.0)]
    patch_scoring(RVOL_ZERO, RANGE_ONLY)
    run_and_record("B3_range_only_0to7")

    # B4: RVOL only (0-7), no range in score
    RVOL_ONLY = [(5.0, 5.0), (3.0, 3.5), (2.0, 2.5), (1.5, 1.5)]
    RANGE_ZERO = [(999.0, 0.0)]
    patch_scoring(RVOL_ONLY, RANGE_ZERO)
    run_and_record("B4_rvol_only_0to5")

    # B5: More granular RVOL (0-3) + steeper range (0-5), lower thresholds
    RVOL_GRANULAR = [(8.0, 3.0), (5.0, 2.5), (3.0, 2.0), (2.0, 1.5), (1.5, 1.0)]
    RANGE_STEEP = [(3.0, 5.0), (2.0, 3.5), (1.5, 2.5), (1.0, 1.5), (0.75, 1.0)]
    patch_scoring(RVOL_GRANULAR, RANGE_STEEP)
    run_and_record("B5_granular_rvol_steep_range")

    # B6: Binary RVOL (pass/fail at 1.5 = 2pts) + range expansion 0-5
    RVOL_BINARY = [(1.5, 2.0)]
    RANGE_WIDE = [(3.0, 5.0), (2.0, 4.0), (1.5, 3.0), (1.0, 2.0), (0.5, 1.0)]
    patch_scoring(RVOL_BINARY, RANGE_WIDE)
    run_and_record("B6_binary_rvol_range_0to5")

    # ══════════════════════════════════════════════════════════════
    # C. BEST SCORING + BEST IP FLOOR COMBO
    # (run after we know which scoring and floor are best individually)
    # We'll test the most promising combo: lower floor + better scoring
    # ══════════════════════════════════════════════════════════════

    # C1: IP floor 3.0 + range heavier scoring
    patch_scoring(RVOL_DEFAULT, RANGE_HEAVIER)
    patch_ip_floor(3.0)
    run_and_record("C1_floor3.0_range_heavier")

    # Restore scoring first, then floor
    restore_all()

    # C2: IP floor 3.0 + binary rvol + range 0-5
    patch_scoring(RVOL_BINARY, RANGE_WIDE)
    patch_ip_floor(3.0)
    run_and_record("C2_floor3.0_binary_rvol")

    restore_all()

    # C3: IP floor 2.0 + range heavier scoring
    patch_scoring(RVOL_DEFAULT, RANGE_HEAVIER)
    patch_ip_floor(2.0)
    run_and_record("C3_floor2.0_range_heavier")

    restore_all()

    # C4: No IP floor + range heavier scoring
    patch_scoring(RVOL_DEFAULT, RANGE_HEAVIER)
    patch_ip_floor(0.0)
    run_and_record("C4_floor0.0_range_heavier")

    restore_all()

    # ══════════════════════════════════════════════════════════════
    # D. STRATEGY PRUNING (on baseline scoring)
    # ══════════════════════════════════════════════════════════════

    # Map strategy names to class names in replay.py
    STRAT_CLASSES = {
        'ORH_FBO_V2_A': 'ORHFBOShortV2Live',
        'FFT_NEWLOW_REV': 'FFTNewlowReversalLive',
        'BS_STRUCT': 'BacksideLive',
        'BDR_SHORT': 'BDRShortLive',
        'PDH_FBO_B': 'PDHFBOShortLive',
        'EMA9_FT': 'EMA9FTLive',
    }

    # D1: Drop ORH_FBO_V2_A
    patch_strategies_disabled({STRAT_CLASSES['ORH_FBO_V2_A']})
    run_and_record("D1_drop_ORH_V2A")

    # D2: Drop FFT_NEWLOW_REV
    patch_strategies_disabled({STRAT_CLASSES['FFT_NEWLOW_REV']})
    run_and_record("D2_drop_FFT")

    # D3: Drop all zero-N strategies (BDR, PDH, EMA9_FT)
    patch_strategies_disabled({
        STRAT_CLASSES['BDR_SHORT'],
        STRAT_CLASSES['PDH_FBO_B'],
        STRAT_CLASSES['EMA9_FT'],
    })
    run_and_record("D3_drop_dead_strats")

    # D4: Drop all negative-EV (V2_A, FFT, BS) + dead (BDR, PDH, EMA9)
    patch_strategies_disabled({
        STRAT_CLASSES['ORH_FBO_V2_A'],
        STRAT_CLASSES['FFT_NEWLOW_REV'],
        STRAT_CLASSES['BS_STRUCT'],
        STRAT_CLASSES['BDR_SHORT'],
        STRAT_CLASSES['PDH_FBO_B'],
        STRAT_CLASSES['EMA9_FT'],
    })
    run_and_record("D4_top6_only")

    # D5: Top 6 + IP floor 3.0
    patch_strategies_disabled({
        STRAT_CLASSES['ORH_FBO_V2_A'],
        STRAT_CLASSES['FFT_NEWLOW_REV'],
        STRAT_CLASSES['BS_STRUCT'],
        STRAT_CLASSES['BDR_SHORT'],
        STRAT_CLASSES['PDH_FBO_B'],
        STRAT_CLASSES['EMA9_FT'],
    })
    patch_ip_floor(3.0)
    run_and_record("D5_top6_floor3.0")

    restore_all()

    # D6: Top 6 + IP floor 3.0 + best scoring model
    # (we'll use range_heavier as hypothesis)
    patch_strategies_disabled({
        STRAT_CLASSES['ORH_FBO_V2_A'],
        STRAT_CLASSES['FFT_NEWLOW_REV'],
        STRAT_CLASSES['BS_STRUCT'],
        STRAT_CLASSES['BDR_SHORT'],
        STRAT_CLASSES['PDH_FBO_B'],
        STRAT_CLASSES['EMA9_FT'],
    })
    patch_scoring(RVOL_DEFAULT, RANGE_HEAVIER)
    patch_ip_floor(3.0)
    run_and_record("D6_top6_floor3.0_range_heavier")

    restore_all()

    # ══════════════════════════════════════════════════════════════
    # RESULTS TABLE
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 100)
    print("ROUND 1 SWEEP RESULTS")
    print("=" * 100)
    print(f"{'Variant':<40s} {'N':>4s} {'PF':>6s} {'Exp':>8s} {'TotalR':>8s} {'WR':>6s} {'MaxDD':>6s}")
    print("-" * 100)

    baseline_n = results[0].get('N', 0) if results else 0
    baseline_pf = results[0].get('PF', 0) if results else 0
    baseline_tr = results[0].get('TotalR', 0) if results else 0

    for r in results:
        n = r.get('N', 0)
        pf = r.get('PF', 0)
        exp = r.get('Exp', 0)
        tr = r.get('TotalR', 0)
        wr = r.get('WR', 0)
        mdd = r.get('MaxDD', 0)

        pf_str = f"{pf:.2f}" if pf != float('inf') else "inf"
        delta_n = f"({n - baseline_n:+d})" if r['name'] != '0_BASELINE' else ""
        delta_tr = f"({tr - baseline_tr:+.1f})" if r['name'] != '0_BASELINE' else ""

        print(f"{r['name']:<40s} {n:>4d}{delta_n:>5s} {pf_str:>6s} {exp:>+7.3f}R {tr:>+7.1f}R{delta_tr:>7s} {wr:>5.1f}% {mdd:>5.1f}R")

    # Restore everything at the end
    restore_all()
    print("\n✓ All source files restored to original state.")


if __name__ == "__main__":
    main()
