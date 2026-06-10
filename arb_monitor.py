"""
Арбітраж монітор: Gate.io / OKX / MEXC
Запуск: python arb_monitor.py

Порівнює всі пари бірж, знаходить кращу можливість.
Реальний спред: (sell_bid - buy_ask) / buy_ask × 100

Надсилає Telegram алерт коли:
- Реальний спред > MIN_SPREAD_PCT
- Глибина стакану > MIN_DEPTH_USD
"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from itertools import combinations

import httpx
import config

# ─── Налаштування ──────────────────────────────────────────────────────────
COINS          = ["SUI", "SEI", "APT", "TIA", "INJ", "ARB", "OP"]
MIN_NET_PCT    = 0.7    # Мінімальний NET спред після комісій для алерту (%)
MIN_DEPTH_USD  = 1000   # Мінімальна глибина стакану ($)
CHECK_INTERVAL = 300    # Кожні 5 хвилин
SLIPPAGE_PCT   = 0.3    # Допустиме прослизання (%)

# Комісії taker по кожній біржі (%)
# Gate.io: 0.2% стандарт (0.1% з GT токеном)
# OKX:     0.1% taker
# MEXC:    0.1% taker
EXCHANGE_FEES = {
    "gate": 0.20,
    "okx":  0.10,
    "mexc": 0.10,
}

BOT_TOKEN = config.TELEGRAM_BOT_TOKEN
CHAT_ID   = config.ADMIN_CHAT_ID

# ─── Дані ──────────────────────────────────────────────────────────────────
@dataclass
class SpreadResult:
    coin: str
    buy_exchange: str        # де купуємо
    sell_exchange: str       # де продаємо
    buy_ask: float           # ціна по якій купуємо (ask)
    sell_bid: float          # ціна по якій продаємо (bid)
    gross_spread_pct: float  # спред до комісій: (sell_bid - buy_ask) / buy_ask × 100
    net_spread_pct: float    # спред після комісій (реальний прибуток %)
    buy_fee_pct: float       # комісія біржі покупки (%)
    sell_fee_pct: float      # комісія біржі продажу (%)
    buy_depth_usd: float
    sell_depth_usd: float
    max_trade_usd: float
    net_profit_usd: float    # прибуток після комісій ($)
    all_prices: dict = field(default_factory=dict)

    @property
    def spread_pct(self) -> float:
        """Для сумісності — повертає net спред."""
        return self.net_spread_pct

    @property
    def is_opportunity(self) -> bool:
        return self.net_spread_pct >= MIN_NET_PCT and self.max_trade_usd >= MIN_DEPTH_USD

    @property
    def profit_usd(self) -> float:
        return self.net_profit_usd

    @property
    def price_str(self) -> str:
        """bid/ask для відображення"""
        buy_p  = self.all_prices.get(self.buy_exchange, {})
        sell_p = self.all_prices.get(self.sell_exchange, {})
        b_str  = f"{buy_p.get('bid',0):.4f}/{buy_p.get('ask',0):.4f}" if buy_p else "—"
        s_str  = f"{sell_p.get('bid',0):.4f}/{sell_p.get('ask',0):.4f}" if sell_p else "—"
        return f"{self.buy_exchange.upper()} {b_str}  →  {self.sell_exchange.upper()} {s_str}"


# ─── API Gate.io ────────────────────────────────────────────────────────────
async def get_gate(client: httpx.AsyncClient, coin: str) -> dict | None:
    try:
        r = await client.get(
            "https://api.gateio.ws/api/v4/spot/order_book",
            params={"currency_pair": f"{coin}_USDT", "limit": 20},
            timeout=5
        )
        data = r.json()
        asks = [(float(p), float(a)) for p, a in data.get("asks", [])]
        bids = [(float(p), float(a)) for p, a in data.get("bids", [])]
        if not asks or not bids:
            return None
        best_ask = asks[0][0]
        best_bid = bids[0][0]
        ask_depth = sum(p*a for p,a in asks if (p-best_ask)/best_ask <= SLIPPAGE_PCT/100)
        bid_depth = sum(p*a for p,a in bids if (best_bid-p)/best_bid <= SLIPPAGE_PCT/100)
        return {"bid": best_bid, "ask": best_ask, "ask_depth": ask_depth, "bid_depth": bid_depth}
    except Exception:
        return None

# ─── API OKX ────────────────────────────────────────────────────────────────
async def get_okx(client: httpx.AsyncClient, coin: str) -> dict | None:
    try:
        r = await client.get(
            "https://www.okx.com/api/v5/market/books",
            params={"instId": f"{coin}-USDT", "sz": "20"},
            timeout=5
        )
        data = r.json()
        if data.get("code") != "0":
            return None
        book = data["data"][0]
        asks = [(float(p[0]), float(p[1])) for p in book.get("asks", [])]
        bids = [(float(p[0]), float(p[1])) for p in book.get("bids", [])]
        if not asks or not bids:
            return None
        best_ask = asks[0][0]
        best_bid = bids[0][0]
        ask_depth = sum(p*a for p,a in asks if (p-best_ask)/best_ask <= SLIPPAGE_PCT/100)
        bid_depth = sum(p*a for p,a in bids if (best_bid-p)/best_bid <= SLIPPAGE_PCT/100)
        return {"bid": best_bid, "ask": best_ask, "ask_depth": ask_depth, "bid_depth": bid_depth}
    except Exception:
        return None

# ─── API MEXC ────────────────────────────────────────────────────────────────
async def get_mexc(client: httpx.AsyncClient, coin: str) -> dict | None:
    try:
        r = await client.get(
            "https://api.mexc.com/api/v3/depth",
            params={"symbol": f"{coin}USDT", "limit": 20},
            timeout=5
        )
        data = r.json()
        asks = [(float(p[0]), float(p[1])) for p in data.get("asks", [])]
        bids = [(float(p[0]), float(p[1])) for p in data.get("bids", [])]
        if not asks or not bids:
            return None
        best_ask = asks[0][0]
        best_bid = bids[0][0]
        ask_depth = sum(p*a for p,a in asks if (p-best_ask)/best_ask <= SLIPPAGE_PCT/100)
        bid_depth = sum(p*a for p,a in bids if (best_bid-p)/best_bid <= SLIPPAGE_PCT/100)
        return {"bid": best_bid, "ask": best_ask, "ask_depth": ask_depth, "bid_depth": bid_depth}
    except Exception:
        return None

# Словник: назва → функція
EXCHANGE_FETCHERS = {
    "gate": get_gate,
    "okx":  get_okx,
    "mexc": get_mexc,
}

# ─── Розрахунок кращої пари ─────────────────────────────────────────────────
def best_spread_for_coin(coin: str, prices: dict) -> SpreadResult | None:
    """
    Порівнює всі комбінації бірж з урахуванням комісій.

    Gross спред: (sell_bid - buy_ask) / buy_ask × 100
    Net спред:   враховує taker комісію обох бірж
      buy_cost     = buy_ask  × (1 + buy_fee/100)
      sell_revenue = sell_bid × (1 - sell_fee/100)
      net_spread   = (sell_revenue - buy_cost) / buy_cost × 100

    Повертає найкращу можливість (max net_spread).
    """
    available = {ex: p for ex, p in prices.items() if p is not None}
    if len(available) < 2:
        return None

    best: SpreadResult | None = None

    for buy_ex, sell_ex in combinations(available.keys(), 2):
        for b_ex, s_ex in [(buy_ex, sell_ex), (sell_ex, buy_ex)]:
            b = available[b_ex]
            s = available[s_ex]

            buy_fee  = EXCHANGE_FEES.get(b_ex, 0.20)
            sell_fee = EXCHANGE_FEES.get(s_ex, 0.20)

            gross_spread = (s["bid"] - b["ask"]) / b["ask"] * 100

            # Реальна вартість покупки і виручка від продажу
            buy_cost     = b["ask"]  * (1 + buy_fee  / 100)
            sell_revenue = s["bid"]  * (1 - sell_fee / 100)
            net_spread   = (sell_revenue - buy_cost) / buy_cost * 100

            max_trade  = min(b["ask_depth"], s["bid_depth"]) * 0.3
            net_profit = max_trade * net_spread / 100 if net_spread > 0 else 0

            result = SpreadResult(
                coin            = coin,
                buy_exchange    = b_ex,
                sell_exchange   = s_ex,
                buy_ask         = b["ask"],
                sell_bid        = s["bid"],
                gross_spread_pct= gross_spread,
                net_spread_pct  = net_spread,
                buy_fee_pct     = buy_fee,
                sell_fee_pct    = sell_fee,
                buy_depth_usd   = b["ask_depth"],
                sell_depth_usd  = s["bid_depth"],
                max_trade_usd   = max_trade,
                net_profit_usd  = net_profit,
                all_prices      = available,
            )

            if best is None or result.net_spread_pct > best.net_spread_pct:
                best = result

    return best

# ─── Telegram ───────────────────────────────────────────────────────────────
async def send_alert(client: httpx.AsyncClient, opportunities: list[SpreadResult]):
    if not BOT_TOKEN or CHAT_ID == 0:
        print("⚠️  Встанови TELEGRAM_BOT_TOKEN і ADMIN_CHAT_ID в .env")
        return

    lines = ["🚨 <b>АРБІТРАЖ МОЖЛИВІСТЬ</b>\n"]

    for r in opportunities:
        total_fee = r.buy_fee_pct + r.sell_fee_pct
        lines += [
            f"━━━━━━━━━━━━━━━━",
            f"🪙 <b>{r.coin}/USDT</b>",
            f"   Gross спред: <code>{r.gross_spread_pct:+.2f}%</code>",
            f"   Комісії:     <code>-{total_fee:.2f}%</code> ({r.buy_exchange.upper()} {r.buy_fee_pct:.2f}% + {r.sell_exchange.upper()} {r.sell_fee_pct:.2f}%)",
            f"   NET прибуток: <b><code>{r.net_spread_pct:+.2f}%</code></b>",
            f"",
            f"   🟢 Купи  на <b>{r.buy_exchange.upper()}</b>  @ <code>${r.buy_ask:.4f}</code> (ask)",
            f"   🔴 Продай на <b>{r.sell_exchange.upper()}</b> @ <code>${r.sell_bid:.4f}</code> (bid)",
            f"",
        ]
        for ex, p in sorted(r.all_prices.items()):
            if p:
                lines.append(f"   {ex.upper():<6} bid/ask: <code>{p['bid']:.4f}/{p['ask']:.4f}</code>")
        lines += [
            f"",
            f"   Угода:       <code>${r.max_trade_usd:,.0f}</code>",
            f"   NET прибуток: <b>${r.net_profit_usd:.1f}</b>",
        ]

    lines.append(f"\n🕐 {time.strftime('%H:%M:%S')}")

    try:
        await client.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": "\n".join(lines), "parse_mode": "HTML"},
            timeout=10
        )
        print(f"✅ Алерт: {[r.coin for r in opportunities]}")
    except Exception as e:
        print(f"❌ Telegram: {e}")

# ─── Головний цикл ──────────────────────────────────────────────────────────
async def check_once(client: httpx.AsyncClient, quiet: bool = False) -> list[SpreadResult]:
    # Паралельно отримуємо ціни з усіх бірж для всіх монет
    all_tasks = {
        ex: [fn(client, coin) for coin in COINS]
        for ex, fn in EXCHANGE_FETCHERS.items()
    }
    gathered = await asyncio.gather(
        *[asyncio.gather(*tasks) for tasks in all_tasks.values()]
    )
    all_results = {
        ex: list(res)
        for ex, res in zip(all_tasks.keys(), gathered)
    }

    results = []
    opps    = []

    for i, coin in enumerate(COINS):
        prices = {ex: all_results[ex][i] for ex in EXCHANGE_FETCHERS}
        r = best_spread_for_coin(coin, prices)
        if r:
            results.append(r)
            if r.is_opportunity:
                opps.append(r)

    if not quiet:
        print(f"\n{'─'*90}")
        print(f"{'Монета':<6} {'Купити':>8} {'ask':>8} {'Продати':>8} {'bid':>8} {'Gross':>7} {'Fees':>6} {'NET':>7} {'MAX$':>8} {'$NET':>7}")
        print(f"{'─'*90}")
        for r in sorted(results, key=lambda x: x.net_spread_pct, reverse=True):
            marker = " 🚨" if r.is_opportunity else ""
            fees   = r.buy_fee_pct + r.sell_fee_pct
            print(
                f"{r.coin:<6} "
                f"{r.buy_exchange.upper():>8} "
                f"${r.buy_ask:>7.4f} "
                f"{r.sell_exchange.upper():>8} "
                f"${r.sell_bid:>7.4f} "
                f"{r.gross_spread_pct:>+6.2f}% "
                f"-{fees:>4.2f}% "
                f"{r.net_spread_pct:>+6.2f}% "
                f"${r.max_trade_usd:>7,.0f} "
                f"${r.net_profit_usd:>6.2f}"
                f"{marker}"
            )
        print(f"{'─'*90}")
        print(f"🕐 {time.strftime('%H:%M:%S')}  |  NET поріг: >{MIN_NET_PCT}%  |  Можливостей: {len(opps)}")

    return opps, results


async def main():
    print("🤖 Арбітраж монітор запущено")
    print(f"   Біржі:    {', '.join(EXCHANGE_FETCHERS.keys()).upper()}")
    print(f"   Монети:   {', '.join(COINS)}")
    print(f"   Поріг:    NET спред >{MIN_NET_PCT}% (після комісій) + глибина >${MIN_DEPTH_USD}")
    print(f"   Комісії:  Gate {EXCHANGE_FEES['gate']}% | OKX {EXCHANGE_FEES['okx']}% | MEXC {EXCHANGE_FEES['mexc']}%\n")

    async with httpx.AsyncClient() as client:
        while True:
            try:
                opps, _ = await check_once(client)
                if opps:
                    await send_alert(client, opps)
            except Exception as e:
                print(f"❌ Помилка: {e}")
            await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
