# Nifty Short Straddle — Optimization Results

**Date:** 2026-03-21
**Data Period:** 2025-03-21 to 2026-03-21 (224 trading days, 92,737 candles)
**Data Source:** Dhan expired options API (1-min OHLC + IV + OI + spot)

---

## Executive Summary

5-phase systematic grid search across 600+ parameter combinations found an optimized configuration that transforms the strategy from **-₹23K loss** to **+₹189K profit** (with re-entry) or **+₹168K** (with 1% realistic slippage).

### Final Comparison

| Configuration | P&L | Win Rate | PF | Max DD | Sharpe | Trades |
|---|---|---|---|---|---|---|
| Production match (Iter 3) | -₹23,107 | 59.4% | 0.84 | -₹31,648 | — | 224 |
| Iter 2 (BE=down, all features) | -₹16,812 | 60.7% | 0.88 | -₹25,353 | — | 224 |
| **Optimized (no re-entry)** | **₹152,797** | **65.2%** | **2.06** | **-₹13,046** | **4.50** | **224** |
| **Optimized + re-entry** | **₹188,871** | **64.3%** | **2.15** | **-₹12,812** | **4.85** | **291** |
| Best + 1% slippage | ₹167,794 | 62.8% | 1.94 | -₹13,601 | — | 290 |

---

## Optimization Phases

### Phase 1: High-Impact Parameters (128 combos)

Tested: `be_buffer_direction`, `spot_multiplier`, `recovery_trail_pct`, `trade_dte`

**Key findings:**
- `spot_move_exit` DISABLED is optimal (was cutting winners short)
- `recovery_lock` DISABLED is optimal (same issue)
- DTE [0,3,4] was best for DTE-filtered, but ALL DTEs work with optimized params
- Breakeven buffer direction barely matters when recovery/spot-move are off

**Best Phase 1:** +₹30,962, WR=58.3%, PF=1.38

### Phase 2: SL Tuning (96 combos)

Tested: `base_sl_pct`, `dynamic_sl`, `trail_trigger_pct`, `trail_lock_pct`

**Key findings:**
- **30% base SL** is optimal (wider = lets trades breathe for theta capture)
- **Dynamic SL disabled** — time-of-day tightening hurts more than helps
- **Trailing SL disabled** — rarely triggers and when it does, cuts profits

**Best Phase 2:** +₹104,021, WR=66.9%, PF=2.54

### Phase 3: Daily Limits & Decay (400 combos)

Tested: `profit_target`, `loss_limit`, `decay_dte0`, `be_buffer_pct`

**Key findings:**
- **Profit target ₹10,000/lot** (higher = capture more; rarely hit anyway)
- **Loss limit -₹6,000/lot** (generous enough to avoid premature stops)
- **Decay DTE0 override 60%** (expiry day exits earlier)
- **BE buffer 5%** (tighter = locks breakeven more precisely)

**Best Phase 3:** +₹120,263, WR=67.7%, PF=2.91

### Phase 4: Slippage Stress (6 levels)

| Slippage | P&L | WR | PF | Max DD |
|---|---|---|---|---|
| 0.0% | ₹120,263 | 67.7% | 2.91 | -₹9,542 |
| 0.5% | ₹117,666 | 66.9% | 2.82 | -₹9,858 |
| 1.0% | ₹115,070 | 66.9% | 2.74 | -₹10,175 |
| 1.5% | ₹112,475 | 66.9% | 2.65 | -₹10,491 |
| 2.0% | ₹109,880 | 66.1% | 2.58 | -₹10,807 |
| 3.0% | ₹104,696 | 66.1% | 2.43 | -₹11,440 |

**Strategy is robust** — profitable even at 3% slippage.

### Phase 5: Re-entry Optimization (32 combos)

Tested: `max_per_day`, `cooldown_min`, `max_loss_per_lot`

**Key findings:**
- **2 re-entries/day** optimal (captures recovery after SL hits)
- **45-minute cooldown** optimal (lets market settle after adverse move)
- **₹2,000/lot max loss filter** optimal (blocks re-entry after big losses)
- 30-min cooldown too aggressive (re-enters into still-trending market)

