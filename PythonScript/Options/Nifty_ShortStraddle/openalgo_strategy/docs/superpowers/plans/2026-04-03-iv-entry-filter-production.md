# IV Entry Filter — Production Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an IV entry filter to the production short straddle strategy that skips entry when ATM implied volatility < 12%, using the OpenAlgo `optiongreeks` API (Black-76 model).

**Architecture:** At entry time (09:17), after existing filters pass, fetch IV for both ATM CE and PE via `broker.get().optiongreeks()`. Average the two IVs. If avg IV < 12%, skip entry. The existing VIX entry filter (min=11, max=25) remains active — VIX guards the upper bound (crash days), IV guards the lower bound (thin premiums).

**Tech Stack:** OpenAlgo Python SDK (`optiongreeks` method), Black-76 model (server-side via py_vollib)

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `ShortStraddle/nifty_short_straddle.py` | Modify | Add constants, `fetch_iv()` helper, IV filter check in `_try_entry()` |

Single file change — the production strategy is a monolith by design.

---

### Task 1: Add IV Entry Filter Constants

**Files:**
- Modify: `ShortStraddle/nifty_short_straddle.py:97-103` (after ORB filter constants, before Combined SL)

- [ ] **Step 1: Add IV entry filter constants after line 99 (ORB_THRESHOLD_PCT)**

Add these lines between the ORB filter block and the Combined SL block:

```python
# Fix 8: IV entry filter — skip entry if ATM implied volatility too low
# Uses OpenAlgo optiongreeks API (Black-76 model) to compute real-time IV
# Backtest validated: Calmar 328 vs 282 (production), max DD -6,719 vs -9,516
IV_ENTRY_FILTER_ENABLED  = True
IV_ENTRY_MIN             = 12.0  # Skip if avg(CE_IV, PE_IV) < this %
```

- [ ] **Step 2: Verify no syntax errors**

Run: `cd ShortStraddle && python -c "import nifty_short_straddle; print('IV_ENTRY_MIN:', nifty_short_straddle.IV_ENTRY_MIN)"`
Expected: `IV_ENTRY_MIN: 12.0`

- [ ] **Step 3: Commit**

```bash
git add ShortStraddle/nifty_short_straddle.py
git commit -m "feat: add IV entry filter constants (min=12%)"
```

---

### Task 2: Add `fetch_iv()` Helper Function

**Files:**
- Modify: `ShortStraddle/nifty_short_straddle.py:511` (after `fetch_vix()`, before EXPIRY RESOLUTION section)

- [ ] **Step 1: Add `fetch_iv()` function after `fetch_vix()` (after line 511)**

Insert between `fetch_vix()` and the `# EXPIRY RESOLUTION` section header:

```python

def fetch_iv(symbol: str) -> float:
    """Fetch implied volatility for an option symbol via OpenAlgo optiongreeks API.

    Uses Black-76 model (server-side). Returns IV as percentage (e.g. 14.85),
    or 0.0 on failure.
    """
    try:
        resp = broker.get().optiongreeks(symbol=symbol, exchange=OPTION_EXCH)
        if api_ok(resp):
            iv = float(resp.get("implied_volatility", 0) or 0)
            if iv > 0:
                plog(f"fetch_iv({symbol}): IV={iv:.2f}%", "DEBUG")
                return iv
        plog(f"fetch_iv({symbol}): failed — {api_err(resp)}", "WARNING")
    except Exception as exc:
        plog(f"fetch_iv({symbol}): {exc}", "WARNING")
    return 0.0
```

- [ ] **Step 2: Verify no syntax errors**

Run: `cd ShortStraddle && python -c "from nifty_short_straddle import fetch_iv; print('fetch_iv loaded OK')"`
Expected: `fetch_iv loaded OK`

- [ ] **Step 3: Commit**

```bash
git add ShortStraddle/nifty_short_straddle.py
git commit -m "feat: add fetch_iv() helper using OpenAlgo optiongreeks API"
```

---

### Task 3: Add IV Filter Check in `_try_entry()`

**Files:**
- Modify: `ShortStraddle/nifty_short_straddle.py:1627-1638` (inside `_try_entry()`, after ORB filter block, before weekly drawdown guard)

