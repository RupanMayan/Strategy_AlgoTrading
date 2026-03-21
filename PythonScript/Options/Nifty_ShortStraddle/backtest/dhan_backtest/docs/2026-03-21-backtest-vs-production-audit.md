# Backtest vs Production Audit — Nifty Short Straddle

**Date:** 2026-03-21
**Backtest Engine:** `nifty_straddle_bt.py` (Iteration 2)
**Production Files:** `src/risk.py`, `src/monitor.py`, `src/_shared.py`, `config.toml`

---

## Config Parameters: ALL MATCH

| Parameter | Backtest | Production | Status |
|---|---|---|---|
| `leg_sl_percent` | 20.0% | 20.0% | MATCH |
| `dte_sl_override` (2/3/4) | 25/28/30% | 25/28/30% | MATCH |
| `daily_profit_target_per_lot` | 5000 | 5000 | MATCH |
| `daily_loss_limit_per_lot` | -4000 | -4000 | MATCH |
| `trail_trigger_pct` | 50.0% | 50.0% | MATCH |
| `trail_lock_pct` | 15.0% | 15.0% | MATCH |
| `breakeven_grace_period_min` | 5 | 5 | MATCH |
| `breakeven_buffer_pct` | 10.0% | 10.0% | MATCH |
| `combined_decay_target_pct` | 60.0% | 60.0% | MATCH |
| `combined_decay DTE overrides` | 70/65/60/50/50 | 70/65/60/50/50 | MATCH |
| `winner_decay_threshold_pct` | 30.0% | 30.0% | MATCH |
| `entry_time` (default) | 09:30 | 09:30 | MATCH |
| `exit_time` | 15:15 | 15:15 | MATCH |
| `dte_entry_time_map` (0-4) | 09:30/09:30/09:35/09:40/09:45 | Same | MATCH |
| Dynamic SL schedule | 12:00→15%, 13:30→10%, 14:30→7% | Same | MATCH |

---

## Logic That Matches

| Feature | Status | Notes |
|---|---|---|
| SL priority chain | MATCH | Trailing > Breakeven > Fixed/Dynamic |
| Trailing SL activation | MATCH | LTP ≤ trigger% of entry |
| Trailing SL ratchet | MATCH | Only tighten, never loosen |
| Trailing SL safety cap | MATCH | Never worse than fixed SL |
| Combined decay exit | MATCH | DTE-aware thresholds, both legs active |
| Winner-leg booking | MATCH | Single survivor at ≤30% of entry |
| Daily P&L limits | MATCH | Combined closed + open MTM every candle |
| Dynamic SL tightening | MATCH | Time-of-day schedule, min(dynamic, base) |

---

## CRITICAL Discrepancy: Breakeven SL Buffer Direction

### The Difference

| | Backtest (current) | Production |
|---|---|---|
| Formula | `raw_be × (1 - buffer/100)` | `raw_be × (1 + buffer/100)` |
| Effect for short options | Tighter — exits sooner, locks small profit | Looser — more room before firing |
| Impact | 96% win rate, +₹186K | ~50% win rate, ~-₹17K |

### Worked Example

```
CE entry = 200, PE entry = 200, buffer = 10%
CE hits fixed SL at 240 → loss = (200-240) × 65 = -₹2,600
raw_be = PE_entry + closed_pnl/qty = 200 + (-2600/65) = 160

BACKTEST (1 - buffer):
  be_price = 160 × 0.90 = 144
  PE exits at 144 → profit = (200-144) × 65 = +₹3,640
  Combined = -₹2,600 + ₹3,640 = +₹1,040 (small win)

PRODUCTION (1 + buffer):
  be_price = 160 × 1.10 = 176
  PE exits at 176 → profit = (200-176) × 65 = +₹1,560
  Combined = -₹2,600 + ₹1,560 = -₹1,040 (small loss)
```

### Analysis

For a **SHORT option** SL (fires when price goes UP above level):
- `(1 - buffer)` = lower SL level → fires sooner → locks MORE profit on survivor → net positive
- `(1 + buffer)` = higher SL level → fires later → survivor profit erodes past breakeven → net negative

**The backtest direction `(1 - buffer)` appears mathematically correct for short options.** The production `(1 + buffer)` may be a bug — it fires AFTER the position has already gone past breakeven, defeating the purpose of breakeven protection. This warrants review of the production code.

---

## HIGH: Breakeven SL Context Awareness (FIX-XXIV)

**Production has, backtest lacks:**
When the survivor leg is already WINNING (LTP < entry for shorts), production **skips** arming breakeven SL. Rationale: if the survivor is profitable, breakeven SL would cap upside unnecessarily.

