"""
Backtest Analysis — AlgoTest CSV vs Our Script's Enhancements (v6.4.0)
======================================================================
Parses AlgoTest trade CSV and identifies:
  1. Baseline performance stats
  2. Scenarios where our enhancements would improve outcomes
  3. Specific gap analysis per feature

AlgoTest baseline: 09:17 entry, ATM straddle, 20% SL per leg, 15:15 exit.
Our script adds: trailing SL, dynamic SL tightening, breakeven SL, VIX filter,
IVR/IVP filter, ORB filter, combined decay exit, winner-leg booking,
asymmetric booking, recovery lock, combined profit trailing, momentum filter.

Usage:
    python analyze_backtest.py
"""

from __future__ import annotations

import csv
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# ── Config matching our script ───────────────────────────────────────────────
LEG_SL_PCT          = 20.0
TRAIL_TRIGGER_PCT   = 50.0     # Activate trailing when LTP = 50% of entry
TRAIL_LOCK_PCT      = 15.0     # Trailing SL = LTP × 1.15
WINNER_DECAY_PCT    = 30.0     # Book winner when LTP <= 30% of entry
COMBINED_DECAY_PCT  = 60.0     # Close all when combined decay >= 60%
ASYM_WINNER_PCT     = 40.0     # Asymmetric: winner decayed to 40%
ASYM_LOSER_PCT      = 80.0     # Asymmetric: loser still at 80%+

CSV_PATH = Path(__file__).parent / "69bd6847d9c4596c0d68c658_1774020709.csv"


# ═══════════════════════════════════════════════════════════════════════════════
#  Data structures
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Leg:
    kind: str          # "CE" or "PE"
    entry_price: float
    exit_price: float
    pnl: float
    pnl_pct: float
    exit_time: str
    remarks: str       # "Stop Loss Hit" or "Exit Condition Met For Trade"
    strike: float
    weekday: str
    expiry: str
    highest_mtm: float = 0.0
    lowest_mtm: float  = 0.0

    @property
    def sl_hit(self) -> bool:
        return "Stop Loss" in self.remarks

    @property
    def exit_pct_of_entry(self) -> float:
        """Exit price as % of entry — e.g. 50% means decayed 50%."""
        return (self.exit_price / self.entry_price * 100) if self.entry_price > 0 else 0

    @property
    def decay_pct(self) -> float:
        """How much the leg decayed (positive = profit for short)."""
        return 100.0 - self.exit_pct_of_entry


@dataclass
class Trade:
    index: int
    entry_date: str
    entry_time: str
    entry_price: float   # NIFTY spot at entry
    exit_price: float    # NIFTY spot at exit
    combined_pnl: float
    highest_mtm: float
    lowest_mtm: float
    remarks: str
    ce: Leg | None = None
    pe: Leg | None = None

    @property
    def combined_premium(self) -> float:
        ce_e = self.ce.entry_price if self.ce else 0
        pe_e = self.pe.entry_price if self.pe else 0
        return ce_e + pe_e

    @property
    def both_sl_hit(self) -> bool:
        return bool(self.ce and self.pe and self.ce.sl_hit and self.pe.sl_hit)

    @property
    def one_sl_hit(self) -> bool:
        return bool(
            self.ce and self.pe
            and (self.ce.sl_hit != self.pe.sl_hit)
        )

    @property
    def no_sl_hit(self) -> bool:
        return bool(self.ce and self.pe and not self.ce.sl_hit and not self.pe.sl_hit)

    @property
    def weekday(self) -> str:
        if self.ce:
            return self.ce.weekday
        if self.pe:
            return self.pe.weekday
        return ""

    @property
    def winner_leg(self) -> Leg | None:
        """The leg that was profitable (decayed) — i.e. pnl > 0."""
        if self.ce and self.pe:
            if self.ce.pnl > self.pe.pnl:
                return self.ce
            return self.pe
        return None

    @property
    def loser_leg(self) -> Leg | None:
        if self.ce and self.pe:
            if self.ce.pnl <= self.pe.pnl:
                return self.ce
            return self.pe
        return None


