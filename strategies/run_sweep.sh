#!/bin/bash
# Round 1 sweep — runs each variant as a separate process
cd "$(dirname "$0")/../.."

REPLAY_PY="alert_overlay/strategies/replay.py"
PROXY_PY="alert_overlay/strategies/shared/in_play_proxy.py"
RESULTS_FILE="alert_overlay/strategies/sweep_results.csv"

# Save originals
cp "$REPLAY_PY" "${REPLAY_PY}.bak"
cp "$PROXY_PY" "${PROXY_PY}.bak"

# CSV header
echo "variant,N,PF,Exp,TotalR,WR,MaxDD" > "$RESULTS_FILE"

run_variant() {
    local name="$1"
    echo "Running: $name..." >&2
    OUTPUT=$(python -m alert_overlay.strategies.replay 2>/dev/null)
    METRICS=$(echo "$OUTPUT" | grep "STRUCTURAL TARGET" | head -1)

    N=$(echo "$METRICS" | grep -oP 'N=\K\d+')
    PF=$(echo "$METRICS" | grep -oP 'PF=\K[0-9.inf]+')
    EXP=$(echo "$METRICS" | grep -oP 'Exp=\K[+\-0-9.]+')
    TR=$(echo "$METRICS" | grep -oP 'TotalR=\K[+\-0-9.]+')
    WR=$(echo "$METRICS" | grep -oP 'WR=\K[0-9.]+')
    MDD=$(echo "$METRICS" | grep -oP 'MaxDD=\K[0-9.]+')

    echo "$name,$N,$PF,$EXP,$TR,$WR,$MDD" >> "$RESULTS_FILE"
    echo "  → N=$N  PF=$PF  TotalR=$TR" >&2

    # Restore originals for next run
    cp "${REPLAY_PY}.bak" "$REPLAY_PY"
    cp "${PROXY_PY}.bak" "$PROXY_PY"
}

patch_ip_floor() {
    sed -i "s/_IP_SCORE_MIN = [0-9.]*/_IP_SCORE_MIN = $1/" "$REPLAY_PY"
}

patch_scoring() {
    # Args: python code for the scoring function body
    local SCORE_BODY="$1"
    python3 -c "
import re
with open('$PROXY_PY', 'r') as f:
    content = f.read()

new_body = '''    def _compute_score(self, abs_gap: float, rvol: float,
                       dolvol: float, range_exp: float) -> float:
        \"\"\"Patched scoring for sweep.\"\"\"
        score = 0.0
$SCORE_BODY
        return min(score, 10.0)
'''

content = re.sub(
    r'    def _compute_score\(self.*?        return min\(score.*?\n',
    new_body,
    content,
    flags=re.DOTALL
)
with open('$PROXY_PY', 'w') as f:
    f.write(content)
"
}

disable_strategies() {
    # Args: space-separated class names to disable
    for cls in $@; do
        sed -i "s/^\(.*${cls}(.*\)$/# DISABLED: \1/" "$REPLAY_PY"
    done
}

# ══════════════════════════════════════════════════════════════
# 0. BASELINE
# ══════════════════════════════════════════════════════════════
run_variant "0_BASELINE"

# ══════════════════════════════════════════════════════════════
# A. IP SCORE FLOOR SWEEP
# ══════════════════════════════════════════════════════════════
for floor in 4.0 3.5 3.0 2.0 1.0 0.0; do
    patch_ip_floor "$floor"
    run_variant "A_IP_floor_${floor}"
done

# ══════════════════════════════════════════════════════════════
# B. SCORING MODEL VARIANTS (at baseline floor 4.5)
# ══════════════════════════════════════════════════════════════

# B1: Range heavier (0-5), RVOL same (0-3)
patch_scoring "
        if rvol >= 5.0: score += 3.0
        elif rvol >= 3.0: score += 2.0
        elif rvol >= 2.0: score += 1.5
        elif rvol >= 1.5: score += 1.0
        if range_exp >= 3.0: score += 5.0
        elif range_exp >= 2.0: score += 4.0
        elif range_exp >= 1.5: score += 3.0
        elif range_exp >= 1.0: score += 2.0
        elif range_exp >= 0.5: score += 1.0"