- [ ] **Step 1: Add IV entry filter block after the ORB filter block (after line 1638) and before the weekly drawdown guard**

Insert between the ORB filter `return` block and the `# Weekly drawdown guard` comment:

```python
        # IV entry filter — skip if ATM implied volatility too low
        if IV_ENTRY_FILTER_ENABLED:
            spot = fetch_ltp(UNDERLYING, EXCHANGE)
            if spot > 0:
                atm_strike = round(spot / STRIKE_ROUNDING) * STRIKE_ROUNDING
                expiry = resolve_expiry()
                if expiry:
                    sym_ce = f"{UNDERLYING}{expiry}{atm_strike}CE"
                    sym_pe = f"{UNDERLYING}{expiry}{atm_strike}PE"
                    ce_iv = fetch_iv(sym_ce)
                    pe_iv = fetch_iv(sym_pe)
                    if ce_iv > 0 and pe_iv > 0:
                        avg_iv = (ce_iv + pe_iv) / 2.0
                        if avg_iv < IV_ENTRY_MIN:
                            plog(f"IV entry filter: avg IV={avg_iv:.2f}% < {IV_ENTRY_MIN}% — skip")
                            telegram.notify(
                                f"🚫 Entry skipped — IV too low: {avg_iv:.2f}% < {IV_ENTRY_MIN}%\n"
                                f"CE IV: {ce_iv:.2f}% | PE IV: {pe_iv:.2f}%"
                            )
                            self._entry_done_today = True
                            return
                        plog(f"IV entry filter: avg IV={avg_iv:.2f}% >= {IV_ENTRY_MIN}% — OK")
                    else:
                        plog(f"IV entry filter: could not fetch IV (CE={ce_iv}, PE={pe_iv}) — allowing entry", "WARNING")

```

- [ ] **Step 2: Verify no syntax errors**

Run: `cd ShortStraddle && python -c "from nifty_short_straddle import Scheduler; print('Scheduler loaded OK')"`
Expected: `Scheduler loaded OK`

- [ ] **Step 3: Commit**

```bash
git add ShortStraddle/nifty_short_straddle.py
git commit -m "feat: add IV entry filter to _try_entry() — skip if avg IV < 12%"
```

---

### Task 4: Update Backtest Config to Match Production

**Files:**
- Modify: `ShortStraddle/backtest/config/config_production.toml:100` (after orb_filter, before charges)

- [ ] **Step 1: Add IV entry filter section to production backtest config**

Insert after the `[risk.orb_filter]` block and before `[charges]`:

```toml
# Fix 8: IV entry filter — skip entry if ATM IV too low (Black-76)
[risk.iv_entry_filter]
enabled = true
min = 12.0
risk_free_rate = 0.065
```

- [ ] **Step 2: Verify TOML is valid**

Run: `cd ShortStraddle/backtest && python -c "import toml; c = toml.load('config/config_production.toml'); print('IV filter:', c['risk']['iv_entry_filter'])"`
Expected: `IV filter: {'enabled': True, 'min': 12.0, 'risk_free_rate': 0.065}`

- [ ] **Step 3: Commit**

```bash
git add ShortStraddle/backtest/config/config_production.toml
git commit -m "feat: sync production backtest config with IV entry filter"
```

---

## Design Decisions

1. **Fail-open on IV fetch failure:** If the API call fails, we allow entry rather than blocking. This matches the production philosophy — we don't want API flakiness to prevent trading.

2. **Average of CE and PE IV:** ATM CE and PE should have nearly identical IV (put-call parity). Averaging provides a robust estimate and handles minor skew.

3. **VIX filter stays active:** VIX max=25 protects against crash days (high vol). IV min=12 protects against low-premium days. They cover different risk scenarios.

4. **Telegram notification on skip:** Trader gets notified when IV filter blocks entry, with exact CE/PE IV values for transparency.

5. **resolve_expiry() called early:** The IV filter needs the expiry to build option symbols. `resolve_expiry()` is already cached and fast. It gets called again later in the normal flow — this is acceptable since it's an API call that returns quickly.