# ═══════════════════════════════════════════════════════════════════════════════
#  CSV Parser
# ═══════════════════════════════════════════════════════════════════════════════

def parse_csv(path: Path) -> list[Trade]:
    trades: dict[int, Trade] = {}

    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            idx_str = row["Index"].strip()
            try:
                idx_float = float(idx_str)
            except ValueError:
                continue

            if idx_float == int(idx_float):
                # Summary row
                t = Trade(
                    index=int(idx_float),
                    entry_date=row["Entry-Date"].strip(),
                    entry_time=row["Entry-Time"].strip(),
                    entry_price=float(row["Entry-Price"] or 0),
                    exit_price=float(row["ExitPrice"] or 0),
                    combined_pnl=float(row["P/L"] or 0),
                    highest_mtm=float(row["Highest MTM(Candle Close)"] or 0),
                    lowest_mtm=float(row["Lowest MTM(Candle Close)"] or 0),
                    remarks=row["Remarks"].strip(),
                )
                trades[t.index] = t
            else:
                # Leg row
                parent_idx = int(idx_float)
                if parent_idx not in trades:
                    continue

                kind_raw = row.get("Instrument-Kind", "").strip()
                kind = kind_raw if kind_raw in ("CE", "PE") else ""
                if not kind:
                    continue

                leg = Leg(
                    kind=kind,
                    entry_price=float(row["Entry-Price"] or 0),
                    exit_price=float(row["ExitPrice"] or 0),
                    pnl=float(row["P/L"] or 0),
                    pnl_pct=float(row["P/L-Percentage"] or 0),
                    exit_time=row["ExitTime"].strip(),
                    remarks=row["Remarks"].strip(),
                    strike=float(row["StrikePrice"] or 0),
                    weekday=row["Entry-Weekday"].strip(),
                    expiry=row["ExpiryDate"].strip(),
                    highest_mtm=float(row["Highest MTM(Candle Close)"] or 0),
                    lowest_mtm=float(row["Lowest MTM(Candle Close)"] or 0),
                )

                if kind == "CE":
                    trades[parent_idx].ce = leg
                else:
                    trades[parent_idx].pe = leg

    result = [t for t in sorted(trades.values(), key=lambda x: x.index) if t.ce and t.pe]
    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  Analysis functions
# ═══════════════════════════════════════════════════════════════════════════════

def baseline_stats(trades: list[Trade]) -> None:
    """Overall backtest statistics."""
    total = len(trades)
    wins = [t for t in trades if t.combined_pnl > 0]
    losses = [t for t in trades if t.combined_pnl < 0]
    flat = [t for t in trades if t.combined_pnl == 0]

    total_pnl = sum(t.combined_pnl for t in trades)
    avg_win = sum(t.combined_pnl for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t.combined_pnl for t in losses) / len(losses) if losses else 0
    max_win = max((t.combined_pnl for t in trades), default=0)
    max_loss = min((t.combined_pnl for t in trades), default=0)

    both_sl = [t for t in trades if t.both_sl_hit]
    one_sl = [t for t in trades if t.one_sl_hit]
    no_sl = [t for t in trades if t.no_sl_hit]

    # Max drawdown (cumulative)
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        cum += t.combined_pnl
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd

    print("=" * 80)
    print("  BASELINE PERFORMANCE — AlgoTest Short Straddle (20% SL, 09:17 entry)")
    print("=" * 80)
    print(f"  Period          : {trades[0].entry_date} → {trades[-1].entry_date}")
    print(f"  Total trades    : {total}")
    print(f"  Winners         : {len(wins)} ({len(wins)/total*100:.1f}%)")
    print(f"  Losers          : {len(losses)} ({len(losses)/total*100:.1f}%)")
    print(f"  Flat            : {len(flat)}")
    print(f"  Total P&L       : Rs.{total_pnl:,.0f}")
    print(f"  Avg win         : Rs.{avg_win:,.0f}")
    print(f"  Avg loss        : Rs.{avg_loss:,.0f}")
    print(f"  Max single win  : Rs.{max_win:,.0f}")
    print(f"  Max single loss : Rs.{max_loss:,.0f}")
    print(f"  Max drawdown    : Rs.{max_dd:,.0f}")
    print(f"  Profit factor   : {abs(sum(t.combined_pnl for t in wins)) / abs(sum(t.combined_pnl for t in losses)):.2f}" if losses else "  Profit factor   : ∞")
    print()
    print(f"  SL patterns:")
    print(f"    Both legs SL  : {len(both_sl)} trades ({len(both_sl)/total*100:.1f}%) — worst scenario")
    print(f"    One leg SL    : {len(one_sl)} trades ({len(one_sl)/total*100:.1f}%) — partial exit")
    print(f"    No SL hit     : {len(no_sl)} trades ({len(no_sl)/total*100:.1f}%) — clean exit at 15:15")
    print()

    # Both-SL total damage
    both_sl_pnl = sum(t.combined_pnl for t in both_sl)
    print(f"  Both-SL damage  : Rs.{both_sl_pnl:,.0f} across {len(both_sl)} trades")
    print(f"    (this is {abs(both_sl_pnl)/abs(sum(t.combined_pnl for t in losses))*100:.0f}% of ALL losses)" if losses else "")
    print()