**Impact:** Backtest arms breakeven SL on ALL partial exits with loss, even when the survivor is deeply profitable. This over-protects but also caps some winning trades.

---

## HIGH: 7 Missing Production Features

### 1. Asymmetric Leg Booking (FIX-XXVII)
- **Production config:** `winner_decay_threshold = 40%`, `loser_intact_threshold = 80%`
- **Logic:** Book deeply decayed winner when other leg barely moved
- **When it fires:** Both legs active, winner at ≤40% of entry, loser at ≥80% of entry
- **Impact on backtest:** Would capture some early exits when one leg decays fast while the other stays flat

### 2. Combined Profit Trailing (FIX-XXVIII)
- **Production config:** `activate_at_decay = 30%`, `trail_points = 40%`
- **Logic:** Once combined premium decays 30%, track the peak decay. If decay retraces by 40% from peak, exit.
- **When it fires:** Both legs active, combined decay ≥30%, then retracement detected
- **Impact on backtest:** Would capture some exits when combined decay peaks and starts reversing

### 3. Post-Partial Recovery Lock (FIX-XXV)
- **Production config:** `min_recovery_per_lot = ₹500`, `trail_pct = 50%`
- **Logic:** After partial exit at loss, trail the recovery P&L peak. If recovery retraces 50% from peak, lock in.
- **When it fires:** One leg closed at loss, survivor recovering, recovery exceeds ₹500/lot
- **Impact on backtest:** Would capture some exits when survivor recovers significantly then starts giving back

### 4. Net P&L Guard (FIX-XX)
- **Production config:** `max_deferral = 15 minutes`
- **Logic:** Defer per-leg SL when partial position is net profitable
- **When it fires:** One leg closed, survivor about to hit SL, but combined P&L is positive
- **Impact on backtest:** Would allow some trades to avoid unnecessary SL hits when already profitable

### 5. VIX Spike Exit
- **Production config:** `threshold = 15%`, `absolute_floor = 18.0`
- **Logic:** Close all when VIX spikes 15%+ AND VIX ≥ 18
- **Backtest limitation:** Dhan ATM data doesn't include VIX — would need separate VIX data source
- **Impact on backtest:** Cannot be implemented without VIX data

### 6. Spot-Move / Breakeven Breach Exit
- **Production config:** `spot_multiplier = 1.0`
- **Logic:** Exit when |current_spot - entry_spot| ≥ combined_premium × multiplier
- **When it fires:** Large NIFTY moves that breach theoretical breakeven range
- **Impact on backtest:** Would trigger on big trend days, adding downside protection

### 7. Re-Entry After Early Close
- **Production config:** `cooldown = 30 min`, `max_loss_threshold = ₹2000/lot`, `max_re_entries = 1`
- **Logic:** After early exit, re-enter if loss was small and cooldown elapsed
- **Impact on backtest:** Could add trades on days where initial exit was due to noise

---

## Summary Impact Assessment

| Discrepancy | Direction | Estimated P&L Impact |
|---|---|---|
| Breakeven buffer direction | Backtest more favorable | Very High — explains 96% vs ~50% win rate |
| Missing FIX-XXIV (context awareness) | Backtest more favorable | Medium — over-protects some trades |
| Missing asymmetric leg booking | Unknown | Low-Medium |
| Missing combined profit trailing | Production more protective | Low-Medium |
| Missing post-partial recovery lock | Production more favorable | Medium |
| Missing net P&L guard | Production more favorable | Low |
| Missing VIX spike exit | Production more protective | Depends on VIX conditions |
| Missing spot-move exit | Production more protective | Medium on trend days |
| Missing re-entry | Production more favorable | Low (max 1/day) |

---

## Recommendations

### Immediate (Iteration 3)
1. **Fix breakeven buffer to match production** — Change to `(1 + buffer/100)` for accurate comparison
2. **Add FIX-XXIV context awareness** — Skip breakeven SL when survivor is winning
3. **Add spot-move exit** — Data available (spot column in Parquet)
4. **Add asymmetric leg booking** — Straightforward to implement
5. **Add combined profit trailing** — Straightforward to implement
6. **Add post-partial recovery lock** — Straightforward to implement
7. **Add net P&L guard** — Straightforward to implement

### Deferred
8. **VIX spike exit** — Requires separate India VIX data source (not in Dhan ATM data)
9. **Re-entry logic** — Adds complexity, low expected impact

### Production Review
10. **Investigate production breakeven buffer direction** — The `(1 + buffer)` direction may be suboptimal for short options. Consider testing `(1 - buffer)` in production paper trading.
