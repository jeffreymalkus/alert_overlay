#!/usr/bin/env python3
"""
session_report.py — First-session validation report for StrategyManager live engine.

Parses dashboard log output and reports on 7 monitoring criteria:
  1. Total alert count
  2. Alert count by strategy
  3. Bad entry/stop/target values
  4. Duplicate / spam behavior
  5. Startup & open latency
  6. Runtime exceptions
  7. In-play / universe tagging

Usage:
  # Pipe live output:
  python -m alert_overlay.dashboard 2>&1 | tee session.log
  # Then after session:
  python -m alert_overlay.session_report session.log

  # Or analyze an existing log:
  python -m alert_overlay.session_report /path/to/session.log
"""

import re
import sys
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path


# ── Log line patterns ──────────────────────────────────────────────

RE_TIMESTAMP = re.compile(r"^(\d{2}:\d{2}:\d{2})")
RE_LEVEL = re.compile(r"\[(INFO|WARNING|ERROR|CRITICAL)\]")

# Alerts:  [AAPL] ALERT: LONG SECOND_CHANCE @ 152.50 perm=0.312
RE_ALERT = re.compile(
    r"\[(\w+)\] ALERT: (LONG|SHORT) (\S+) @ ([\d.]+)"
    r"(?: perm=([\d.+-]+))?"
)

# Blocked: [AAPL] BLOCKED long SECOND_CHANCE: tape perm=-0.123 < 0.10
RE_BLOCKED = re.compile(
    r"\[(\w+)\] BLOCKED (long|short) (\S+): tape perm=([\d.+-]+)"
)

# Bar count: [AAPL] bar #10 @ 10:00 C=150.50 | signals=2
RE_BAR = re.compile(
    r"\[(\w+)\] bar #(\d+) @ (\S+) C=([\d.]+) \| signals=(\d+)"
)

# Setup ready: [AAPL] Ready (1/64, universe=STATIC).
RE_READY = re.compile(
    r"\[(\w+)\] Ready \((\d+)/(\d+), universe=(\w+)\)"
)

# Errors: [AAPL] Failed to set up: ...
RE_ERROR = re.compile(r"\[(ERROR|CRITICAL)\] (.+)")

# Engine label: Engine: StrategyManager (step)
RE_ENGINE = re.compile(r"Engine: (.+?)\.")

# Dashboard started: Dashboard running at ...
RE_DASHBOARD_START = re.compile(r"Dashboard running at")

# IBKR connected
RE_CONNECTED = re.compile(r"Connected\.")

# All runners set up
RE_ALL_SETUP = re.compile(r"All (\d+) runners set up")

# reqMktData subscribed per symbol
RE_SUBSCRIBED = re.compile(r"\[(\w+)\] reqMktData subscribed")

# In-play loaded
RE_IN_PLAY_LOADED = re.compile(r"Loaded (\d+) in-play symbols: (.+)")

# Legacy engine warning
RE_LEGACY = re.compile(r"LEGACY ENGINE ACTIVE")

# Caught up (warmup bars)
RE_CAUGHT_UP = re.compile(
    r"\[(\w+)\] Caught up: (\d+) 1-min bars"
)

# ATR warmed
RE_ATR = re.compile(r"\[(\w+)\] ATR warmed: (\d+) days")


def parse_time(s: str) -> datetime:
    return datetime.strptime(s, "%H:%M:%S")


