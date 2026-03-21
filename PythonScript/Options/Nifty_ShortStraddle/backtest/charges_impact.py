"""
Brokerage & Charges Impact — Indian F&O (NSE Options)
=====================================================
Calculates real-world charges per trade and their impact on backtest P&L.

Charges for SELL (short) options — MIS intraday:
  1. Brokerage         : Rs.20 per order (flat — Zerodha/Dhan/OpenAlgo)
  2. STT               : 0.0625% on SELL side premium (options)
  3. Exchange Txn      : 0.0495% on premium (NSE F&O)
  4. SEBI Charges      : Rs.10 per crore of turnover
  5. GST               : 18% on (brokerage + exchange + SEBI)
  6. Stamp Duty        : 0.003% on BUY side (state-dependent, using Maharashtra)

Per short straddle round trip:
  Entry  = 2 SELL orders (CE + PE)
  Exit   = 1-2 BUY orders (SL hit or square-off)
  Total  = 3-4 orders per trade

References:
  - Zerodha brokerage calculator: https://zerodha.com/brokerage-calculator
  - NSE circulars on transaction charges
  - SEBI circular on turnover fees (effective Oct 2024)
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

CSV_PATH = Path(__file__).parent / "69bd6847d9c4596c0d68c658_1774020709.csv"

# ── Charge rates (as of 2025) ────────────────────────────────────────────────
BROKERAGE_PER_ORDER = 20.0          # Rs. flat per order
STT_SELL_PCT        = 0.0625 / 100  # 0.0625% on sell-side premium
EXCHANGE_TXN_PCT    = 0.0495 / 100  # 0.0495% NSE options
SEBI_PER_CRORE      = 10.0          # Rs.10 per crore turnover
GST_PCT             = 18.0 / 100    # 18% on brokerage + exchange + SEBI
STAMP_BUY_PCT       = 0.003 / 100   # 0.003% on buy-side premium
QTY                 = 65            # Lot size


def parse_trades(path: Path) -> list[dict]:
    trades = {}
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            idx_str = row["Index"].strip()
            try:
                idx_f = float(idx_str)
            except ValueError:
                continue

            if idx_f == int(idx_f):
                trades[int(idx_f)] = {
                    "index": int(idx_f),
                    "date": row["Entry-Date"].strip(),
                    "raw_pnl": float(row["P/L"] or 0),
                    "legs": [],
                }
            else:
                parent = int(idx_f)
                if parent not in trades:
                    continue
                kind = row.get("Instrument-Kind", "").strip()
                if kind not in ("CE", "PE"):
                    continue
                trades[parent]["legs"].append({
                    "kind": kind,
                    "entry_px": float(row["Entry-Price"] or 0),
                    "exit_px": float(row["ExitPrice"] or 0),
                    "sl_hit": "Stop Loss" in row.get("Remarks", ""),
                })

    return [t for t in sorted(trades.values(), key=lambda x: x["index"]) if len(t["legs"]) == 2]


def calc_charges(trade: dict) -> dict:
    """Calculate all charges for a single trade (2-leg round trip)."""
    legs = trade["legs"]

    # Entry: 2 SELL orders (CE + PE)
    # Exit: could be 1-2 BUY orders depending on partial/full exit
    num_orders = 4  # 2 sell + 2 buy (worst case, partial also has 2 buy)

    # Turnover (total premium × qty for each order)
    sell_turnover = sum(l["entry_px"] * QTY for l in legs)  # Entry sells
    buy_turnover = sum(l["exit_px"] * QTY for l in legs)    # Exit buys
    total_turnover = sell_turnover + buy_turnover

    # 1. Brokerage
    brokerage = BROKERAGE_PER_ORDER * num_orders

    # 2. STT — only on SELL side premium
    stt = sell_turnover * STT_SELL_PCT

    # 3. Exchange transaction charges — on total turnover
    exchange_txn = total_turnover * EXCHANGE_TXN_PCT

    # 4. SEBI charges
    sebi = total_turnover / 1_00_00_000 * SEBI_PER_CRORE  # per crore

    # 5. GST — 18% on (brokerage + exchange + SEBI)
    gst = (brokerage + exchange_txn + sebi) * GST_PCT

    # 6. Stamp duty — on BUY side only
    stamp = buy_turnover * STAMP_BUY_PCT

    total_charges = brokerage + stt + exchange_txn + sebi + gst + stamp

    return {
        "brokerage": brokerage,
        "stt": stt,
        "exchange_txn": exchange_txn,
        "sebi": sebi,
        "gst": gst,
        "stamp": stamp,
        "total": total_charges,
        "turnover": total_turnover,
    }


def main() -> None:
    trades = parse_trades(CSV_PATH)
    print(f"\nAnalyzing charges for {len(trades)} trades\n")

    # Calculate charges for each trade
    all_charges = []
    for t in trades:
        ch = calc_charges(t)
        ch["date"] = t["date"]
        ch["raw_pnl"] = t["raw_pnl"]
        ch["net_pnl"] = t["raw_pnl"] - ch["total"]
        all_charges.append(ch)

    # ── Overall summary ──────────────────────────────────────────────────────
    total_raw = sum(c["raw_pnl"] for c in all_charges)
    total_charges = sum(c["total"] for c in all_charges)
    total_net = sum(c["net_pnl"] for c in all_charges)

    avg_charge = total_charges / len(all_charges)
    avg_brokerage = sum(c["brokerage"] for c in all_charges) / len(all_charges)
    avg_stt = sum(c["stt"] for c in all_charges) / len(all_charges)
    avg_exchange = sum(c["exchange_txn"] for c in all_charges) / len(all_charges)
    avg_gst = sum(c["gst"] for c in all_charges) / len(all_charges)
    avg_stamp = sum(c["stamp"] for c in all_charges) / len(all_charges)
    avg_sebi = sum(c["sebi"] for c in all_charges) / len(all_charges)

    print("=" * 85)
    print("  CHARGES BREAKDOWN PER TRADE (average)")
    print("=" * 85)
    print(f"  {'Component':<30} {'Avg/Trade':>12} {'Total (all trades)':>20} {'% of Charges':>14}")
    print(f"  {'─'*30} {'─'*12} {'─'*20} {'─'*14}")
    print(f"  {'Brokerage (Rs.20 × 4)':<30} {avg_brokerage:>11,.1f} {sum(c['brokerage'] for c in all_charges):>19,.0f} {sum(c['brokerage'] for c in all_charges)/total_charges*100:>13.1f}%")
    print(f"  {'STT (0.0625% sell)':<30} {avg_stt:>11,.1f} {sum(c['stt'] for c in all_charges):>19,.0f} {sum(c['stt'] for c in all_charges)/total_charges*100:>13.1f}%")
    print(f"  {'Exchange Txn (0.0495%)':<30} {avg_exchange:>11,.1f} {sum(c['exchange_txn'] for c in all_charges):>19,.0f} {sum(c['exchange_txn'] for c in all_charges)/total_charges*100:>13.1f}%")
    print(f"  {'GST (18%)':<30} {avg_gst:>11,.1f} {sum(c['gst'] for c in all_charges):>19,.0f} {sum(c['gst'] for c in all_charges)/total_charges*100:>13.1f}%")
    print(f"  {'Stamp Duty (0.003% buy)':<30} {avg_stamp:>11,.1f} {sum(c['stamp'] for c in all_charges):>19,.0f} {sum(c['stamp'] for c in all_charges)/total_charges*100:>13.1f}%")
    print(f"  {'SEBI (Rs.10/Cr)':<30} {avg_sebi:>11,.1f} {sum(c['sebi'] for c in all_charges):>19,.0f} {sum(c['sebi'] for c in all_charges)/total_charges*100:>13.1f}%")
    print(f"  {'─'*30} {'─'*12} {'─'*20} {'─'*14}")
    print(f"  {'TOTAL CHARGES':<30} {avg_charge:>11,.1f} {total_charges:>19,.0f} {'100.0%':>14}")
    print()

    # ── P&L impact ───────────────────────────────────────────────────────────
    raw_wins = sum(1 for c in all_charges if c["raw_pnl"] > 0)
    net_wins = sum(1 for c in all_charges if c["net_pnl"] > 0)

    print("=" * 85)
    print("  P&L IMPACT — Raw vs After Charges")
    print("=" * 85)
    print(f"  {'Metric':<35} {'Raw (no charges)':>20} {'After Charges':>20}")
    print(f"  {'─'*35} {'─'*20} {'─'*20}")
    print(f"  {'Total P&L (Rs.)':<35} {total_raw:>19,.0f} {total_net:>19,.0f}")
    print(f"  {'Total Charges (Rs.)':<35} {'—':>20} {total_charges:>19,.0f}")
    print(f"  {'Charges as % of Raw P&L':<35} {'—':>20} {total_charges/total_raw*100:>18.1f}%" if total_raw > 0 else "")
    print(f"  {'Winners':<35} {raw_wins:>20,} {net_wins:>20,}")
    print(f"  {'Win Rate':<35} {raw_wins/len(all_charges)*100:>19.1f}% {net_wins/len(all_charges)*100:>19.1f}%")
    print(f"  {'Avg P&L / Trade':<35} {total_raw/len(all_charges):>19,.0f} {total_net/len(all_charges):>19,.0f}")
    print()

    # ── Year-wise breakdown ──────────────────────────────────────────────────
    by_year: dict[str, list] = defaultdict(list)
    for c in all_charges:
        by_year[c["date"][:4]].append(c)

    print("=" * 85)
    print("  YEAR-WISE: Raw P&L vs Net P&L (after charges)")
    print("=" * 85)
    print(f"  {'Year':<6} {'Trades':>7} {'Raw P&L':>12} {'Charges':>12} {'Net P&L':>12} {'Charge%':>9} {'Net Win%':>9}")
    print(f"  {'─'*6} {'─'*7} {'─'*12} {'─'*12} {'─'*12} {'─'*9} {'─'*9}")

    for year in sorted(by_year):
        yc = by_year[year]
        y_raw = sum(c["raw_pnl"] for c in yc)
        y_charges = sum(c["total"] for c in yc)
        y_net = sum(c["net_pnl"] for c in yc)
        y_wins = sum(1 for c in yc if c["net_pnl"] > 0)
        charge_pct = y_charges / y_raw * 100 if y_raw > 0 else 0
        print(f"  {year:<6} {len(yc):>7} {y_raw:>11,.0f} {y_charges:>11,.0f} {y_net:>11,.0f} {charge_pct:>8.1f}% {y_wins/len(yc)*100:>8.1f}%")

    print()

    # ── Our Script comparison (after charges) ────────────────────────────────
    # Our script takes ~189 fewer trades (Nov skip + IVR filter)
    # Fewer trades = less charges paid
    our_script_est_pnl = 1_065_630  # from compare_2yr.py
    our_script_trades = 1347
    our_avg_charge = avg_charge * 0.95  # slightly lower avg charge (skip low-premium)
    our_total_charges = our_avg_charge * our_script_trades
    our_net_pnl = our_script_est_pnl - our_total_charges

    print("=" * 85)
    print("  FINAL COMPARISON — After All Charges")
    print("=" * 85)
    print(f"  {'Metric':<40} {'AlgoTest Baseline':>20} {'Our Script (Est.)':>20}")
    print(f"  {'─'*40} {'─'*20} {'─'*20}")
    print(f"  {'Raw P&L (Rs.)':<40} {total_raw:>19,.0f} {our_script_est_pnl:>19,.0f}")
    print(f"  {'Total Trades':<40} {len(all_charges):>20,} {our_script_trades:>20,}")
    print(f"  {'Total Charges (Rs.)':<40} {total_charges:>19,.0f} {our_total_charges:>19,.0f}")
    print(f"  {'Charges Saved (fewer trades)':<40} {'—':>20} {total_charges - our_total_charges:>19,.0f}")
    print(f"  {'NET P&L after charges (Rs.)':<40} {total_net:>19,.0f} {our_net_pnl:>19,.0f}")
    print(f"  {'Net Avg P&L / Trade (Rs.)':<40} {total_net/len(all_charges):>19,.0f} {our_net_pnl/our_script_trades:>19,.0f}")
    print(f"  {'Improvement (Rs.)':<40} {'—':>20} {our_net_pnl - total_net:>+19,.0f}")
    print()

    # ── Per-lot charges summary ──────────────────────────────────────────────
    print("-" * 85)
    print("  QUICK REFERENCE — Charges per lot per trade")
    print("-" * 85)
    print(f"  Avg combined premium (CE+PE)  : Rs.{sum(c['turnover'] for c in all_charges) / len(all_charges) / QTY / 2:,.0f} per unit")
    print(f"  Avg charges per trade (1 lot) : Rs.{avg_charge:,.0f}")
    print(f"  Charges as % of avg premium   : {avg_charge / (sum(c['turnover'] for c in all_charges) / len(all_charges) / 2) * 100:.2f}%")
    print(f"  Break-even premium needed     : Rs.{avg_charge / QTY:.1f} per unit combined")
    print()
    print(f"  For 2 lots: charges ≈ Rs.{avg_charge * 1.8:.0f}/trade (not exactly 2× due to flat brokerage)")
    print(f"  For 3 lots: charges ≈ Rs.{avg_charge * 2.6:.0f}/trade")
    print()


if __name__ == "__main__":
    main()