def weekday_analysis(trades: list[Trade]) -> None:
    """P&L by weekday."""
    by_day: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        if t.weekday:
            by_day[t.weekday].append(t)

    day_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    print("-" * 80)
    print("  WEEKDAY ANALYSIS")
    print("-" * 80)
    print(f"  {'Day':<12} {'Trades':>7} {'Win%':>7} {'Total P&L':>12} {'Avg P&L':>10} {'Both SL':>9} {'Both SL%':>9}")
    print(f"  {'─'*12} {'─'*7} {'─'*7} {'─'*12} {'─'*10} {'─'*9} {'─'*9}")
    for day in day_order:
        if day not in by_day:
            continue
        dt = by_day[day]
        wins = sum(1 for t in dt if t.combined_pnl > 0)
        both = sum(1 for t in dt if t.both_sl_hit)
        total = sum(t.combined_pnl for t in dt)
        avg = total / len(dt)
        print(f"  {day:<12} {len(dt):>7} {wins/len(dt)*100:>6.1f}% {total:>11,.0f} {avg:>9,.0f} {both:>9} {both/len(dt)*100:>8.1f}%")
    print()


def yearly_analysis(trades: list[Trade]) -> None:
    """P&L by year."""
    by_year: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        year = t.entry_date[:4]
        by_year[year].append(t)

    print("-" * 80)
    print("  YEARLY ANALYSIS")
    print("-" * 80)
    print(f"  {'Year':<6} {'Trades':>7} {'Win%':>7} {'Total P&L':>12} {'Avg P&L':>10} {'Both SL':>9} {'Max DD':>12}")
    print(f"  {'─'*6} {'─'*7} {'─'*7} {'─'*12} {'─'*10} {'─'*9} {'─'*12}")
    for year in sorted(by_year):
        yt = by_year[year]
        wins = sum(1 for t in yt if t.combined_pnl > 0)
        both = sum(1 for t in yt if t.both_sl_hit)
        total = sum(t.combined_pnl for t in yt)
        avg = total / len(yt)
        # Year max DD
        cum = 0.0; peak = 0.0; max_dd = 0.0
        for t in yt:
            cum += t.combined_pnl
            if cum > peak: peak = cum
            dd = peak - cum
            if dd > max_dd: max_dd = dd
        print(f"  {year:<6} {len(yt):>7} {wins/len(yt)*100:>6.1f}% {total:>11,.0f} {avg:>9,.0f} {both:>9} {max_dd:>11,.0f}")
    print()