run_variant "B1_range_heavier"

# B2: RVOL lighter (0-2), range same (0-4)
patch_scoring "
        if rvol >= 5.0: score += 2.0
        elif rvol >= 3.0: score += 1.5
        elif rvol >= 2.0: score += 1.0
        elif rvol >= 1.5: score += 0.5
        if range_exp >= 4.0: score += 4.0
        elif range_exp >= 3.0: score += 3.0
        elif range_exp >= 2.0: score += 2.0
        elif range_exp >= 1.5: score += 1.5
        elif range_exp >= 1.0: score += 1.0"
run_variant "B2_rvol_lighter"

# B3: Range only (0-7), no RVOL in score
patch_scoring "
        if range_exp >= 4.0: score += 7.0
        elif range_exp >= 3.0: score += 5.5
        elif range_exp >= 2.0: score += 4.0
        elif range_exp >= 1.5: score += 3.0
        elif range_exp >= 1.0: score += 2.0
        elif range_exp >= 0.5: score += 1.0"
run_variant "B3_range_only"

# B4: RVOL only, no range in score
patch_scoring "
        if rvol >= 5.0: score += 5.0
        elif rvol >= 3.0: score += 3.5
        elif rvol >= 2.0: score += 2.5
        elif rvol >= 1.5: score += 1.5"
run_variant "B4_rvol_only"

# B5: Continuous-ish scoring (more granular tiers)
patch_scoring "
        if rvol >= 8.0: score += 3.0
        elif rvol >= 5.0: score += 2.5
        elif rvol >= 3.0: score += 2.0
        elif rvol >= 2.0: score += 1.5
        elif rvol >= 1.5: score += 1.0
        if range_exp >= 3.0: score += 5.0
        elif range_exp >= 2.0: score += 3.5
        elif range_exp >= 1.5: score += 2.5
        elif range_exp >= 1.0: score += 1.5
        elif range_exp >= 0.75: score += 1.0"
run_variant "B5_granular"

# B6: Binary RVOL (2 pts if >=1.5) + range 0-5
patch_scoring "
        if rvol >= 1.5: score += 2.0
        if range_exp >= 3.0: score += 5.0
        elif range_exp >= 2.0: score += 4.0
        elif range_exp >= 1.5: score += 3.0
        elif range_exp >= 1.0: score += 2.0
        elif range_exp >= 0.5: score += 1.0"
run_variant "B6_binary_rvol"

# ══════════════════════════════════════════════════════════════
# C. COMBO: BEST SCORING + LOWER FLOOR
# ══════════════════════════════════════════════════════════════

# C1: floor 3.0 + range heavier
patch_scoring "
        if rvol >= 5.0: score += 3.0
        elif rvol >= 3.0: score += 2.0
        elif rvol >= 2.0: score += 1.5
        elif rvol >= 1.5: score += 1.0
        if range_exp >= 3.0: score += 5.0
        elif range_exp >= 2.0: score += 4.0
        elif range_exp >= 1.5: score += 3.0
        elif range_exp >= 1.0: score += 2.0
        elif range_exp >= 0.5: score += 1.0"
patch_ip_floor 3.0
run_variant "C1_floor3.0_range_heavier"

# C2: floor 3.0 + binary rvol
patch_scoring "
        if rvol >= 1.5: score += 2.0
        if range_exp >= 3.0: score += 5.0
        elif range_exp >= 2.0: score += 4.0
        elif range_exp >= 1.5: score += 3.0
        elif range_exp >= 1.0: score += 2.0
        elif range_exp >= 0.5: score += 1.0"
patch_ip_floor 3.0
run_variant "C2_floor3.0_binary_rvol"

# C3: floor 2.0 + range heavier
patch_scoring "
        if rvol >= 5.0: score += 3.0
        elif rvol >= 3.0: score += 2.0
        elif rvol >= 2.0: score += 1.5
        elif rvol >= 1.5: score += 1.0
        if range_exp >= 3.0: score += 5.0
        elif range_exp >= 2.0: score += 4.0
        elif range_exp >= 1.5: score += 3.0
        elif range_exp >= 1.0: score += 2.0
        elif range_exp >= 0.5: score += 1.0"
