"""
AI Solana memecoin PAPER-trading bot.
Simulated money only: no wallet, no private keys, no real swaps.

Setup:
    pip install requests anthropic      # anthropic is optional
    export ANTHROPIC_API_KEY=...        # optional: enables the AI decision layer
    python solana_paper_bot.py

Without an API key it falls back to a simple rule-based scorer.
Not financial advice. Memecoins are extremely risky.
"""
import json
import os
import time

import requests

DEX = "https://api.dexscreener.com"
RUGCHECK = "https://api.rugcheck.xyz/v1/tokens/{}/report/summary"
STATE_FILE = "paper_state.json"

CFG = {
    "start_balance": 1000.0,   # paper USD
    "position_usd": 50.0,      # size per trade
    "max_positions": 5,
    "take_profit": 0.50,       # +50%
    "stop_loss": -0.25,        # -25%
    "max_hold_secs": 2 * 3600, # time stop
    "slippage": 0.03,          # simulated 3% each side (fees + slippage)
    "min_liquidity": 15_000,   # USD
    "min_vol_1h": 5_000,       # USD
    "min_age_min": 10,
    "max_age_hours": 24,
    "max_rug_score": 30,       # RugCheck normalised score; lower = safer (tune this)
    "poll_secs": 60,
}


def get_json(url):
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def candidates():
    """Newest Solana tokens from DexScreener profiles and boosts."""
    mints = []
    for path in ("/token-profiles/latest/v1", "/token-boosts/latest/v1"):
        data = get_json(DEX + path) or []
        mints += [d["tokenAddress"] for d in data if d.get("chainId") == "solana"]
    return list(dict.fromkeys(mints))


def best_pair(mint):
    data = get_json(f"{DEX}/latest/dex/tokens/{mint}")
    pairs = [p for p in (data or {}).get("pairs") or [] if p.get("chainId") == "solana"]
    if not pairs:
        return None
    return max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)


def passes_filters(p):
    liq = (p.get("liquidity") or {}).get("usd") or 0
    vol1h = (p.get("volume") or {}).get("h1") or 0
    created = p.get("pairCreatedAt")
    if not created:
        return False
    age_min = (time.time() * 1000 - created) / 60000
    return (
        liq >= CFG["min_liquidity"]
        and vol1h >= CFG["min_vol_1h"]
        and CFG["min_age_min"] <= age_min <= CFG["max_age_hours"] * 60
    )


def rug_ok(mint):
    """Fails closed: if the check can't run, skip the token."""
    r = get_json(RUGCHECK.format(mint))
    score = (r or {}).get("score_normalised")
    return score is not None and score <= CFG["max_rug_score"]


def summarize(p):
    tx = (p.get("txns") or {}).get("h1") or {}
    return {
        "symbol": (p.get("baseToken") or {}).get("symbol"),
        "price_usd": p.get("priceUsd"),
        "liquidity_usd": (p.get("liquidity") or {}).get("usd"),
        "fdv": p.get("fdv"),
        "volume_1h": (p.get("volume") or {}).get("h1"),
        "buys_1h": tx.get("buys"),
        "sells_1h": tx.get("sells"),
        "change_5m": (p.get("priceChange") or {}).get("m5"),
        "change_1h": (p.get("priceChange") or {}).get("h1"),
    }


def rule_decision(s):
    buys, sells = s["buys_1h"] or 0, s["sells_1h"] or 0
    ratio = buys / max(buys + sells, 1)
    ch = s["change_1h"] or 0
    ok = ratio > 0.55 and 0 < ch < 300  # momentum, but don't chase parabolic pumps
    return ok, f"buy ratio {ratio:.2f}, 1h change {ch}%"


def ai_decision(s):
    if not os.getenv("ANTHROPIC_API_KEY"):
        return rule_decision(s)
    try:
        import anthropic

        client = anthropic.Anthropic()
        msg = client.messages.create(
            model="claude-sonnet-5-5",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": (
                    "You are a cautious memecoin analyst. This token already passed "
                    "liquidity and rug-check filters. Decide whether to paper-buy it. "
                    "Skip anything that looks like a late pump, wash trading, or "
                    "one-sided selling. Reply with ONLY JSON: "
                    '{"action":"buy"|"skip","reason":"<short>"}\n\n'
                    + json.dumps(s)
                ),
            }],
        )
        out = json.loads(msg.content[0].text.strip())
        return out["action"] == "buy", out.get("reason", "")
    except Exception:
        return rule_decision(s)


def load():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"cash": CFG["start_balance"], "positions": {}, "closed": [], "seen": []}


def save(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def manage(state):
    for mint, pos in list(state["positions"].items()):
        p = best_pair(mint)
        if not p or not p.get("priceUsd"):
            continue
        price = float(p["priceUsd"])
        pnl = price * (1 - CFG["slippage"]) / pos["entry"] - 1
        pos["last_pnl"] = pnl
        held = time.time() - pos["opened"]
        reason = None
        if pnl >= CFG["take_profit"]:
            reason = "take profit"
        elif pnl <= CFG["stop_loss"]:
            reason = "stop loss"
        elif held > CFG["max_hold_secs"]:
            reason = "time stop"
        if reason:
            state["cash"] += pos["usd"] * (1 + pnl)
            state["closed"].append({"mint": mint, "symbol": pos["symbol"], "pnl": pnl, "reason": reason})
            del state["positions"][mint]
            print(f"SELL {pos['symbol']} {pnl:+.1%} ({reason})")


def scan(state):
    for mint in candidates():
        if len(state["positions"]) >= CFG["max_positions"] or state["cash"] < CFG["position_usd"]:
            return
        if mint in state["positions"] or mint in state["seen"]:
            continue
        state["seen"].append(mint)
        p = best_pair(mint)
        if not p or not p.get("priceUsd") or not passes_filters(p) or not rug_ok(mint):
            continue
        s = summarize(p)
        buy, why = ai_decision(s)
        if not buy:
            print(f"skip {s['symbol']}: {why}")
            continue
        state["cash"] -= CFG["position_usd"]
        state["positions"][mint] = {
            "symbol": s["symbol"],
            "entry": float(p["priceUsd"]) * (1 + CFG["slippage"]),
            "usd": CFG["position_usd"],
            "opened": time.time(),
            "last_pnl": 0.0,
        }
        print(f"BUY {s['symbol']} ${CFG['position_usd']} | {why}")
    state["seen"] = state["seen"][-500:]


def equity(state):
    return state["cash"] + sum(p["usd"] * (1 + p.get("last_pnl", 0)) for p in state["positions"].values())


def main():
    import sys

    state = load()
    if "--once" in sys.argv:  # single cycle, used by GitHub Actions
        manage(state)
        scan(state)
        save(state)
        wins = sum(1 for t in state["closed"] if t["pnl"] > 0)
        print(f"equity ${equity(state):.2f} | open {len(state['positions'])} | closed {len(state['closed'])} (wins {wins})")
        return
    print("Paper trading started. Ctrl+C to stop.")
    while True:
        try:
            manage(state)
            scan(state)
        except Exception as e:
            print("error:", e)
        save(state)
        wins = sum(1 for t in state["closed"] if t["pnl"] > 0)
        print(f"equity ${equity(state):.2f} | open {len(state['positions'])} | closed {len(state['closed'])} (wins {wins})")
        time.sleep(CFG["poll_secs"])


if __name__ == "__main__":
    main()
