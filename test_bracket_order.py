#!/usr/bin/env python3
import os
import time
import argparse
from datetime import datetime, timezone

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

API_BASE = "https://api.topstepx.com/api"


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def pick_account(accounts, explicit_id=None):
    if explicit_id is not None:
        for account in accounts:
            if account.get("id") == explicit_id:
                return account
        raise RuntimeError(f"Account {explicit_id} not found")

    tradable = [account for account in accounts if account.get("canTrade")]
    prac = [account for account in tradable if "PRAC" in account.get("name", "").upper()]
    if prac:
        return prac[0]
    if tradable:
        return tradable[0]
    raise RuntimeError("No tradable account found")


def pick_contract(contracts, explicit_id=None):
    if explicit_id:
        for contract in contracts:
            if contract.get("id") == explicit_id:
                return contract
        raise RuntimeError(f"Contract {explicit_id} not found")

    es = [c for c in contracts if c.get("symbolId") == "F.US.EP"]
    if es:
        return es[0]
    if contracts:
        return contracts[0]
    raise RuntimeError("No contracts returned")


def build_payload(account_id, contract_id, side, size, sl_ticks, tp_ticks):
    side_value = 0 if side.lower() == "buy" else 1

    sl_ticks = max(4, abs(int(sl_ticks)))
    tp_abs = abs(int(tp_ticks))
    if tp_abs == 0:
        raise RuntimeError("tp_ticks must be non-zero")

    tp_directional = tp_abs if side_value == 0 else -tp_abs

    tag = f"BRKT_TEST_{int(time.time())}"

    return {
        "accountId": account_id,
        "contractId": contract_id,
        "type": 2,
        "side": side_value,
        "size": int(size),
        "customTag": tag,
        "stopLossBracket": {
            "ticks": sl_ticks,
            "type": 4,
        },
        "takeProfitBracket": {
            "ticks": tp_directional,
            "type": 1,
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Place a single TopstepX market order with bracket SL/TP")
    parser.add_argument("--side", choices=["buy", "sell"], default="sell")
    parser.add_argument("--size", type=int, default=1)
    parser.add_argument("--sl-ticks", type=int, default=12)
    parser.add_argument("--tp-ticks", type=int, default=24)
    parser.add_argument("--account-id", type=int, default=None)
    parser.add_argument("--contract-id", type=str, default=None)
    parser.add_argument("--live", action="store_true", help="Request live contracts instead of sim")
    parser.add_argument("--dry-run", action="store_true", help="Print payload only")
    args = parser.parse_args()

    username = require_env("PROJECT_X_USERNAME")
    api_key = require_env("PROJECT_X_API_KEY")

    with httpx.Client(timeout=20.0) as client:
        login = client.post(f"{API_BASE}/Auth/loginKey", json={"userName": username, "apiKey": api_key})
        login_data = login.json()
        if not login_data.get("success") or not login_data.get("token"):
            raise RuntimeError(f"Login failed: {login_data}")

        token = login_data["token"]
        headers = {"Authorization": f"Bearer {token}"}

        account_resp = client.post(f"{API_BASE}/Account/search", json={"onlyActiveAccounts": True}, headers=headers)
        accounts = account_resp.json().get("accounts", [])
        account = pick_account(accounts, args.account_id)

        contract_resp = client.post(
            f"{API_BASE}/Contract/available",
            json={"live": bool(args.live)},
            headers=headers,
        )
        contracts = contract_resp.json().get("contracts", [])
        contract = pick_contract(contracts, args.contract_id)

        payload = build_payload(
            account_id=account["id"],
            contract_id=contract["id"],
            side=args.side,
            size=args.size,
            sl_ticks=args.sl_ticks,
            tp_ticks=args.tp_ticks,
        )

        print(f"[{datetime.now(timezone.utc).isoformat()}] Account: {account.get('name')} ({account.get('id')})")
        print(f"[{datetime.now(timezone.utc).isoformat()}] Contract: {contract.get('id')} ({contract.get('symbolId')})")
        print(f"[{datetime.now(timezone.utc).isoformat()}] Payload: {payload}")

        if args.dry_run:
            print("Dry run complete. No order sent.")
            return

        place = client.post(f"{API_BASE}/Order/place", json=payload, headers=headers)
        place_data = place.json()
        print(f"[{datetime.now(timezone.utc).isoformat()}] Response: {place_data}")


if __name__ == "__main__":
    main()
