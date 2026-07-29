use std::path::Path;
use std::fs;

fn main() {
    let dir = "../es_sessions";
    let p = Path::new(dir);
    let mut files: Vec<_> = fs::read_dir(p).unwrap()
        .filter_map(|e| e.ok())
        .filter(|e| e.path().extension().map(|x| x == "json").unwrap_or(false))
        .map(|e| e.path())
        .collect();
    files.sort();

    println!("{:<12} {:>8} {:>8} {:>6} {:>6} {:>6} {:>8} {:>8} {:>6}",
        "DATE", "HL", "SIGMA_PT", "ADF_T", "MAX_Z", "ENTRY_Z", "OBJ", "BARS_OK", "SIGS");

    let mut total_ok = 0usize;
    let mut total_sigs = 0usize;
    let mut total_sess = 0usize;

    for path in &files {
        let text = fs::read_to_string(path).unwrap();
        let raw: serde_json::Value = serde_json::from_str(&text).unwrap();
        let date = raw["date"].as_str().unwrap_or("?").to_string();
        let raw_bars = match raw["bars"].as_array() { Some(b) => b, None => continue };
        if raw_bars.len() < 50 { continue; }
        total_sess += 1;

        let mut cum_pv = 0.0f64;
        let mut cum_v = 0.0f64;
        let mut ewma = 0.0f64;
        let mut y_buf: Vec<f64> = Vec::new();
        let mut bars_regime_ok = 0usize;
        let mut bars_signal = 0usize;

        let n_bars = raw_bars.len();
        let mut last_hl = 10.0f64;
        let mut last_sigma = 0.0f64;
        let mut last_adf = 0.0f64;
        let mut last_entry_z = 1.5f64;
        let mut last_exit_z = 0.3f64;
        let mut last_obj = 0.0f64;
        let mut max_abs_z = 0.0f64;

        for (i, rb) in raw_bars.iter().enumerate() {
            let c = rb["c"].as_f64().unwrap_or(0.0);
            let v = rb["v"].as_f64().unwrap_or(0.0);
            if c == 0.0 { continue; }
            if i < 15 { continue; } // match backtest warmup
            cum_pv += c * v.max(1.0);
            cum_v += v.max(1.0);
            let vwap = cum_pv / cum_v;
            let raw_res = c - vwap;
            ewma = if y_buf.is_empty() { raw_res } else { ewma * 0.95 + raw_res * 0.05 };
            let y = raw_res - ewma;
            y_buf.push(y);

            // Fit OU on rolling window
            let est_len = ((8.0 * last_hl.max(5.0)) as usize).max(30).min(y_buf.len());
            if est_len < 20 { continue; }

            let window = &y_buf[y_buf.len() - est_len..];
            let n = window.len();
            let m_f = (n - 1) as f64;
            let x = &window[..n-1];
            let yy = &window[1..];
            let x_mean = x.iter().sum::<f64>() / m_f;
            let y_mean = yy.iter().sum::<f64>() / m_f;
            let sxx: f64 = x.iter().map(|&xi| (xi - x_mean).powi(2)).sum();
            let sxy: f64 = x.iter().zip(yy.iter()).map(|(&xi, &yi)| (xi - x_mean) * (yi - y_mean)).sum();
            if sxx < 1e-12 { continue; }
            let phi_hat = sxy / sxx;
            let phi = (phi_hat + (1.0 + 3.0 * phi_hat) / m_f).clamp(0.001, 0.999);
            if phi >= 1.0 || phi <= 0.0 { continue; }
            let theta = -phi.ln();
            let half_life = 0.693147 / theta;
            let c_int = y_mean - phi * x_mean;
            let var_eps: f64 = x.iter().zip(yy.iter())
                .map(|(&xi, &yi)| (yi - c_int - phi * xi).powi(2)).sum::<f64>() / (m_f - 2.0);
            let sigma_eq = (var_eps / (1.0 - phi * phi)).sqrt();
            let se_phi = (var_eps / sxx).sqrt();
            let adf_t = (phi_hat - 1.0) / se_phi;
            let sigma_dollars = sigma_eq * 50.0;
            let rt_dollars = 2.80f64;

            last_hl = half_life.clamp(0.5, 200.0);
            last_sigma = sigma_eq;
            last_adf = adf_t;

            let regime_ok = adf_t < -2.57
                && half_life >= 2.0 && half_life <= 30.0
                && sigma_dollars >= rt_dollars * 2.0;

            // Compute optimal thresholds when regime is OK
            if regime_ok {
                // Simple Bertram grid search
                let mut best_obj = f64::NEG_INFINITY;
                let mut best_a = 1.5f64;
                let mut best_m = 0.0f64;
                for ai in 5usize..=35 {
                    let a = ai as f64 * 0.1;
                    for mi in 0..ai {
                        let m = mi as f64 * 0.1;
                        let profit = (a + m) * sigma_dollars - rt_dollars;
                        if profit <= rt_dollars { continue; }
                        // FPT approximation
                        let lo = -a;
                        let hi = m;
                        let np = 20usize;
                        let h = (hi - lo) / np as f64;
                        let mut s = 0.0f64;
                        for k in 0..=np {
                            let z = lo + k as f64 * h;
                            let w = if k == 0 || k == np { 1.0 } else if k % 2 == 1 { 4.0 } else { 2.0 };
                            let phi_z = 0.5 * (1.0 + libm_erf(z / 1.41421356));
                            s += w * (0.5 * z * z).min(40.0).exp() * phi_z;
                        }
                        let fpt = (6.283185_f64.sqrt() / theta) * s * h / 3.0;
                        if fpt <= 0.1 { continue; }
                        let obj = profit / fpt;
                        if obj > best_obj { best_obj = obj; best_a = a; best_m = m; }
                    }
                }
                last_entry_z = best_a;
                last_exit_z = best_m;
                last_obj = best_obj;
            }

            if regime_ok {
                bars_regime_ok += 1;
                // Check entry signal: skip lunch and power hour
                let tod = i as i64 - 15; // tod_mins from RTH open (approx, since bars are 1-min)
                if tod >= 0 && tod < 330 && !(tod >= 120 && tod < 195) {
                    let z_now = if sigma_eq > 1e-6 { y / sigma_eq } else { 0.0 };
                    if z_now.abs() > max_abs_z { max_abs_z = z_now.abs(); }
                    if z_now.abs() >= last_entry_z {
                        bars_signal += 1;
                    }
                }
            }
        }

        total_ok += bars_regime_ok;
        total_sigs += bars_signal;

        if bars_regime_ok > 0 || bars_signal > 0 {
            println!("{:<12} {:>8.1} {:>8.3} {:>6.2} {:>6.2} {:>6.2} {:>8.3} {:>8} {:>6}",
                date, last_hl, last_sigma, last_adf, max_abs_z, last_entry_z, last_obj,
                bars_regime_ok, bars_signal);
        }
    }
    println!("---");
    println!("Sessions: {} | Total bars in regime: {} | Total signals: {}",
        total_sess, total_ok, total_sigs);
}

fn libm_erf(x: f64) -> f64 {
    // Abramowitz & Stegun approximation
    let t = 1.0 / (1.0 + 0.3275911 * x.abs());
    let poly = t * (0.254829592 + t * (-0.284496736 + t * (1.421413741 + t * (-1.453152027 + t * 1.061405429))));
    let r = 1.0 - poly * (-x * x).exp();
    if x >= 0.0 { r } else { -r }
}
