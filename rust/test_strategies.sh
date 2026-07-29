#!/usr/bin/env bash
# Compare all strategies: ES vs NQ, scale-out ON vs OFF
# Columns: P&L | Win% | P-val | MC% | AvgPass | Sharpe | Sortino | MktNeut | Alpha
set -uo pipefail

BIN="./target/release/es-bot"
ES_DIR="../es_sessions"
NQ_DIR="../nq_sessions"
BASE_FLAGS="--no-lunch --prop-firm"

STRATEGIES=(
    fade
    trend
    orb
    first_pullback
    regime
    kalman
    exhaust
    opening_drive
    vwap_reclaim
    z_cross
    quiet_fade
    bar_rejection
    delta_fade
)

px()     { echo "$2" | perl -ne "if (/\Q$1\E\s+([+-]?[\d.]+)/)  { print \"\$1\n\"; exit }"; }
pxpct()  { echo "$2" | perl -ne "if (/\Q$1\E\s+([+-]?[\d.]+)%/) { print \"\$1\n\"; exit }"; }
pxalpha(){ echo "$1" | perl -ne "if (/Alpha.*?([+-][\d.]+)\s*$/) { print \"\$1\n\"; exit }"; }
pxmkt()  { echo "$1" | perl -ne "if (/Market r.*\[(NEUTRAL|CORRELATED)\]/) { print \"\$1\n\"; exit }"; }

HDR="%-16s %-5s %10s %7s %8s %8s %10s %8s %8s %-10s %9s"
SEP="$(printf -- '-%.0s' {1..107})"

print_header() {
    printf "\n$HDR\n" \
        "STRATEGY" "SCALE" "TOTAL_PNL" "WIN_RT%" "P-VAL" "MC_PASS%" "AVG_PASS" "SHARPE" "SORTINO" "MKT_NEUT" "ALPHA/DAY"
    echo "$SEP"
}

run_block() {
    local label="$1" data_dir="$2" tick_flag="$3" scale_flag="$4"
    local scale_label; [[ "$scale_flag" == "--no-scale-out" ]] && scale_label="OFF" || scale_label="ON"
    echo ""
    echo "  $label — scale $scale_label"
    echo "$SEP"
    for strat in "${STRATEGIES[@]}"; do
        local out
        out=$("$BIN" backtest "$data_dir" --strategy "$strat" $BASE_FLAGS $tick_flag $scale_flag 2>&1 || true)
        local pnl winrt pval mc avg sharpe sortino mkt alpha
        pnl=$(px      "Total P&L"        "$out")
        winrt=$(pxpct "Win Rate"          "$out")
        pval=$(px     "P-Value (mean>0)"  "$out")
        mc=$(pxpct    "MC Pass Rate"      "$out")
        avg=$(px      "Avg Days to Pass"  "$out")
        sharpe=$(px   "Sharpe (ann.)"     "$out")
        sortino=$(px  "Sortino (ann.)"    "$out")
        mkt=$(pxmkt   "$out")
        alpha=$(pxalpha "$out")
        printf "$HDR\n" \
            "$strat" "$scale_label" \
            "${pnl:--}" "${winrt:--}%" "${pval:--}" "${mc:--}%" \
            "${avg:--}" "${sharpe:--}" "${sortino:--}" \
            "${mkt:-?}" "${alpha:--}"
    done
}

# ── ES ─────────────────────────────────────────────────────────────────────────
print_header
run_block "ES" "$ES_DIR" "--es" ""
run_block "ES" "$ES_DIR" "--es" "--no-scale-out"

# ── NQ ─────────────────────────────────────────────────────────────────────────
print_header
run_block "NQ" "$NQ_DIR" "--nq" ""
run_block "NQ" "$NQ_DIR" "--nq" "--no-scale-out"

echo ""
echo "Flags: $BASE_FLAGS | Scale ON = 2 contracts (close 1 at exit_min_ticks, trail rest to BE)"
echo "P-val:   one-tailed t-test, H0: mean daily P&L = 0  (want ≤ 0.05)"
echo "Sharpe:  annualized daily Sharpe = mean/std × √252"
echo "Sortino: annualized using downside std only"
echo "Alpha:   OLS intercept = expected P&L/day at zero market move"
