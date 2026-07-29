use std::io::{self, Write};

pub struct MenuSelection {
    pub use_nq: bool,
}

pub fn run_menu() -> io::Result<Option<MenuSelection>> {
    println!();
    println!("  ES-Bot  ·  Select Instrument");
    println!("  ─────────────────────────────────────────────────────");
    println!("  [1]  ES  VwapReclaim    p=0.006  Sharpe 5.2  MC 91%");
    println!("  [2]  NQ  FirstPullback  p=0.031  Sharpe 4.7  MC 81%");
    println!();
    print!("  Choice [1/2, Enter=ES, q=quit]: ");
    io::stdout().flush()?;

    let mut input = String::new();
    io::stdin().read_line(&mut input)?;
    match input.trim() {
        "2" => Ok(Some(MenuSelection { use_nq: true })),
        "q" | "Q" => Ok(None),
        _ => Ok(Some(MenuSelection { use_nq: false })),
    }
}

pub fn select_account(accounts: &[(String, i64)]) -> io::Result<Option<usize>> {
    if accounts.is_empty() {
        return Ok(None);
    }
    println!();
    println!("  ES-Bot  ·  Select Account");
    println!("  ──────────────────────────");
    for (i, (name, id)) in accounts.iter().enumerate() {
        println!("  [{}]  {}  (id={})", i + 1, name, id);
    }
    println!();
    print!("  Choice [1-{}, Enter=1, q=quit]: ", accounts.len());
    io::stdout().flush()?;

    let mut input = String::new();
    io::stdin().read_line(&mut input)?;
    let t = input.trim();
    if t == "q" || t == "Q" { return Ok(None); }
    if t.is_empty() { return Ok(Some(0)); }
    if let Ok(n) = t.parse::<usize>() {
        if n >= 1 && n <= accounts.len() { return Ok(Some(n - 1)); }
    }
    Ok(Some(0))
}
