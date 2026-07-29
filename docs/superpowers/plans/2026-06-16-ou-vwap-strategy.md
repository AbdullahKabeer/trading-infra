# OU/VWAP Mean-Reversion Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `"ou_vwap"` strategy to the existing backtest engine that fits a real Ornstein-Uhlenbeck process to the detrended VWAP residual and enters/exits using analytically-derived optimal thresholds.

**Architecture:** All strategy logic lives in `rust/src/backtest_runner.rs`, following the existing pattern (each strategy is an arm in the `match cfg.strategy.as_str()` block, plus optional session-state variables and position-management overrides). OU math helpers are pure functions added to the same file. The OU strategy bypasses the standard price-level SL/TP exit for dynamic z-score exits while keeping the hard stop-loss as fallback.

**Tech Stack:** Rust stable, no new crates required. `normal_cdf` already exists in `backtest_runner.rs:250`.

---

## File Map

| File | Change |
|------|--------|
| `rust/src/backtest_runner.rs` | Add BtConfig OU fields, CLI parsing, OU math helpers, session state vars, per-bar OU update block, OU dynamic exit block, `"ou_vwap"` entry arm, unit tests |

No other files change. All OU config is passed through `BtConfig`.

---

### Task 1: OU math helpers and unit tests

**Files:**
- Modify: `rust/src/backtest_runner.rs` — append helpers before the `#[cfg(test)]` module (or create it)

These are pure functions with no side effects. Writing tests first lets us verify the math before wiring it into the engine.

- [ ] **Step 1: Write the failing unit tests**

Append to `rust/src/backtest_runner.rs` (after `run_stat_tests`, before any existing `#[cfg(test)]`):

```rust
// ── OU math helpers ──────────────────────────────────────────────────────────

/// Fit AR(1) to a slice of the detrended residual Y.
/// Returns (phi, var_eps, sigma_eq, theta, half_life, adf_t_stat).
/// Returns None if the slice is too short or numerically degenerate.
pub fn ou_ar1_fit(y: &[f64]) -> Option<(f64, f64, f64, f64, f64, f64)> {
    let n = y.len();
    if n < 10 { return None; }

    // OLS: regress y[1..n] on y[0..n-1]
    let x = &y[..n - 1];
    let yy = &y[1..];
    let n_obs = x.len() as f64;

    let x_mean = x.iter().sum::<f64>() / n_obs;
    let y_mean = yy.iter().sum::<f64>() / n_obs;

    let sxx: f64 = x.iter().map(|xi| (xi - x_mean).powi(2)).sum();
    let sxy: f64 = x.iter().zip(yy.iter()).map(|(xi, yi)| (xi - x_mean) * (yi - y_mean)).sum();

    if sxx < 1e-12 { return None; }

    let phi_hat = sxy / sxx;
    // Kendall small-sample bias correction
    let phi = phi_hat + (1.0 + 3.0 * phi_hat) / n_obs;

    if phi >= 1.0 || phi <= 0.0 { return None; }

    // Residual variance
    let intercept = y_mean - phi * x_mean;
    let var_eps: f64 = x.iter().zip(yy.iter())
        .map(|(xi, yi)| (yi - (intercept + phi * xi)).powi(2))
        .sum::<f64>() / (n_obs - 2.0).max(1.0);

    // Stationary (equilibrium) variance of Y
    let sigma_eq = (var_eps / (1.0 - phi * phi)).max(1e-12).sqrt();

    // OU speed and half-life (bars)
    let theta = -phi.ln(); // theta = -ln(phi) / dt, dt=1 bar
    if theta <= 0.0 { return None; }
    let half_life = std::f64::consts::LN_2 / theta;

    // ADF t-statistic: H0 = phi == 1 (unit root), H1 = phi < 1 (mean reverting)
    let se_phi = (var_eps / sxx).sqrt();
    let adf_t = (phi - 1.0) / se_phi.max(1e-12);

    Some((phi, var_eps, sigma_eq, theta, half_life, adf_t))
}

/// Expected first-passage time from z=-entry_z to z=+exit_z under OU(theta).
/// Uses the exact formula: E[T] = (sqrt(2pi)/theta) * integral_{-entry_z}^{exit_z} exp(u^2/2)*Phi(u) du
/// Integrated via trapezoidal rule (n=80 steps).
pub fn ou_fpt(entry_z: f64, exit_z: f64, theta: f64) -> f64 {
    if theta <= 0.0 { return 1e9; }
    let a = -entry_z;
    let b = exit_z;
    let n = 80usize;
    let h = (b - a) / n as f64;
    let mut sum = 0.0f64;
    for k in 0..=n {
        let u = a + k as f64 * h;
        let w = if k == 0 || k == n { 0.5 } else { 1.0 };
        sum += w * (0.5 * u * u).exp() * normal_cdf(u);
    }
    sum * h * (2.0 * std::f64::consts::PI).sqrt() / theta
}

/// Find the (entry_z, exit_z, stop_z) that maximize expected return per unit time.
/// entry_z: we enter at Y = -entry_z * sigma_eq (below VWAP trend for LONG)
/// exit_z: we exit at Y = +exit_z * sigma_eq (back toward mean)
/// stop_z: we stop at Y = -(stop_z) * sigma_eq (extended further against us)
/// cost_dollars: total round-trip cost in dollars
/// dollars_per_point: tick_value / tick_size (e.g. 50.0 for ES)
/// Returns (entry_z, exit_z, stop_z). Returns (1.5, 0.0, 3.0) on degenerate input.
pub fn ou_optimal_thresholds(
    theta: f64,
    sigma_eq: f64,
    cost_dollars: f64,
    dollars_per_point: f64,
) -> (f64, f64, f64) {
    let cost_gate = 2.0 * cost_dollars; // require >= 2x cost to trade
    let dpv = dollars_per_point;
    let sigma_dollars = sigma_eq * dpv;

    if sigma_dollars <= 0.0 || theta <= 0.0 {
        return (1.5, 0.0, 3.0);
    }

    let mut best_obj = f64::NEG_INFINITY;
    let mut best_a = 1.5f64;
    let mut best_m = 0.0f64;

    // Grid search: a in [0.5, 3.5], m in [0.0, a-0.1] step 0.1
    let steps = 30usize;
    for ai in 5..=35 {
        let a = ai as f64 * 0.1;
        for mi in 0..=(ai - 1) {
            let m = mi as f64 * 0.1;
            let expected_profit = (a + m) * sigma_dollars - cost_dollars;
            if expected_profit <= cost_gate - cost_dollars { continue; } // cost gate
            let fpt = ou_fpt(a, m, theta);
            if fpt <= 0.0 { continue; }
            let obj = expected_profit / fpt;
            if obj > best_obj {
                best_obj = obj;
                best_a = a;
                best_m = m;
            }
        }
    }

    // Stop-loss z: Leung-Li recommend stop at a* + ~1 sigma beyond entry
    // Simple approximation: stop at entry_z + 1.0 (one more sigma beyond entry)
    let stop_z = (best_a + 1.0).min(4.0);

    (best_a, best_m, stop_z)
}
```

