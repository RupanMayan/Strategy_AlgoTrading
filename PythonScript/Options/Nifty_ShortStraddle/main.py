"""
main.py — Entry point for Nifty Short Straddle (Partial) strategy
═══════════════════════════════════════════════════════════════════
Production run:
    python main.py

Manual / diagnostic one-shots (comment run(), uncomment one below):
    strategy.check_connection()       → verify OpenAlgo + show funds
    strategy.check_margin_now()       → test margin guard without trading
    strategy.manual_entry()           → force entry now (all filters run)
    strategy.manual_exit()            → close all active legs immediately
    strategy.show_state()             → dump state + SL levels + DTE info
    strategy.manual_bootstrap_vix()   → fetch/refresh vix_history.csv from NSE
"""

from src.strategy_core import StrategyCore

if __name__ == "__main__":
    strategy = StrategyCore()

    # ── Production ─────────────────────────────────────────────────────────────
    strategy.run()

    # ── Testing (uncomment one at a time) ──────────────────────────────────────
    # strategy.check_connection()
    # strategy.check_margin_now()
    # strategy.manual_entry()
    # strategy.manual_exit()
    # strategy.show_state()
    # strategy.manual_bootstrap_vix()