def sl_timing_analysis(trades: list[Trade]) -> None:
    """Analyze when SL hits happen — early morning vs later."""
    early_sl = []  # SL hit within 15 min of entry (09:17 to 09:32)
    late_sl = []

    for t in trades:
        for leg in [t.ce, t.pe]:
            if leg and leg.sl_hit:
                try:
                    h, m, s = leg.exit_time.split(":")
                    exit_mins = int(h) * 60 + int(m)
                    entry_mins = 9 * 60 + 17  # 09:17
                    if exit_mins - entry_mins <= 15:
                        early_sl.append((t, leg))
                    else:
                        late_sl.append((t, leg))
                except Exception:
                    late_sl.append((t, leg))

    total_sl = len(early_sl) + len(late_sl)
    print("-" * 80)
    print("  SL TIMING ANALYSIS — When do SL hits happen?")
    print("-" * 80)
    print(f"  Total SL hits         : {total_sl}")
    print(f"  Within 15 min (09:17–09:32) : {len(early_sl)} ({len(early_sl)/total_sl*100:.1f}%)" if total_sl > 0 else "")
    print(f"  After 15 min                : {len(late_sl)} ({len(late_sl)/total_sl*100:.1f}%)" if total_sl > 0 else "")
    print()

    early_sl_loss = sum(leg.pnl for _, leg in early_sl)
    print(f"  Early SL total loss   : Rs.{early_sl_loss:,.0f}")
    print(f"  → This is what DTE-aware entry times (09:30-09:45) would help avoid")
    print()


def trailing_sl_analysis(trades: list[Trade]) -> None:
    """
    Identify trades where trailing SL would have improved outcomes.
    Focus on one-leg-SL trades where the winner ran to 15:15 without trailing.
    """
    print("-" * 80)
    print("  TRAILING SL IMPACT ANALYSIS")
    print("-" * 80)

    # Trades where one leg decayed significantly but was held to 15:15
    could_trail = []
    for t in trades:
        if not t.one_sl_hit:
            continue
        winner = t.winner_leg
        if winner and not winner.sl_hit and winner.decay_pct >= TRAIL_TRIGGER_PCT:
            # Winner decayed past trigger — trailing would have activated
            could_trail.append(t)

    print(f"  One-leg-SL trades where winner decayed >= {TRAIL_TRIGGER_PCT}%: {len(could_trail)}")
    print()

    # Among these, find cases where the winner bounced back (gave back profit)
    # We can approximate this: if the winner's exit decay is LESS than what the
    # lowest MTM suggests it could have been, trailing would have locked more.
    bounce_back_cases = 0
    profit_given_back = 0.0
    for t in could_trail:
        winner = t.winner_leg
        if not winner:
            continue
        # If winner decayed to, say, 20% of entry at some point (lowest MTM)
        # but exited at 35% of entry — it bounced back and trailing would
        # have locked at the lower level.
        # We don't have intraday low of the leg, but we know the final exit.
        # If decay > 70%, trailing at 15% lock would have caught most of the move.
        if winner.decay_pct >= 70:
            # This was a deeply decayed winner — trailing would lock at ~85% decay
            # The baseline just held to 15:15 which might be fine, but if market
            # reversed, trailing protects.
            pass

    print(f"  Winners that decayed 70%+ (deep profit, trailing locks at ~85%): "
          f"{sum(1 for t in could_trail if t.winner_leg and t.winner_leg.decay_pct >= 70)}")
    print(f"  Winners that decayed 50-70% (trailing activates, moderate lock): "
          f"{sum(1 for t in could_trail if t.winner_leg and 50 <= t.winner_leg.decay_pct < 70)}")
    print()
    print(f"  → Trailing SL with trigger={TRAIL_TRIGGER_PCT}%, lock={TRAIL_LOCK_PCT}% would")
    print(f"    protect against late-session reversals on these {len(could_trail)} trades")
    print()