patch_ip_floor 2.0
run_variant "C3_floor2.0_range_heavier"

# C4: floor 0.0 (no score gate) + range heavier
patch_scoring "
        if rvol >= 5.0: score += 3.0
        elif rvol >= 3.0: score += 2.0
        elif rvol >= 2.0: score += 1.5
        elif rvol >= 1.5: score += 1.0
        if range_exp >= 3.0: score += 5.0
        elif range_exp >= 2.0: score += 4.0
        elif range_exp >= 1.5: score += 3.0
        elif range_exp >= 1.0: score += 2.0
        elif range_exp >= 0.5: score += 1.0"
patch_ip_floor 0.0
run_variant "C4_floor0.0_range_heavier"

# ══════════════════════════════════════════════════════════════
# D. STRATEGY PRUNING
# ══════════════════════════════════════════════════════════════

# D1: Drop ORH_FBO_V2_A
disable_strategies "ORHFBOShortV2Live"
run_variant "D1_drop_V2A"

# D2: Drop FFT_NEWLOW_REV
disable_strategies "FFTNewlowReversalLive"
run_variant "D2_drop_FFT"

# D3: Drop all dead strats (BDR, PDH, EMA9_FT)
disable_strategies "BDRShortLive PDHFBOShortLive EMA9FTLive"
run_variant "D3_drop_dead"

# D4: Top 6 only (drop V2_A, FFT, BS, BDR, PDH, EMA9_FT)
disable_strategies "ORHFBOShortV2Live FFTNewlowReversalLive BacksideLive BDRShortLive PDHFBOShortLive EMA9FTLive"
run_variant "D4_top6_only"

# D5: Top 6 + IP floor 3.0
disable_strategies "ORHFBOShortV2Live FFTNewlowReversalLive BacksideLive BDRShortLive PDHFBOShortLive EMA9FTLive"
patch_ip_floor 3.0
run_variant "D5_top6_floor3.0"

# D6: Top 6 + floor 3.0 + range heavier scoring
disable_strategies "ORHFBOShortV2Live FFTNewlowReversalLive BacksideLive BDRShortLive PDHFBOShortLive EMA9FTLive"
patch_ip_floor 3.0
patch_scoring "
        if rvol >= 5.0: score += 3.0
        elif rvol >= 3.0: score += 2.0
        elif rvol >= 2.0: score += 1.5
        elif rvol >= 1.5: score += 1.0
        if range_exp >= 3.0: score += 5.0
        elif range_exp >= 2.0: score += 4.0
        elif range_exp >= 1.5: score += 3.0
        elif range_exp >= 1.0: score += 2.0
        elif range_exp >= 0.5: score += 1.0"
run_variant "D6_top6_floor3.0_range_heavier"

# ══════════════════════════════════════════════════════════════
# CLEANUP
# ══════════════════════════════════════════════════════════════
cp "${REPLAY_PY}.bak" "$REPLAY_PY"
cp "${PROXY_PY}.bak" "$PROXY_PY"
rm -f "${REPLAY_PY}.bak" "${PROXY_PY}.bak"

echo "" >&2
echo "═══════════════════════════════════════════════════════" >&2
echo "SWEEP COMPLETE — results in $RESULTS_FILE" >&2
echo "═══════════════════════════════════════════════════════" >&2

# Pretty-print results
echo ""
echo "════════════════════════════════════════════════════════════════════════════════════"
echo "ROUND 1 SWEEP RESULTS"
echo "════════════════════════════════════════════════════════════════════════════════════"
printf "%-40s %4s %6s %8s %8s %6s %6s\n" "Variant" "N" "PF" "Exp" "TotalR" "WR" "MaxDD"
echo "────────────────────────────────────────────────────────────────────────────────────"
tail -n +2 "$RESULTS_FILE" | while IFS=, read -r name n pf exp tr wr mdd; do
    printf "%-40s %4s %6s %7sR %7sR %5s%% %5sR\n" "$name" "$n" "$pf" "$exp" "$tr" "$wr" "$mdd"
done