**Best Phase 5:** +₹188,871, WR=64.3%, PF=2.15

---

## Per-DTE Analysis (Best Config)

| DTE | P&L | Status |
|---|---|---|
| 0 (Expiry) | +₹81,179 | Best performer |
| 1 | +₹26,099 | Profitable (was loss-making before) |
| 2 | +₹12,521 | Profitable (was loss-making before) |
| 3 | +₹38,716 | Strong |
| 4 | +₹30,355 | Strong |

**All DTEs now profitable** — no need to skip any.

---

## IV Analysis

| IV Bucket | Trades | Total P&L | Win Rate | Avg P&L/Trade |
|---|---|---|---|---|
| Low (12-20) | 73 | +₹16,305 | 57.5% | +₹223 |
| Med-Low (20-25) | 73 | +₹32,274 | 60.3% | +₹442 |
| Med-High (25-32) | 72 | +₹47,084 | 69.4% | +₹654 |
| High (32-81) | 73 | +₹93,207 | 69.9% | +₹1,277 |

Higher IV = better for option sellers (more premium collected). All IV buckets profitable, so no IV filter needed.

---

## Exit Reasons (Best Config)

| Exit | Count | % |
|---|---|---|
| hard_exit (15:15) | 162 | 55.7% |
| winner_booking | 67 | 23.0% |
| both_sl_hit | 53 | 18.2% |
| loss_limit | 4 | 1.4% |
| combined_decay | 2 | 0.7% |
| profit_target | 2 | 0.7% |
| asymmetric_book | 1 | 0.3% |

Most trades exit at hard exit (55.7%) = holding to capture max theta. Winner booking (23%) captures deep-decay winners early.

---

## Optimal Configuration

```toml
[risk]
leg_sl_percent              = 30.0    # Wider SL (was 20.0)
daily_profit_target_per_lot = 10000   # Higher target (was 5000)
daily_loss_limit_per_lot    = -6000   # Wider limit (was -4000)

[risk.dynamic_sl]
enabled = false                        # DISABLED (was true)

[risk.trailing_sl]
enabled = false                        # DISABLED (was true)

[risk.breakeven_sl]
enabled          = true
buffer_pct       = 5.0                 # Tighter (was 10.0)

[risk.combined_decay_exit]
enabled          = true
[risk.combined_decay_exit.dte_override]
"0" = 60.0                             # Unchanged

[risk.winner_leg_booking]
enabled             = true             # Unchanged

[risk.recovery_lock]
enabled = false                        # DISABLED (was true)

[risk.spot_move_exit]
enabled = false                        # DISABLED (was true)

[risk.reentry]
enabled              = true            # NEW
max_per_day          = 2
cooldown_min         = 45
max_loss_per_lot     = 2000

[backtest]
trade_dte = [0, 1, 2, 3, 4]           # ALL DTEs (was skipping 1,2)
```

### Key Changes from Production

| Parameter | Production | Optimized | Impact |
|---|---|---|---|
| leg_sl_percent | 20% | 30% | +₹74K (lets trades breathe) |
| dynamic_sl | ON | OFF | Avoids premature tightening |
| trailing_sl | ON | OFF | Rarely triggered, cuts winners |
| spot_move_exit | ON | OFF | Was exiting profitable positions |
| recovery_lock | ON | OFF | Was cutting profits short |
| breakeven buffer | 10% | 5% | Tighter breakeven protection |
| profit_target | ₹5K/lot | ₹10K/lot | Captures full theta |
| loss_limit | -₹4K/lot | -₹6K/lot | Fewer premature stops |
| re-entry | OFF | ON (2/day) | +₹36K recovery after SL hits |

---

## Next Steps

1. **Sync production config** with optimized parameters
2. **Paper trade** for 2-4 weeks to validate in live market conditions
3. **Monitor re-entry** performance separately to confirm backtest edge holds
4. **Consider seasonal analysis** — check if performance varies by month
5. **Forward walk analysis** — split data into train/test to check for overfitting