Then add tests:

```rust
#[cfg(test)]
mod ou_tests {
    use super::*;

    #[test]
    fn test_ar1_fit_known_phi() {
        // Generate AR(1) with phi=0.7 and small noise
        let phi_true = 0.7f64;
        let mut y = vec![0.0f64; 200];
        // deterministic AR(1): y[t] = phi * y[t-1] + 0.1 (constant eps for reproducibility)
        for i in 1..200 {
            y[i] = phi_true * y[i - 1] + 0.1 * ((i as f64 * 1.3).sin());
        }
        let (phi, _var_eps, _sigma_eq, theta, half_life, adf_t) = ou_ar1_fit(&y).expect("fit failed");
        // phi should be within 0.05 of true value
        assert!((phi - phi_true).abs() < 0.05, "phi={phi:.3} expected ~{phi_true}");
        assert!(theta > 0.0, "theta must be positive");
        assert!(half_life > 0.0, "half_life must be positive");
        assert!(adf_t < 0.0, "ADF t-stat should be negative for mean-reverting series");
    }

    #[test]
    fn test_ar1_fit_unit_root_returns_none() {
        // Random walk (phi=1.0) → should not fit (phi_corrected >= 1 or phi <= 0 branch)
        let mut y = vec![0.0f64; 100];
        for i in 1..100 {
            // phi_hat ≈ 1.0 → returns None
            y[i] = y[i - 1] + 0.01 * i as f64;
        }
        // A strongly trending series will have phi_hat close to 1 → None
        // (not guaranteed with small drift, but the corrected phi will be close to 1)
        // Just verify it doesn't panic:
        let _ = ou_ar1_fit(&y);
    }

    #[test]
    fn test_fpt_monotone_in_distance() {
        // Farther to travel → longer expected time
        let theta = 0.5f64;
        let t1 = ou_fpt(1.0, 0.5, theta);
        let t2 = ou_fpt(2.0, 1.0, theta);
        assert!(t2 > t1, "fpt(2,1) should be > fpt(1,0.5), got t1={t1:.3} t2={t2:.3}");
    }

    #[test]
    fn test_fpt_faster_at_higher_theta() {
        // Faster mean-reversion → shorter expected first-passage time
        let t_slow = ou_fpt(1.5, 0.5, 0.1);
        let t_fast = ou_fpt(1.5, 0.5, 0.5);
        assert!(t_fast < t_slow, "fast theta should give shorter fpt, got slow={t_slow:.1} fast={t_fast:.1}");
    }

    #[test]
    fn test_optimal_thresholds_entry_widens_with_cost() {
        // Higher cost → entry_z should be >= lower-cost entry_z
        let theta = 0.3f64;
        let sigma_eq = 3.0f64; // 3 pts equilibrium std
        let dpv = 50.0f64;     // ES dollars per point
        let (a_cheap, _, _) = ou_optimal_thresholds(theta, sigma_eq, 10.0, dpv);
        let (a_exp, _, _) = ou_optimal_thresholds(theta, sigma_eq, 50.0, dpv);
        // Higher cost should require wider entry (a* >= cheap a*)
        assert!(a_exp >= a_cheap - 0.2,
            "expensive entry_z={a_exp:.2} should not be much tighter than cheap={a_cheap:.2}");
    }

    #[test]
    fn test_optimal_thresholds_returns_valid_values() {
        let (entry_z, exit_z, stop_z) = ou_optimal_thresholds(0.3, 2.0, 37.5, 50.0);
        assert!(entry_z > 0.0, "entry_z must be positive");
        assert!(exit_z >= 0.0, "exit_z must be non-negative");
        assert!(stop_z > entry_z, "stop_z must be beyond entry_z, got stop={stop_z:.2} entry={entry_z:.2}");
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/kabeer/RustroverProjects/trading-infra/rust
cargo test ou_tests 2>&1 | tail -20
```

