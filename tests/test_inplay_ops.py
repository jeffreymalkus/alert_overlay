"""
Live Operational Check — Dual Universe (STATIC / IN_PLAY / BOTH)

Tests the in-play panel API endpoints against a RUNNING dashboard server.
Verifies:
  1. Add in-play symbol → universe tags update correctly
  2. Adding static symbol to in-play → tagged BOTH
  3. Alerts carry correct universe label
  4. Remove in-play → reverts to STATIC or unsubscribes
  5. Clear all in-play → resets everything
  6. Export CSV includes universe column

Prerequisites:
  - Dashboard running at http://localhost:8877
  - At least one static symbol active (e.g., AAPL)

Usage:
    python -m alert_overlay.test_inplay_ops [--port 8877]
"""

import argparse
import json
import sys
import time
import urllib.request
import urllib.error

PASS = "\033[92m PASS \033[0m"
FAIL = "\033[91m FAIL \033[0m"
SKIP = "\033[93m SKIP \033[0m"

results = []


def api(base, method, path, data=None):
    """Call dashboard API."""
    url = f"{base}{path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(
        url, data=body, method=method,
        headers={"Content-Type": "application/json"} if body else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode()
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw


def check(label, condition, detail=""):
    status = PASS if condition else FAIL
    results.append((label, condition))
    print(f"  [{status}] {label}" + (f"  ({detail})" if detail else ""))
    return condition