def analyze(log_lines: list[str]) -> dict:
    """Parse log lines and return structured report data."""

    report = {
        "engine": "UNKNOWN",
        "legacy_active": False,
        "total_alerts": 0,
        "alerts_by_strategy": Counter(),
        "alerts_by_symbol": Counter(),
        "alerts_by_direction": Counter(),
        "blocked_by_strategy": Counter(),
        "blocked_total": 0,
        "bad_values": [],          # alerts with suspect entry/stop/target
        "duplicate_suspects": [],  # rapid-fire same symbol+setup
        "errors": [],
        "warnings": [],
        "symbols_setup": {},       # symbol → universe
        "symbols_failed": [],
        "in_play_count": 0,
        "bars_by_symbol": {},      # symbol → max bar count seen
        "first_timestamp": None,
        "last_timestamp": None,
        "dashboard_start_time": None,
        "connected_time": None,
        "all_setup_time": None,
        "first_bar_time": None,
        "first_alert_time": None,
        "total_runners": 0,
    }

    # For duplicate detection: track recent alerts per (symbol, strategy)
    recent_alerts = defaultdict(list)  # (sym, strat) → [timestamp_str, ...]

    for line in log_lines:
        line = line.strip()
        if not line:
            continue

        # Extract timestamp
        ts_match = RE_TIMESTAMP.match(line)
        ts_str = ts_match.group(1) if ts_match else None
        if ts_str:
            if report["first_timestamp"] is None:
                report["first_timestamp"] = ts_str
            report["last_timestamp"] = ts_str

        # Engine label
        m = RE_ENGINE.search(line)
        if m:
            report["engine"] = m.group(1)

        # Legacy flag
        if RE_LEGACY.search(line):
            report["legacy_active"] = True

        # Dashboard start
        if RE_DASHBOARD_START.search(line):
            report["dashboard_start_time"] = ts_str

        # Connected
        if RE_CONNECTED.search(line) and "IBKR" not in line:
            if report["connected_time"] is None:
                report["connected_time"] = ts_str

        # All runners setup
        m = RE_ALL_SETUP.search(line)
        if m:
            report["all_setup_time"] = ts_str
            report["total_runners"] = int(m.group(1))

        # Symbol ready
        m = RE_READY.search(line)
        if m:
            sym, idx, total, universe = m.group(1), m.group(2), m.group(3), m.group(4)
            report["symbols_setup"][sym] = universe

        # In-play loaded
        m = RE_IN_PLAY_LOADED.search(line)
        if m:
            report["in_play_count"] = int(m.group(1))

        # Bar progress
        m = RE_BAR.search(line)
        if m:
            sym, bar_num = m.group(1), int(m.group(2))
            report["bars_by_symbol"][sym] = max(
                report["bars_by_symbol"].get(sym, 0), bar_num
            )
            if report["first_bar_time"] is None:
                report["first_bar_time"] = ts_str

        # ── ALERTS ──
        m = RE_ALERT.search(line)
        if m:
            sym, direction, setup, entry_str = m.group(1), m.group(2), m.group(3), m.group(4)
            entry = float(entry_str)
            perm = float(m.group(5)) if m.group(5) else None

            report["total_alerts"] += 1
            report["alerts_by_strategy"][setup] += 1
            report["alerts_by_symbol"][sym] += 1
            report["alerts_by_direction"][direction] += 1

            if report["first_alert_time"] is None:
                report["first_alert_time"] = ts_str

            # Bad value check: entry <= 0, or suspiciously large/small
            if entry <= 0 or entry > 50000:
                report["bad_values"].append(
                    f"{ts_str} [{sym}] {direction} {setup} entry={entry} — out of range"
                )

            # Duplicate/spam check: same (sym, setup) within 1 bar (5 min)
            key = (sym, setup)
            recent_alerts[key].append(ts_str)
            if len(recent_alerts[key]) >= 2:
                prev = recent_alerts[key][-2]
                try:
                    dt = parse_time(ts_str) - parse_time(prev)
                    if dt < timedelta(minutes=6):
                        report["duplicate_suspects"].append(
                            f"{ts_str} [{sym}] {setup} fired again ({dt.seconds}s after previous)"
                        )
                except Exception:
                    pass

        # ── BLOCKED ──
        m = RE_BLOCKED.search(line)
        if m:
            sym, direction, setup, perm = m.group(1), m.group(2), m.group(3), m.group(4)
            report["blocked_total"] += 1
            report["blocked_by_strategy"][setup] += 1

        # ── ERRORS ──
        m = RE_ERROR.search(line)
        if m:
            level, msg = m.group(1), m.group(2)
            if level == "ERROR":
                report["errors"].append(f"{ts_str} {msg}")
            elif level == "CRITICAL":
                report["errors"].append(f"{ts_str} [CRITICAL] {msg}")

        # Warnings
        if "[WARNING]" in line:
            report["warnings"].append(f"{ts_str} {line.split('[WARNING]')[-1].strip()}")

        # Failed symbols — extract symbol name (skip level tags like [ERROR])
        if "Failed to set up" in line or "Failed to subscribe" in line:
            sym_m = re.search(r"\[(?!INFO|ERROR|WARNING|CRITICAL)(\w+)\]", line)
            if sym_m:
                report["symbols_failed"].append(sym_m.group(1))

    return report