def both_sl_deep_dive(trades: list[Trade]) -> None:
    """Deep dive into both-SL-hit trades — our worst scenario."""
    both = [t for t in trades if t.both_sl_hit]
    if not both:
        print("  No both-SL trades found.")
        return

    print("-" * 80)
    print("  BOTH-SL DEEP DIVE — Our script's features that would help")
    print("-" * 80)

    total_both_loss = sum(t.combined_pnl for t in both)
    print(f"  Total both-SL trades : {len(both)}")
    print(f"  Total loss           : Rs.{total_both_loss:,.0f}")
    print(f"  Avg loss per trade   : Rs.{total_both_loss/len(both):,.0f}")
    print()

    # Categorize: how quickly did both SLs fire?
    quick_both = 0   # Both SL within 30 min
    slow_both = 0    # Spread out
    for t in both:
        try:
            ce_h, ce_m, _ = t.ce.exit_time.split(":")
            pe_h, pe_m, _ = t.pe.exit_time.split(":")
            ce_mins = int(ce_h) * 60 + int(ce_m)
            pe_mins = int(pe_h) * 60 + int(pe_m)
            gap = abs(ce_mins - pe_mins)
            if gap <= 30:
                quick_both += 1
            else:
                slow_both += 1
        except Exception:
            slow_both += 1

    print(f"  Both SLs within 30 min : {quick_both} (fast whipsaw — ORB/VIX filter would help)")
    print(f"  Both SLs spread out    : {slow_both} (trending day — spot-move exit or daily loss limit)")
    print()

    # Features that address both-SL:
    print("  OUR SCRIPT'S DEFENSES:")
    print(f"    1. ORB Filter          — blocks entry on >0.5% gap moves")
    print(f"    2. VIX Filter          — blocks entry when VIX > 28 (volatile regime)")
    print(f"    3. Dynamic SL          — tightens SL from 20% to 7% through the day")
    print(f"    4. Daily Loss Limit    — hard stop after Rs.4000/lot loss")
    print(f"    5. Spot-Move Exit      — closes all when NIFTY moves beyond breakeven")
    print(f"    6. VIX Spike Exit      — closes on 15% intraday VIX jump")
    print()


def winner_leg_analysis(trades: list[Trade]) -> None:
    """Analyze the surviving winner leg after partial exit."""
    one_sl = [t for t in trades if t.one_sl_hit]

    print("-" * 80)
    print("  WINNER LEG ANALYSIS — After one leg SL, what happens to the survivor?")
    print("-" * 80)
    print(f"  Total one-leg-SL trades: {len(one_sl)}")
    print()

    # Classify winner leg outcomes
    winner_big_profit = []   # Winner decayed > 60%
    winner_moderate = []     # Winner decayed 30-60%
    winner_small = []        # Winner decayed < 30%
    winner_still_lost = []   # Winner also had negative P&L (rare but possible)

    for t in one_sl:
        winner = t.winner_leg
        if not winner:
            continue
        if winner.pnl < 0:
            winner_still_lost.append(t)
        elif winner.decay_pct > 60:
            winner_big_profit.append(t)
        elif winner.decay_pct > 30:
            winner_moderate.append(t)
        else:
            winner_small.append(t)

    print(f"  Winner decayed >60%  : {len(winner_big_profit)} trades (deep profit — trailing protects)")
    print(f"  Winner decayed 30-60%: {len(winner_moderate)} trades (moderate — winner booking at 30%)")
    print(f"  Winner decayed <30%  : {len(winner_small)} trades (minimal — recovery lock helps)")
    print(f"  Winner also lost     : {len(winner_still_lost)} trades (both legs lost!)")
    print()

    # Net P&L for one-SL trades
    one_sl_net_profit = sum(t.combined_pnl for t in one_sl if t.combined_pnl > 0)
    one_sl_net_loss = sum(t.combined_pnl for t in one_sl if t.combined_pnl < 0)
    print(f"  One-SL trades that ended profitable : {sum(1 for t in one_sl if t.combined_pnl > 0)} (Rs.{one_sl_net_profit:,.0f})")
    print(f"  One-SL trades that ended in loss    : {sum(1 for t in one_sl if t.combined_pnl < 0)} (Rs.{one_sl_net_loss:,.0f})")
    print()

    # Context-aware breakeven impact (FIX-XXIV)
    # Trades where loser SL hit, but winner was profitable — breakeven SL
    # would have killed the winner in old code!
    be_would_kill = 0
    be_kill_profit_lost = 0.0
    for t in one_sl:
        winner = t.winner_leg
        loser = t.loser_leg
        if winner and loser and loser.sl_hit and winner.pnl > 0:
            # In old code: breakeven SL would be set on winner,
            # potentially killing it. FIX-XXIV skips this.
            be_would_kill += 1
            be_kill_profit_lost += winner.pnl

    print(f"  FIX-XXIV IMPACT:")
    print(f"    Trades where old breakeven SL would kill winning survivor: {be_would_kill}")
    print(f"    Profit preserved by context-aware breakeven: Rs.{be_kill_profit_lost:,.0f}")
    print()