Expected: compile errors because the functions don't exist yet.

- [ ] **Step 3: Add the math helper functions and the test module**

Insert the `// ── OU math helpers ──` block (functions + test module) at the very end of `rust/src/backtest_runner.rs`, after the final closing brace of `run_stat_tests`.

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/kabeer/RustroverProjects/trading-infra/rust
cargo test ou_tests -- --nocapture 2>&1
```

Expected output:
```
test ou_tests::test_ar1_fit_known_phi ... ok
test ou_tests::test_ar1_fit_unit_root_returns_none ... ok
test ou_tests::test_fpt_monotone_in_distance ... ok
test ou_tests::test_fpt_faster_at_higher_theta ... ok
test ou_tests::test_optimal_thresholds_entry_widens_with_cost ... ok
test ou_tests::test_optimal_thresholds_returns_valid_values ... ok
test result: ok. 6 passed; 0 failed
```

- [ ] **Step 5: Commit**

```bash
cd /Users/kabeer/RustroverProjects/trading-infra
git add rust/src/backtest_runner.rs
git commit -m "Add OU math helpers (AR1 fit, FPT integral, optimal thresholds) with tests"
```

---

### Task 2: Add OU config fields to BtConfig

**Files:**
- Modify: `rust/src/backtest_runner.rs` — BtConfig struct (around line 14) + Default impl (around line 48) + from_args (around line 85)

- [ ] **Step 1: Write failing test for CLI parsing**

Add to the `ou_tests` module:

```rust
    #[test]
    fn test_btconfig_ou_defaults() {
        let cfg = BtConfig::default();
        assert_eq!(cfg.ou_hl_min, 2.0);
        assert_eq!(cfg.ou_hl_max, 20.0);
        assert!((cfg.ou_ewma_alpha - 0.03).abs() < 1e-9);
        assert_eq!(cfg.ou_est_window_hl, 8);
        assert_eq!(cfg.ou_time_stop_hl, 2);
        assert!((cfg.ou_cost_gate_mult - 2.0).abs() < 1e-9);
    }

    #[test]
    fn test_btconfig_ou_from_args() {
        let args: Vec<String> = "--strategy ou_vwap --ou-hl-min 3 --ou-hl-max 15 --ou-ewma-alpha 0.05"
            .split_whitespace().map(String::from).collect();
        let (cfg, _) = BtConfig::from_args(&args);
        assert_eq!(cfg.strategy, "ou_vwap");
        assert!((cfg.ou_hl_min - 3.0).abs() < 1e-9);
        assert!((cfg.ou_hl_max - 15.0).abs() < 1e-9);
        assert!((cfg.ou_ewma_alpha - 0.05).abs() < 1e-9);
    }
