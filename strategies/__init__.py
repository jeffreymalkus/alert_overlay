"""
New strategy framework — independent of engine.py.
Phase 1: shared pipeline (in-play, regime, rejection, quality).
Phase 2: SecondChance_Sniper + FL_AntiChop_Only.
Phase 3: Spencer_A_Tier_Only + HitchHiker_Program_Quality.
Phase 4: EMA_FPIP_ATier + BDR_SHORT.
Phase 5: EMA9_FirstTouch_Only + Backside_Structure_Only.

All strategies promoted — 6-month backtest (Sep 2025 → Mar 2026, 74 symbols, IEX+IBKR data):
  SC_SNIPER:   N=25,  PF=1.21, Exp=+0.118R, TotalR=+3.0R   → PROMOTED (auto)
  FL_ANTICHOP: N=111, PF=1.34, Exp=+0.116R, TotalR=+12.9R  → PROMOTED (auto)
  SP_ATIER:    N=13,  PF=1.19, Exp=+0.079R, TotalR=+1.0R   → PROMOTED (auto)
  HH_QUALITY:  N=46,  PF=1.64, Exp=+0.152R, TotalR=+7.0R   → PROMOTED (auto)
  EMA_FPIP:    N=24,  PF=2.00, Exp=+0.250R, TotalR=+6.0R   → PROMOTED (owner override — Train PF=0.40, edge concentrated in recent months)
  BDR_SHORT:   N=1,   PF=inf,  Exp=+0.000R, TotalR=+0.0R   → PROMOTED (owner override — low N, needs live validation)
  EMA9_FT:     N=4,   PF=2.00, Exp=+0.250R, TotalR=+1.0R   → PROMOTED (owner override — needs 1-min data for proper N)
  BS_STRUCT:   N=10,  PF=inf,  Exp=+1.000R, TotalR=+10.0R  → PROMOTED (auto — zero losses)
  COMB LONG:   N=233, PF=1.55, Exp=+0.176R, TotalR=+40.9R
  COMB ALL:    N=234, PF=1.55, Exp=+0.175R, TotalR=+40.9R

Config: ip_mode=hybrid (manual in-play curation OR proxy qualification)
Data: 6-month Alpaca IEX 1-min + 5-min, dual-timeframe live pipeline (1-min base → 5-min engine)
"""