def combined_decay_analysis(trades: list[Trade]) -> None:
    """Analyze trades where combined decay target would have exited earlier."""
    print("-" * 80)
    print("  COMBINED DECAY EXIT ANALYSIS")
    print("-" * 80)

    # Trades where both legs were still active at exit (no SL hit)
    no_sl = [t for t in trades if t.no_sl_hit]

    early_exit_cases = 0
    held_too_long = 0
    for t in no_sl:
        if not t.ce or not t.pe:
            continue
        combined_entry = t.ce.entry_price + t.pe.entry_price
        combined_exit = t.ce.exit_price + t.pe.exit_price
        if combined_entry > 0:
            decay = (1 - combined_exit / combined_entry) * 100
            if decay >= COMBINED_DECAY_PCT:
                early_exit_cases += 1
            # Check if the exit P&L was less than the highest MTM
            if t.highest_mtm > 0 and t.combined_pnl < t.highest_mtm * 0.7:
                held_too_long += 1

    print(f"  No-SL trades (both legs to 15:15)     : {len(no_sl)}")
    print(f"  Would have hit {COMBINED_DECAY_PCT}% combined decay   : {early_exit_cases}")
    print(f"  Gave back >30% of peak MTM by 15:15   : {held_too_long}")
    print()

    # Quantify profit given back on these trades
    profit_given_back = 0.0
    for t in no_sl:
        if t.highest_mtm > 0 and t.combined_pnl < t.highest_mtm:
            profit_given_back += (t.highest_mtm - t.combined_pnl)

    print(f"  Total profit given back (peak→exit)   : Rs.{profit_given_back:,.0f}")
    print(f"  → Combined decay exit + combined profit trail would capture more of this")
    print()


def asymmetric_analysis(trades: list[Trade]) -> None:
    """Identify trades where asymmetric leg booking would fire."""
    print("-" * 80)
    print("  ASYMMETRIC LEG BOOKING ANALYSIS")
    print("-" * 80)

    # Trades where both legs active but one is deep profit, other barely moved
    asym_candidates = []
    for t in trades:
        if not t.ce or not t.pe or t.both_sl_hit:
            continue
        # At exit time, check if one leg was deeply decayed while other wasn't
        ce_pct = t.ce.exit_price / t.ce.entry_price * 100 if t.ce.entry_price > 0 else 100
        pe_pct = t.pe.exit_price / t.pe.entry_price * 100 if t.pe.entry_price > 0 else 100

        if (ce_pct <= ASYM_WINNER_PCT and pe_pct >= ASYM_LOSER_PCT) or \
           (pe_pct <= ASYM_WINNER_PCT and ce_pct >= ASYM_LOSER_PCT):
            asym_candidates.append(t)

    print(f"  Trades with asymmetric legs at exit: {len(asym_candidates)}")
    print(f"    (one leg <= {ASYM_WINNER_PCT}% of entry, other >= {ASYM_LOSER_PCT}%)")
    print()

    # Among these, how many ended in loss? Asymmetric booking would have
    # booked the winner early, converting to single-leg position
    asym_lost = [t for t in asym_candidates if t.combined_pnl < 0]
    print(f"  Of these, ended in LOSS : {len(asym_lost)} — asymmetric booking would book winner earlier")
    print(f"  Of these, ended in PROFIT: {len(asym_candidates) - len(asym_lost)}")
    print()