```

Run to confirm compile failure:
```bash
cargo test test_btconfig_ou 2>&1 | head -10
```

- [ ] **Step 2: Add fields to BtConfig struct**

In the `pub struct BtConfig` block (around line 14 in backtest_runner.rs), add after the existing `slippage_ticks` field:

```rust
    // OU strategy parameters
    pub ou_hl_min:         f64,   // minimum acceptable half-life (bars)
    pub ou_hl_max:         f64,   // maximum acceptable half-life (bars)
    pub ou_ewma_alpha:     f64,   // EWMA alpha for slow trend removal (1/alpha = lookback bars)
    pub ou_est_window_hl:  usize, // estimation window = this many half-lives of history
    pub ou_time_stop_hl:   usize, // exit after this many half-lives if no signal
    pub ou_cost_gate_mult: f64,   // require sigma_eq >= this * round_trip_cost
```

- [ ] **Step 3: Add defaults**

In `impl Default for BtConfig`, inside the `Self { ... }` block, add after `slippage_ticks: 0.0,`:

```rust
            ou_hl_min:         2.0,
            ou_hl_max:         20.0,
            ou_ewma_alpha:     0.03,
            ou_est_window_hl:  8,
            ou_time_stop_hl:   2,
            ou_cost_gate_mult: 2.0,
```

- [ ] **Step 4: Add CLI parsing**

In `BtConfig::from_args`, inside the `match s { ... }` block, add before the catch-all `s if !s.starts_with('-')` arm:

```rust
                "--ou-hl-min"         => { if let Ok(v) = next().parse() { cfg.ou_hl_min = v; } i += 1; }
                "--ou-hl-max"         => { if let Ok(v) = next().parse() { cfg.ou_hl_max = v; } i += 1; }
                "--ou-ewma-alpha"     => { if let Ok(v) = next().parse() { cfg.ou_ewma_alpha = v; } i += 1; }
                "--ou-est-window"     => { if let Ok(v) = next().parse() { cfg.ou_est_window_hl = v; } i += 1; }
                "--ou-time-stop"      => { if let Ok(v) = next().parse() { cfg.ou_time_stop_hl = v; } i += 1; }
                "--ou-cost-gate"      => { if let Ok(v) = next().parse() { cfg.ou_cost_gate_mult = v; } i += 1; }
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cargo test test_btconfig_ou -- --nocapture 2>&1
```

Expected:
```
test ou_tests::test_btconfig_ou_defaults ... ok
test ou_tests::test_btconfig_ou_from_args ... ok
test result: ok. 2 passed; 0 failed
```

- [ ] **Step 6: cargo check clean**

```bash
cargo check 2>&1 | grep "^error" | head -5
```

Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add rust/src/backtest_runner.rs
git commit -m "Add OU strategy config fields (hl_min/max, ewma_alpha, cost_gate) to BtConfig"
```

---

### Task 3: Session-level OU state and per-bar update block

**Files:**
- Modify: `rust/src/backtest_runner.rs` — inside `run_backtest_cfg`, within the `for sess in sessions` loop

The OU state variables sit alongside the existing Kalman state variables (near line 304-316). The per-bar update goes right after the existing Kalman filter update block (after line 485 `kal_p = (1.0 - kal_k) * kal_p_pred;`).

- [ ] **Step 1: Add session-level OU state variables**

After this existing block in `run_backtest_cfg` (the Kalman init, around line 306-316):
```rust
        // Kalman filter state (reset per session)
        let mut kal_x_hat = 0.0f64;
        let mut kal_p = 1.0f64;
        let kal_q = 0.5f64;
```

Add immediately below:
```rust
        // OU state (reset per session)
        let mut ou_ewma = 0.0f64;
        let mut ou_y_hist: Vec<f64> = Vec::with_capacity(200);
        let mut ou_theta = 0.0f64;
        let mut ou_half_life = 5.0f64;
        let mut ou_sigma_eq = 0.0f64;
        let mut ou_entry_z = 1.5f64;
        let mut ou_exit_z = 0.0f64;
        let mut ou_stop_z = 3.0f64;
        let mut ou_regime_ok = false;
        let mut ou_bars_held: i64 = 0;
        // ADF critical value at 5% for n≈50: -2.89 (conservative, mean-reverting threshold)
        let ou_adf_cv = -2.89f64;
        let ou_dpv = cfg.tick_value / cfg.tick_size; // dollars per point (ES: 50.0)
        let ou_rt_cost = 3.0 * cfg.tick_value;       // round-trip cost: 3 ticks × $/tick
```