def main():
    parser = argparse.ArgumentParser(description="Dual Universe Operational Check")
    parser.add_argument("--port", type=int, default=8877)
    args = parser.parse_args()
    base = f"http://localhost:{args.port}"

    print("=" * 80)
    print("DUAL UNIVERSE — LIVE OPERATIONAL CHECK")
    print("=" * 80)

    # ── 0. Connectivity ──
    print("\n── 0. Connectivity ──")
    code, status = api(base, "GET", "/api/status")
    if not check("Dashboard reachable", code == 200):
        print("  Cannot reach dashboard. Is it running?")
        sys.exit(1)

    symbols = list(status.get("symbols", {}).keys())
    check("At least one symbol active", len(symbols) > 0, f"{len(symbols)} symbols")
    if not symbols:
        print("  No symbols active. Start dashboard with symbols first.")
        sys.exit(1)

    # Pick a static symbol for BOTH testing
    static_sym = symbols[0]
    # Use a symbol NOT in the watchlist for pure IN_PLAY testing
    test_in_play = "ZZZZ"  # unlikely to be in watchlist

    print(f"\n  Static symbol for BOTH test: {static_sym}")
    print(f"  Fake symbol for IN_PLAY test: {test_in_play}")

    # ── 1. Initial in-play state ──
    print("\n── 1. Initial In-Play State ──")
    code, ip_data = api(base, "GET", "/api/in-play")
    check("GET /api/in-play returns 200", code == 200)
    initial_in_play = ip_data.get("symbols", []) if isinstance(ip_data, dict) else []
    print(f"  Current in-play: {initial_in_play}")

    # ── 2. Add static symbol to in-play → should become BOTH ──
    print(f"\n── 2. Add '{static_sym}' to In-Play (expect BOTH) ──")
    code, resp = api(base, "POST", "/api/add-in-play", {"symbol": static_sym})
    check(f"POST add-in-play {static_sym} → 200 or 409", code in (200, 409))

    time.sleep(0.5)
    code, status = api(base, "GET", "/api/status")
    sym_info = status.get("symbols", {}).get(static_sym, {})
    uni = sym_info.get("universe", "???")
    check(f"{static_sym} universe = BOTH", uni == "BOTH", f"got: {uni}")

    # Verify in-play list includes it
    code, ip_data = api(base, "GET", "/api/in-play")
    in_play_list = ip_data.get("symbols", []) if isinstance(ip_data, dict) else []
    check(f"{static_sym} in in-play list", static_sym in in_play_list)

    # ── 3. Duplicate add → 409 ──
    print(f"\n── 3. Duplicate Add (expect 409) ──")
    code, resp = api(base, "POST", "/api/add-in-play", {"symbol": static_sym})
    check("Duplicate add-in-play → 409", code == 409)

    # ── 4. Test alert carries universe field ──
    print("\n── 4. Test Alert Universe Tagging ──")
    code, resp = api(base, "GET", "/api/test-alert")
    if code == 200:
        alert = resp.get("alert", {}) if isinstance(resp, dict) else {}
        has_universe = "universe" in alert
        check("Test alert has 'universe' field", has_universe,
              f"universe={alert.get('universe', 'MISSING')}")
    else:
        check("Test alert endpoint reachable", False, f"code={code}")

    # ── 5. Check alerts endpoint for universe field ──
    print("\n── 5. Alert History Universe Field ──")
    code, alerts = api(base, "GET", "/api/alerts")
    if code == 200 and isinstance(alerts, list) and len(alerts) > 0:
        latest = alerts[0]
        has_uni = "universe" in latest
        check("Latest alert has 'universe' key", has_uni,
              f"universe={latest.get('universe', 'MISSING')}")
    else:
        check("Alert history available", False, f"code={code}, len={len(alerts) if isinstance(alerts, list) else '?'}")

    # ── 6. Export CSV includes universe column ──
    print("\n── 6. Export CSV Universe Column ──")
    code, csv_data = api(base, "GET", "/api/export-alerts")
    if code == 200 and isinstance(csv_data, str):
        header_line = csv_data.split("\n")[0] if csv_data else ""
        check("Export CSV has 'universe' column", "universe" in header_line,
              f"headers: {header_line[:120]}")
    else:
        check("Export CSV available", isinstance(csv_data, str) and len(csv_data) > 0)

    # ── 7. Remove in-play → revert to STATIC ──
    print(f"\n── 7. Remove '{static_sym}' from In-Play (expect STATIC) ──")
    code, resp = api(base, "POST", "/api/remove-in-play", {"symbol": static_sym})
    check(f"POST remove-in-play {static_sym} → 200", code == 200)

    time.sleep(0.5)
    code, status = api(base, "GET", "/api/status")
    sym_info = status.get("symbols", {}).get(static_sym, {})
    uni = sym_info.get("universe", "???")
    check(f"{static_sym} reverted to STATIC", uni == "STATIC", f"got: {uni}")

    # Verify removed from in-play list
    code, ip_data = api(base, "GET", "/api/in-play")
    in_play_list = ip_data.get("symbols", []) if isinstance(ip_data, dict) else []
    check(f"{static_sym} no longer in in-play list", static_sym not in in_play_list)

    # ── 8. Clear All ──
    print("\n── 8. Clear All In-Play ──")
    # Add a symbol back first
    api(base, "POST", "/api/add-in-play", {"symbol": static_sym})
    time.sleep(0.3)
    code, resp = api(base, "POST", "/api/clear-in-play", {})
    check("POST clear-in-play → 200", code == 200)

    time.sleep(0.5)
    code, ip_data = api(base, "GET", "/api/in-play")
    in_play_list = ip_data.get("symbols", []) if isinstance(ip_data, dict) else []
    check("In-play list empty after clear", len(in_play_list) == 0, f"got: {in_play_list}")

    # Static symbol should be back to STATIC
    code, status = api(base, "GET", "/api/status")
    sym_info = status.get("symbols", {}).get(static_sym, {})
    uni = sym_info.get("universe", "???")
    check(f"{static_sym} back to STATIC after clear", uni == "STATIC", f"got: {uni}")

    # ── 9. in_play.txt persistence ──
    print("\n── 9. File Persistence ──")
    from pathlib import Path
    ip_file = Path(__file__).parent / "in_play.txt"
    check("in_play.txt exists", ip_file.exists())
    if ip_file.exists():
        content = ip_file.read_text().strip()
        check("in_play.txt is empty after clear", content == "", f"content: {content[:60]}")

    # ── 10. In-Play History Capture ──
    print("\n── 10. In-Play History Capture ──")
    code, hist_data = api(base, "GET", "/api/iplog")
    check("GET /api/iplog → 200", code == 200)

    if code == 200 and isinstance(hist_data, dict):
        history = hist_data.get("history", [])
        snapshots = hist_data.get("snapshots", [])

        # We did add, remove, add, clear — should have at least 3 events
        check("History log has events", len(history) >= 3,
              f"got {len(history)} events")

        # Check for ADD event
        add_events = [h for h in history if h.get("action") == "ADD"]
        check("History contains ADD events", len(add_events) > 0)

        # Check for CLEAR event
        clear_events = [h for h in history if h.get("action") == "CLEAR"]
        check("History contains CLEAR events", len(clear_events) > 0)

        # Check snapshot exists for today
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        today_snap = [s for s in snapshots if s.get("date") == today]
        check(f"Snapshot exists for {today}", len(today_snap) > 0)

        # Verify history file on disk
        hist_file = Path(__file__).parent / "in_play_history.csv"
        check("in_play_history.csv exists on disk", hist_file.exists())

        snap_dir = Path(__file__).parent / "in_play_snapshots"
        check("in_play_snapshots/ directory exists", snap_dir.exists())
    else:
        check("In-play history API responded", False)

    # ════════════════════════════════════════════════════════════
    #  SUMMARY
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 80)
    passed = sum(1 for _, ok in results if ok)
    failed = sum(1 for _, ok in results if not ok)
    total = len(results)
    print(f"RESULTS: {passed}/{total} passed, {failed} failed")

    if failed > 0:
        print("\nFailed checks:")
        for label, ok in results:
            if not ok:
                print(f"  ✗ {label}")
    else:
        print("\nAll checks passed. Dual universe operational.")
    print("=" * 80)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