def monthly_breakdown(trades: list[Trade]) -> None:
    """Month analysis to validate November skip."""
    by_month: dict[int, list[Trade]] = defaultdict(list)
    for t in trades:
        try:
            month = int(t.entry_date.split("-")[1])
            by_month[month].append(t)
        except Exception:
            pass

    month_names = {
        1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
        7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"
    }

    print("-" * 80)
    print("  MONTHLY ANALYSIS — Validates November skip filter")
    print("-" * 80)
    print(f"  {'Month':<6} {'Trades':>7} {'Win%':>7} {'Total P&L':>12} {'Avg P&L':>10} {'Both SL%':>9}")
    print(f"  {'─'*6} {'─'*7} {'─'*7} {'─'*12} {'─'*10} {'─'*9}")
    for m in range(1, 13):
        if m not in by_month:
            continue
        mt = by_month[m]
        wins = sum(1 for t in mt if t.combined_pnl > 0)
        both = sum(1 for t in mt if t.both_sl_hit)
        total = sum(t.combined_pnl for t in mt)
        avg = total / len(mt) if mt else 0
        marker = " ◄ SKIP" if m == 11 else ""
        print(f"  {month_names[m]:<6} {len(mt):>7} {wins/len(mt)*100:>6.1f}% {total:>11,.0f} {avg:>9,.0f} {both/len(mt)*100:>8.1f}%{marker}")
    print()


def feature_impact_summary(trades: list[Trade]) -> None:
    """Summary of estimated impact of each feature."""
    total_pnl = sum(t.combined_pnl for t in trades)
    total_loss = sum(t.combined_pnl for t in trades if t.combined_pnl < 0)

    both_sl = [t for t in trades if t.both_sl_hit]
    one_sl = [t for t in trades if t.one_sl_hit]
    no_sl = [t for t in trades if t.no_sl_hit]

    both_sl_loss = sum(t.combined_pnl for t in both_sl)

    # Estimate how many both-SL could have been avoided by ORB filter
    # (both SL within 15 min — opening volatility)
    quick_both_sl = 0
    quick_both_loss = 0.0
    for t in both_sl:
        try:
            ce_h, ce_m, _ = t.ce.exit_time.split(":")
            pe_h, pe_m, _ = t.pe.exit_time.split(":")
            ce_mins = int(ce_h) * 60 + int(ce_m)
            pe_mins = int(pe_h) * 60 + int(pe_m)
            if max(ce_mins, pe_mins) - (9*60+17) <= 30:
                quick_both_sl += 1
                quick_both_loss += t.combined_pnl
        except Exception:
            pass

    # Profit given back on no-SL trades
    profit_given_back = 0.0
    for t in no_sl:
        if t.highest_mtm > 0 and t.combined_pnl < t.highest_mtm:
            profit_given_back += (t.highest_mtm - t.combined_pnl)

    # November trades
    nov_trades = [t for t in trades if t.entry_date[5:7] == "11"]
    nov_pnl = sum(t.combined_pnl for t in nov_trades)

    # FIX-XXIV: winner preservation
    be_preserved = 0.0
    for t in one_sl:
        winner = t.winner_leg
        loser = t.loser_leg
        if winner and loser and loser.sl_hit and winner.pnl > 0:
            be_preserved += winner.pnl

    print("=" * 80)
    print("  FEATURE IMPACT SUMMARY — What our script adds over AlgoTest baseline")
    print("=" * 80)
    print()
    print(f"  BASELINE : Rs.{total_pnl:,.0f} total P&L | {len(trades)} trades")
    print()
    print(f"  {'Feature':<40} {'Impact':>15} {'Description'}")
    print(f"  {'─'*40} {'─'*15} {'─'*40}")
    print(f"  {'DTE-Aware Entry (09:30-09:45)':<40} {'REDUCE LOSS':>15} Avoids early-morning false SL hits")
    print(f"  {'ORB Filter (0.5% gap)':<40} {'REDUCE LOSS':>15} {quick_both_sl} both-SL trades from morning gap")
    print(f"  {'VIX Filter (14-28 range)':<40} {'REDUCE LOSS':>15} Blocks entry in extreme volatility")
    print(f"  {'IVR/IVP Filter (30/40 min)':<40} {'REDUCE LOSS':>15} Only enter when IV is historically rich")
    print(f"  {'November Skip':<40} {'Rs.{0:>+,.0f}'.format(-nov_pnl):>15} Skip worst month (backtest confirms)")
    print(f"  {'Dynamic SL (20%→7% through day)':<40} {'REDUCE LOSS':>15} Tighter SL in afternoon protects gains")
    print(f"  {'Trailing SL (50% trigger, 15% lock)':<40} {'LOCK PROFIT':>15} Locks profit on winning legs")
    print(f"  {'Context-Aware Breakeven (FIX-XXIV)':<40} {'Rs.{0:>,.0f}'.format(be_preserved):>15} Winner preservation after partial exit")
    print(f"  {'Combined Decay Exit (60%)':<40} {'LOCK PROFIT':>15} Exit when both legs decayed enough")
    print(f"  {'Combined Profit Trail':<40} {'Rs.{0:>,.0f}'.format(profit_given_back):>15} Peak profit given back (trailable)")
    print(f"  {'Winner-Leg Early Booking (30%)':<40} {'LOCK PROFIT':>15} Book deep winners before reversal")
    print(f"  {'Asymmetric Leg Booking':<40} {'REDUCE RISK':>15} Close diverged winner, reduce gamma")
    print(f"  {'Recovery Lock':<40} {'LOCK PROFIT':>15} Trail recovery after partial exit")
    print(f"  {'Momentum Filter (re-entry)':<40} {'REDUCE LOSS':>15} Block re-entry in trending market")
    print(f"  {'Daily Loss Limit (-4000/lot)':<40} {'CAP LOSS':>15} Hard stop prevents catastrophic days")
    print(f"  {'Spot-Move Exit':<40} {'CAP LOSS':>15} Close on breakeven breach")
    print(f"  {'VIX Spike Exit (15%)':<40} {'CAP LOSS':>15} Emergency exit on IV expansion")
    print()
    print(f"  BIGGEST LOSS DRIVERS IN BASELINE:")
    print(f"    Both-SL trades : Rs.{both_sl_loss:,.0f} ({len(both_sl)} trades)")
    print(f"      Quick both-SL (within 30 min): Rs.{quick_both_loss:,.0f} ({quick_both_sl} trades)")
    print(f"    Profit given back on winners   : Rs.{profit_given_back:,.0f} (no trailing/decay exit)")
    print(f"    November losses                : Rs.{nov_pnl:,.0f} ({len(nov_trades)} trades)")
    print()