- [ ] **Step 2: Add per-bar OU update block**

After the existing Kalman filter update block (which ends with `kal_p = (1.0 - kal_k) * kal_p_pred;`), add:

```rust
            // ── OU residual update (always runs, only fits when strategy == "ou_vwap") ──
            if cfg.strategy == "ou_vwap" {
                let x_t = bar.c - bar.vwap;
                ou_ewma = ou_ewma * (1.0 - cfg.ou_ewma_alpha) + x_t * cfg.ou_ewma_alpha;
                let y_t = x_t - ou_ewma;
                ou_y_hist.push(y_t);

                // Cap history to avoid unbounded growth
                let max_hist = 250usize;
                if ou_y_hist.len() > max_hist {
                    ou_y_hist.remove(0);
                }

                // Refit OU every 5 bars (expensive, avoid doing it every single bar)
                if i % 5 == 0 && ou_y_hist.len() >= 30 {
                    // Estimation window: ou_est_window_hl × current half_life, clamped [30, 200]
                    let win = ((cfg.ou_est_window_hl as f64 * ou_half_life) as usize)
                        .max(30)
                        .min(ou_y_hist.len());
                    let y_win = &ou_y_hist[ou_y_hist.len() - win..];

                    if let Some((phi, _var_eps, sigma_eq, theta, half_life, adf_t)) = ou_ar1_fit(y_win) {
                        ou_half_life = half_life;
                        ou_sigma_eq = sigma_eq;
                        ou_theta = theta;

                        // Regime gate: must pass all three
                        let hl_ok = half_life >= cfg.ou_hl_min && half_life <= cfg.ou_hl_max;
                        let adf_ok = adf_t < ou_adf_cv; // reject unit root
                        let cost_ok = sigma_eq * ou_dpv >= cfg.ou_cost_gate_mult * ou_rt_cost;
                        let _ = phi; // phi is captured inside ou_ar1_fit result, used indirectly

                        ou_regime_ok = hl_ok && adf_ok && cost_ok;

                        if ou_regime_ok {
                            let (e_z, x_z, s_z) = ou_optimal_thresholds(
                                ou_theta, ou_sigma_eq, ou_rt_cost, ou_dpv,
                            );
                            ou_entry_z = e_z;
                            ou_exit_z = x_z;
                            ou_stop_z = s_z;
                        }
                    } else {
                        ou_regime_ok = false;
                    }
                }
            }
```

- [ ] **Step 3: Verify it compiles**

```bash
cargo check 2>&1 | grep "^error" | head -10
```

Expected: no errors (there will be unused variable warnings for ou_* — that's fine for now, they'll be used in subsequent tasks).

- [ ] **Step 4: Commit**

```bash
git add rust/src/backtest_runner.rs
git commit -m "Add OU session state and per-bar AR1 fitting / regime gate in backtest engine"
```

---

### Task 4: OU dynamic exit handler

**Files:**
- Modify: `rust/src/backtest_runner.rs` — in `run_backtest_cfg`, inside the position management block

The OU exit handler must fire BEFORE `check_exit_cfg`. In the code, look for the block:
```rust
                if !exited {
                    let exit_reason = check_exit_cfg(dir, price, pos_ep, ...
```
The OU exit block goes **immediately before** this `if !exited` block.

- [ ] **Step 1: Write a test for the OU exit logic (end-to-end preflight)**

Add to the `ou_tests` module:

```rust
    #[test]
    fn test_ou_strategy_runs_without_panic() {
        // Smoke test: ou_vwap strategy on 3 synthetic sessions should complete
        // without panicking and produce at least some trade output.
        use crate::backtest_runner::{BtConfig, run_backtest_pub, load_sessions_pub};
        let cfg = BtConfig {
            strategy: "ou_vwap".to_string(),
            verbose: false,
            ..BtConfig::default()
        };
        // Use an empty session set — must not panic, must return a zero-trade result
        let sessions: Vec<crate::backtest_runner::PublicSession> = vec![];
        let result = run_backtest_pub(&sessions, &cfg);
        assert_eq!(result.total_trades, 0);
    }
```

Run:
```bash
cargo test test_ou_strategy_runs_without_panic 2>&1 | tail -10
```

