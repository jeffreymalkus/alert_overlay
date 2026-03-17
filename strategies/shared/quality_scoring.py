"""
Quality scoring — composite stock + market + setup factors → A/B/C tier.
"""

from typing import Dict, Tuple

from .signal_schema import QualityTier


class QualityScorer:
    """Composite quality scorer for the strategy pipeline."""

    def __init__(self, cfg):
        self.cfg = cfg

    def score(self, stock_factors: Dict[str, float],
              market_factors: Dict[str, float],
              setup_factors: Dict[str, float]) -> Tuple[QualityTier, int]:
        """
        Compute composite quality score and tier.

        Each factor is 0.0-1.0 (or unbounded for some).
        Returns (tier, composite_score 0-10).

        Factor groups:
          stock_factors:
            - in_play_score (0-10, from InPlayProxy)
            - rs_market (-1 to +1, relative strength vs SPY)
            - rs_sector (-1 to +1, relative strength vs sector)
            - volume_profile (0-1, current rvol quality)

          market_factors:
            - regime_score (0-1, GREEN=1.0, FLAT=0.5, RED=0.0)
            - alignment_score (0-1, SPY above VWAP + EMA aligned = 1.0)

          setup_factors:
            - trigger_quality (0-1, trigger bar body/volume/range)
            - structure_quality (0-1, setup-specific structure score)
            - confluence_count (0+, number of confirming factors)
        """
        # ── Stock component (0-3 points) ──
        stock_score = 0.0

        ip = stock_factors.get("in_play_score", 0.0)
        # Thresholds rescaled for 0-7 score (v3: gap removed, max=7)
        if ip >= 5.0:
            stock_score += 1.5
        elif ip >= 3.5:
            stock_score += 1.0
        elif ip >= 2.0:
            stock_score += 0.5

        rs_mkt = stock_factors.get("rs_market", 0.0)
        if rs_mkt > 0.003:  # outperforming SPY by 0.3%+
            stock_score += 1.0
        elif rs_mkt > 0:
            stock_score += 0.5

        vol_profile = stock_factors.get("volume_profile", 0.0)
        if vol_profile >= 0.7:
            stock_score += 0.5

        stock_score = min(stock_score, 3.0)

        # ── Market component (0-2 points) ──
        market_score = 0.0

        regime = market_factors.get("regime_score", 0.0)
        market_score += regime * 0.5  # GREEN=0.5, FLAT=0.25 (reduced from 1.0)

        alignment = market_factors.get("alignment_score", 0.0)
        market_score += alignment * 1.0

        market_score = min(market_score, 2.0)

        # ── Hard reject: trigger_quality < 0.20 → garbage bar shape ──
        # Data-backed: 9 trades below 0.20, avg -0.576R, 22.2% WR
        trigger_q = setup_factors.get("trigger_quality", 0.0)
        if trigger_q < 0.20:
            return QualityTier.C_TIER, 0

        # ── Setup component (0-5 points) ──
        setup_score = 0.0

        setup_score += trigger_q * 2.0  # 0-2 points

        structure_q = setup_factors.get("structure_quality", 0.0)
        setup_score += structure_q * 2.0  # 0-2 points

        confluence = setup_factors.get("confluence_count", 0)
        setup_score += min(confluence * 0.5, 1.0)  # 0-1 point

        setup_score = min(setup_score, 5.0)

        # ── Composite ──
        composite = stock_score + market_score + setup_score
        composite = min(round(composite), 10)

        # ── Tier assignment ──
        if composite >= self.cfg.quality_a_min:
            tier = QualityTier.A_TIER
        elif composite >= self.cfg.quality_b_min:
            tier = QualityTier.B_TIER
        else:
            tier = QualityTier.C_TIER

        return tier, composite