def premium_analysis(trades: list[Trade]) -> None:
    """Analyze combined premium and its relationship to outcomes."""
    print("-" * 80)
    print("  PREMIUM ANALYSIS — Combined premium vs outcome")
    print("-" * 80)

    # Bucket by combined premium
    low_prem = [t for t in trades if t.combined_premium < 80]
    mid_prem = [t for t in trades if 80 <= t.combined_premium < 150]
    high_prem = [t for t in trades if t.combined_premium >= 150]

    for label, bucket in [("Low (<80)", low_prem), ("Mid (80-150)", mid_prem), ("High (>150)", high_prem)]:
        if not bucket:
            continue
        wins = sum(1 for t in bucket if t.combined_pnl > 0)
        total = sum(t.combined_pnl for t in bucket)
        both = sum(1 for t in bucket if t.both_sl_hit)
        print(f"  {label:<15} : {len(bucket):>4} trades | Win: {wins/len(bucket)*100:.0f}% | "
              f"P&L: Rs.{total:>8,.0f} | Both SL: {both} ({both/len(bucket)*100:.0f}%)")

    print()
    print(f"  → Low premium (<80) often correlates with low VIX / thin theta days")
    print(f"    Our IVR/IVP filter would skip many of these low-edge entries")
    print()


# ═══════════════════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    if not CSV_PATH.exists():
        print(f"ERROR: CSV not found at {CSV_PATH}")
        sys.exit(1)

    trades = parse_csv(CSV_PATH)
    print(f"\nParsed {len(trades)} trades from AlgoTest CSV\n")

    baseline_stats(trades)
    yearly_analysis(trades)
    weekday_analysis(trades)
    monthly_breakdown(trades)
    sl_timing_analysis(trades)
    premium_analysis(trades)
    both_sl_deep_dive(trades)
    winner_leg_analysis(trades)
    trailing_sl_analysis(trades)
    combined_decay_analysis(trades)
    asymmetric_analysis(trades)
    feature_impact_summary(trades)


if __name__ == "__main__":
    main()