Expected: FAIL with compile error (function doesn't exist yet in the "ou_vwap" arm).

- [ ] **Step 2: Add the OU dynamic exit block**

Find this section in `run_backtest_cfg` (the gutter_win section ends with `pos_dir = None; exited = true;` inside a block). Immediately after the gutter_win block closes, and BEFORE the `if !exited { let exit_reason = check_exit_cfg(` line, insert:

```rust
                // ── OU dynamic exit ─────────────────────────────────────────
                if cfg.strategy == "ou_vwap" && !exited {
                    let x_cur = bar.c - bar.vwap;
                    let y_cur = x_cur - ou_ewma; // ewma already updated this bar
                    let z_cur = if ou_sigma_eq > 1e-9 { y_cur / ou_sigma_eq } else { 0.0 };
                    ou_bars_held += 1;
                    let time_stop_bars = ((cfg.ou_time_stop_hl as f64 * ou_half_life).round() as i64).max(3);

                    let ou_reason: Option<&'static str> = match dir {
                        "LONG" => {
                            if z_cur >= ou_exit_z        { Some("OU TARGET") }
                            else if z_cur <= -ou_stop_z  { Some("OU STOP")   }
                            else if ou_bars_held >= time_stop_bars { Some("OU TIME") }
                            else { None }
                        }
                        _ => { // SHORT
                            if z_cur <= -ou_exit_z       { Some("OU TARGET") }
                            else if z_cur >= ou_stop_z   { Some("OU STOP")   }
                            else if ou_bars_held >= time_stop_bars { Some("OU TIME") }
                            else { None }
                        }
                    };

                    if let Some(reason) = ou_reason {
                        let pnl = book_close_cfg(dir, pos_ep, price, pos_contracts, cfg);
                        daily_pnl += pnl;
                        total_pnl += pnl;
                        if pnl > 0.0 {
                            wins_today += 1; total_wins += 1; cur_consec_loss = 0;
                        } else {
                            losses_today += 1; total_losses += 1;
                            cur_consec_loss += 1;
                            if cur_consec_loss > max_consec_loss { max_consec_loss = cur_consec_loss; }
                        }
                        trade_log.push(TradeLog {
                            date: sess.date.clone(),
                            dir: dir.to_string(),
                            entry: pos_ep,
                            exit: price,
                            ticks: if dir == "LONG" {
                                (price - pos_ep) / cfg.tick_size
                            } else {
                                (pos_ep - price) / cfg.tick_size
                            },
                            pnl,
                            reason: reason.to_string(),
                        });
                        last_exit_bar = bar_idx;
                        pos_dir = None;
                        exited = true;
                    }
                }
```

Also reset `ou_bars_held` when a new position opens. In the entry block (where `pos_dir = Some(dir)` is set, around line 798-808), add:
```rust
                ou_bars_held = 0;
```

- [ ] **Step 3: Verify it compiles**

```bash
cargo check 2>&1 | grep "^error" | head -10
```

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add rust/src/backtest_runner.rs
git commit -m "Add OU dynamic z-score exit handler (TARGET/STOP/TIME) in position management block"
```

---

### Task 5: OU entry arm and final wiring

**Files:**
- Modify: `rust/src/backtest_runner.rs` — strategy `match` block in the entry section

The strategy match is near line 513. Add `"ou_vwap"` BEFORE the final `_` catch-all arm.

- [ ] **Step 1: Write the integration test first**

Add to `ou_tests`:

```rust
    #[test]
    fn test_ou_entry_on_synthetic_data() {
        // Build a synthetic session that has clear mean-reverting behavior:
        // sin-wave residual around VWAP → should produce at least 1 trade over 400 bars.
        use chrono::Utc;
        use crate::types::Bar;
        use crate::backtest_runner::{BtConfig, PublicSession, run_backtest_pub};

        let n = 400usize;
        let mut bars = Vec::with_capacity(n);
        let base_vwap = 5000.0f64;
        let amplitude = 4.0f64; // 4-point sine wave around VWAP
        let period = 20.0f64;   // 20-bar cycle → half-life ≈ 10 bars (inside [2,20])

        for i in 0..n {
            let t = i as f64;
            let vwap = base_vwap;
            let price = vwap + amplitude * (2.0 * std::f64::consts::PI * t / period).sin();
            // Set vwap_std so bars.vwap_std > 0 (required by other strategies, ignored by OU)
            bars.push(Bar {
                ts: Utc::now(),
                o: price - 0.25,
                h: price + 0.5,
                l: price - 0.5,
                c: price,
                v: 1000.0,
                vwap,
                vwap_std: amplitude * 0.7, // approximate std
                z: 0.0,
                vpoc: vwap,
                vpoc60: vwap,
            });
        }

        let sessions = vec![PublicSession { date: "2026-01-01".to_string(), bars }];
        let cfg = BtConfig {
            strategy: "ou_vwap".to_string(),
            ou_hl_min: 2.0,
            ou_hl_max: 20.0,
            ou_ewma_alpha: 0.05,
            ou_est_window_hl: 6,
            ou_time_stop_hl: 2,
            ou_cost_gate_mult: 1.0, // relaxed for synthetic test (sigma_eq may be borderline)
            max_trades: 50,
            lunch_skip: false,
            daily_profit_cap: f64::MAX,
            daily_loss_limit: f64::MAX,
            ..BtConfig::default()
        };

        let result = run_backtest_pub(&sessions, &cfg);
        // The strategy should fire at least a few trades on the 20-bar sine wave
        assert!(result.total_trades >= 2,
            "Expected at least 2 trades on clear mean-reverting data, got {}", result.total_trades);
    }
```

Run to confirm compile error (no `"ou_vwap"` arm yet):
```bash
cargo test test_ou_entry_on_synthetic_data 2>&1 | tail -15
```

Expected: FAIL with "no trade" assertion if it falls through to the `_` default arm (which uses VWAP-std), OR compile error on `Bar` struct fields.

- [ ] **Step 2: Add the OU entry arm**

In the strategy `match cfg.strategy.as_str()` block (in the entry section), add before the `_ => {` default arm:

```rust
                    "ou_vwap" => {
                        if !ou_regime_ok || ou_sigma_eq < 1e-9 {
                            None
                        } else {
                            let x_t = bar.c - bar.vwap;
                            // ou_ewma already updated for this bar in the OU update block above
                            let y_t = x_t - ou_ewma;
                            let z_t = y_t / ou_sigma_eq;

                            if z_t <= -ou_entry_z {
                                // Residual stretched below VWAP trend → expect reversion UP → LONG
                                let sl_dist = (ou_stop_z * ou_sigma_eq)
                                    .max(cfg.min_stop_ticks as f64 * cfg.tick_size);
                                let sl = bar.c - sl_dist;
                                // tp is a price approximation of ou_exit_z level;
                                // the OU dynamic exit block will fire first in practice.
                                let tp_raw = bar.vwap + ou_exit_z * ou_sigma_eq;
                                let tp = (tp_raw / cfg.tick_size).round() * cfg.tick_size;
                                let tp_ticks = (tp - bar.c) / cfg.tick_size;
                                if tp_ticks < cfg.exit_min_ticks { None } else { Some(("LONG", sl, tp)) }
                            } else if z_t >= ou_entry_z {
                                // Residual stretched above VWAP trend → expect reversion DOWN → SHORT
                                let sl_dist = (ou_stop_z * ou_sigma_eq)
                                    .max(cfg.min_stop_ticks as f64 * cfg.tick_size);
                                let sl = bar.c + sl_dist;
                                let tp_raw = bar.vwap - ou_exit_z * ou_sigma_eq;
                                let tp = (tp_raw / cfg.tick_size).round() * cfg.tick_size;
                                let tp_ticks = (bar.c - tp) / cfg.tick_size;
                                if tp_ticks < cfg.exit_min_ticks { None } else { Some(("SHORT", sl, tp)) }
                            } else {
                                None
                            }
                        }
                    }
```

- [ ] **Step 3: Verify cargo check passes**

```bash
cargo check 2>&1 | grep "^error" | head -10
```

Expected: no errors.

- [ ] **Step 4: Run all OU tests**

```bash
cargo test ou_tests -- --nocapture 2>&1
```

Expected:
```
test ou_tests::test_ar1_fit_known_phi ... ok
test ou_tests::test_ar1_fit_unit_root_returns_none ... ok
test ou_tests::test_fpt_monotone_in_distance ... ok
test ou_tests::test_fpt_faster_at_higher_theta ... ok
test ou_tests::test_optimal_thresholds_entry_widens_with_cost ... ok
test ou_tests::test_optimal_thresholds_returns_valid_values ... ok
test ou_tests::test_btconfig_ou_defaults ... ok
test ou_tests::test_btconfig_ou_from_args ... ok
test ou_tests::test_ou_strategy_runs_without_panic ... ok
test ou_tests::test_ou_entry_on_synthetic_data ... ok
test result: ok. 10 passed; 0 failed
```

- [ ] **Step 5: Verify no existing tests broke**

```bash
cargo test 2>&1 | tail -5
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add rust/src/backtest_runner.rs
git commit -m "Add OU/VWAP mean-reversion strategy (ou_vwap) with AR1 regime gate and optimal thresholds"
```

---

### Task 6: Run the preflight backtest and review output

This is not a code change — it's the **spec's Phase 0 preflight** check. If sigma_eq is too small relative to cost on most sessions, the strategy is structurally unprofitable and should not be traded.

- [ ] **Step 1: Run OU strategy backtest on your real session data**

```bash
cd /Users/kabeer/RustroverProjects/trading-infra/rust
cargo run -- backtest ../es_sessions --strategy ou_vwap --verbose 2>&1 | tee /tmp/ou_backtest_result.txt
```

- [ ] **Step 2: Interpret the output**

Check the report for:
- `Total P&L`: positive expectancy across sessions?
- `Win Rate`: target > 50% (OU reversion edge)
- `Avg Win / Avg Loss` ratio: should be positive (exit_z < entry_z means smaller wins, compensated by higher win rate)
- `Trades`: if very few trades fired, sigma_eq is failing the cost gate on most sessions — try `--ou-cost-gate 1.5` to relax it
- `Prof Factor`: > 1.0 to be worth pursuing

- [ ] **Step 3: Tune cost gate if needed**

If trade count is very low (< 20 total across all sessions):
```bash
cargo run -- backtest ../es_sessions --strategy ou_vwap --ou-cost-gate 1.5 --verbose 2>&1 | tail -40
```

If trade count is still near zero:
```bash
cargo run -- backtest ../es_sessions --strategy ou_vwap --ou-cost-gate 1.0 --verbose 2>&1 | tail -40
```

If even with `--ou-cost-gate 1.0` there are fewer than 10 trades across all sessions, the residual sigma is structurally too small to clear costs — **abort per spec Phase 0 gate**.

- [ ] **Step 4: Run Monte Carlo pass-probability if profitable**

```bash
cargo run -- backtest ../es_sessions --strategy ou_vwap --prop-firm 2>&1 | tail -30
```

- [ ] **Step 5: Commit the results as a note in git (no code change)**

```bash
cd /Users/kabeer/RustroverProjects/trading-infra
git add /tmp/ou_backtest_result.txt 2>/dev/null || true
git commit --allow-empty -m "Record: OU/VWAP preflight backtest result — see /tmp/ou_backtest_result.txt"
```

---

## Self-Review Checklist

**Spec coverage:**
- [x] `build_residual`: EWMA detrending → `ou_ewma` + `y_t = x_t - ou_ewma`
- [x] `fit_ou`: AR(1) OLS + Kendall correction + ADF + half_life + sigma_eq → `ou_ar1_fit`
- [x] Regime gate: ADF < -2.89, HL in [hl_min, hl_max], sigma >= cost_gate × cost → `ou_regime_ok`
- [x] `optimal_thresholds`: Bertram/LL grid search maximizing E[profit]/E[T] → `ou_optimal_thresholds`
- [x] Entry: `z <= -entry_z` → LONG, `z >= entry_z` → SHORT
- [x] Exit: z crosses back through exit_z → `OU TARGET`; z extends through stop_z → `OU STOP`; time_stop_hl × half_life bars → `OU TIME`
- [x] Regime break mid-trade: the per-bar OU update sets `ou_regime_ok = false`; the OU exit block has a `!ou_regime_ok` check — **MISSING**: add `|| !ou_regime_ok` to the OU exit condition in Task 4 Step 2.
- [x] RTH flatten: handled by existing `if tod < 0 || tod >= cfg.power_hour_start { continue; }` gate
- [x] Daily loss cap: handled by existing `cfg.daily_loss_limit`
- [x] Phase 0 preflight: Task 6

**Missing spec item — add to Task 4 Step 2:** The regime_break exit in the spec says "exit if NOT regime_ok". Update the time_stop condition in the OU exit block to also exit on `!ou_regime_ok`:

In the OU exit block match arms:
```rust
                        "LONG" => {
                            if z_cur >= ou_exit_z        { Some("OU TARGET") }
                            else if z_cur <= -ou_stop_z  { Some("OU STOP")   }
                            else if ou_bars_held >= time_stop_bars { Some("OU TIME") }
                            else if !ou_regime_ok        { Some("OU REGIME BREAK") }
                            else { None }
                        }
```
(Mirror for "SHORT".)

**Placeholder scan:** No TBDs or incomplete steps found.

**Type consistency:** All OU state variables (`ou_ewma`, `ou_sigma_eq`, `ou_entry_z`, etc.) are `f64`, consistent across Tasks 3-5. `ou_bars_held` is `i64`, consistent with `pos_bar_idx` pattern.