def format_report(r: dict) -> str:
    """Format the analysis into a readable session report."""

    lines = []
    lines.append("=" * 65)
    lines.append("  STRATEGYMANAGER — FIRST SESSION VALIDATION REPORT")
    lines.append("=" * 65)
    lines.append("")

    # Engine
    lines.append(f"Engine:    {r['engine']}")
    if r["legacy_active"]:
        lines.append("⚠ LEGACY ENGINE WAS ACTIVE (rollback flag on)")
    lines.append(f"Session:   {r['first_timestamp'] or '?'} → {r['last_timestamp'] or '?'}")
    lines.append(f"Runners:   {r['total_runners']}")
    lines.append("")

    # ── 1. Total alert count ──
    lines.append("─" * 45)
    lines.append("1. TOTAL ALERT COUNT")
    lines.append(f"   Alerts fired:   {r['total_alerts']}")
    lines.append(f"   Alerts blocked: {r['blocked_total']} (tape permission gate)")
    lines.append(f"   LONG:  {r['alerts_by_direction'].get('LONG', 0)}")
    lines.append(f"   SHORT: {r['alerts_by_direction'].get('SHORT', 0)}")
    lines.append("")

    # ── 2. Alert count by strategy ──
    lines.append("─" * 45)
    lines.append("2. ALERTS BY STRATEGY")
    if r["alerts_by_strategy"]:
        for strat, count in r["alerts_by_strategy"].most_common():
            blocked = r["blocked_by_strategy"].get(strat, 0)
            lines.append(f"   {strat:<22} {count:>3} fired"
                         + (f"  ({blocked} blocked)" if blocked else ""))
    else:
        lines.append("   (none)")
    lines.append("")
    if r["alerts_by_symbol"]:
        lines.append("   By symbol (top 10):")
        for sym, count in r["alerts_by_symbol"].most_common(10):
            lines.append(f"     {sym:<8} {count:>3}")
    lines.append("")

    # ── 3. Bad entry/stop/target values ──
    lines.append("─" * 45)
    lines.append("3. BAD ENTRY/STOP/TARGET VALUES")
    if r["bad_values"]:
        for bv in r["bad_values"]:
            lines.append(f"   ⚠ {bv}")
    else:
        lines.append("   PASS — no suspicious values detected")
    lines.append("")

    # ── 4. Duplicate / spam ──
    lines.append("─" * 45)
    lines.append("4. DUPLICATE / SPAM BEHAVIOR")
    if r["duplicate_suspects"]:
        for ds in r["duplicate_suspects"]:
            lines.append(f"   ⚠ {ds}")
    else:
        lines.append("   PASS — no rapid-fire duplicates detected")
    lines.append("")

    # ── 5. Startup / open latency ──
    lines.append("─" * 45)
    lines.append("5. STARTUP & OPEN LATENCY")
    lines.append(f"   Dashboard start:  {r['dashboard_start_time'] or '?'}")
    lines.append(f"   IBKR connected:   {r['connected_time'] or 'N/A (offline?)'}")
    lines.append(f"   All runners ready: {r['all_setup_time'] or '?'}")
    lines.append(f"   First bar:        {r['first_bar_time'] or '(none seen)'}")
    lines.append(f"   First alert:      {r['first_alert_time'] or '(none fired)'}")
    if r["dashboard_start_time"] and r["all_setup_time"]:
        try:
            startup_delta = parse_time(r["all_setup_time"]) - parse_time(r["dashboard_start_time"])
            lines.append(f"   Startup→Ready:    {startup_delta.seconds}s")
        except Exception:
            pass
    lines.append("")

    # ── 6. Runtime exceptions ──
    lines.append("─" * 45)
    lines.append("6. RUNTIME EXCEPTIONS")
    if r["errors"]:
        for err in r["errors"][:20]:  # cap at 20
            lines.append(f"   ✗ {err}")
        if len(r["errors"]) > 20:
            lines.append(f"   ... and {len(r['errors']) - 20} more")
    else:
        lines.append("   PASS — zero errors")
    if r["warnings"]:
        lines.append(f"   Warnings: {len(r['warnings'])}")
        for w in r["warnings"][:5]:
            lines.append(f"     {w}")
    if r["symbols_failed"]:
        lines.append(f"   Failed symbols: {', '.join(r['symbols_failed'])}")
    lines.append("")

    # ── 7. In-play / universe tagging ──
    lines.append("─" * 45)
    lines.append("7. IN-PLAY / UNIVERSE TAGGING")
    lines.append(f"   In-play symbols loaded: {r['in_play_count']}")
    universe_counts = Counter(r["symbols_setup"].values())
    for uni, count in universe_counts.most_common():
        lines.append(f"   {uni}: {count} symbols")
    if not r["symbols_setup"]:
        lines.append("   (no setup data — offline mode?)")
    lines.append("")

    # ── Bar progress summary ──
    if r["bars_by_symbol"]:
        bars_vals = list(r["bars_by_symbol"].values())
        lines.append("─" * 45)
        lines.append("BAR PROGRESS")
        lines.append(f"   Symbols receiving bars: {len(bars_vals)}")
        lines.append(f"   Min bars: {min(bars_vals)}  Max bars: {max(bars_vals)}  "
                     f"Avg: {sum(bars_vals)/len(bars_vals):.0f}")
        zero_bar_syms = [s for s, u in r["symbols_setup"].items()
                         if s not in r["bars_by_symbol"]]
        if zero_bar_syms:
            lines.append(f"   ⚠ Zero bars received: {', '.join(zero_bar_syms[:10])}")
        lines.append("")

    # ── Verdict ──
    lines.append("=" * 65)
    issues = len(r["bad_values"]) + len(r["duplicate_suspects"]) + len(r["errors"])
    if issues == 0:
        lines.append("VERDICT: CLEAN SESSION — no issues detected")
    else:
        lines.append(f"VERDICT: {issues} issue(s) found — review above")
    lines.append("=" * 65)

    return "\n".join(lines)


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m alert_overlay.session_report <logfile>")
        print("       python -m alert_overlay.dashboard 2>&1 | tee session.log")
        sys.exit(1)

    log_path = Path(sys.argv[1])
    if not log_path.exists():
        print(f"File not found: {log_path}")
        sys.exit(1)

    lines = log_path.read_text().splitlines()
    print(f"Parsing {len(lines)} log lines from {log_path.name}...")
    report = analyze(lines)
    print(format_report(report))

    # Also dump raw JSON for programmatic use
    json_path = log_path.with_suffix(".report.json")
    json_data = {k: (dict(v) if isinstance(v, (Counter, defaultdict)) else v)
                 for k, v in report.items()}
    json_path.write_text(json.dumps(json_data, indent=2))
    print(f"\nRaw data: {json_path}")


if __name__ == "__main__":
    main()
