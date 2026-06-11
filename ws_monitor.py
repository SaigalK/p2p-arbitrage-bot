"""
WebSocket Price Deviation Monitor + Volatility Tracker
Відслідковує різкі зміни цін між біржами в реальному часі.

Логіка:
1. Слідкує за цінами Binance і Gate через WebSocket
2. Кожні 10 хв перевіряє 24h волатильність всіх монет (Binance REST)
3. Якщо монета зросла/впала >10% за добу — режим "висока волатильність":
   - Поріг девіації знижується з 1.0% до 0.3%
   - Надсилає Telegram: "⚡ SUI +12.5% за 24h — слідкуй за арбітражем"
4. Надсилає алерт коли ціни між біржами відрізняються вище порогу

Запуск: python ws_monitor.py
"""

import asyncio
import json
import time
import logging
from dataclasses import dataclass, field
from collections import deque

import websockets
import httpx
import config
import hot_coins

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ─── Налаштування ────────────────────────────────────────────────────────────
COINS = hot_coins.get_current_coins()  # динамічний список

MIN_DEVIATION_PCT    = 1.0    # % відхилення між біржами (звичайний режим)
HIGH_VOL_DEVIATION   = 0.3    # % відхилення при високій волатильності
ALERT_COOLDOWN       = 120    # сек між алертами по одній монеті
SPIKE_WINDOW_SEC     = 30     # вікно різкого руху
SPIKE_PCT            = 1.5    # % зміни за 30 сек = різкий рух

VOLATILITY_THRESHOLD = 10.0   # % зміни за 24h = висока волатильність
VOLATILITY_CHECK_SEC = 600    # перевіряти волатильність кожні 10 хв

BOT_TOKEN = config.TELEGRAM_BOT_TOKEN
CHAT_ID   = config.ADMIN_CHAT_ID

# ─── Сховище ─────────────────────────────────────────────────────────────────
@dataclass
class CoinState:
    coin: str
    prices: dict = field(default_factory=dict)
    history: dict = field(default_factory=dict)
    last_alert: float = 0.0
    last_logged: float = 0.0
    change_24h: float = 0.0        # % зміна за 24h (з Binance REST)
    high_volatility: bool = False   # чи в режимі підвищеної уваги

    def update(self, exchange: str, price: float):
        self.prices[exchange] = price
        if exchange not in self.history:
            self.history[exchange] = deque(maxlen=200)
        self.history[exchange].append((time.time(), price))

    def deviation(self) -> float:
        prices = [p for p in self.prices.values() if p > 0]
        if len(prices) < 2:
            return 0.0
        return (max(prices) - min(prices)) / min(prices) * 100

    def threshold(self) -> float:
        """Поріг алерту залежить від режиму волатильності."""
        return HIGH_VOL_DEVIATION if self.high_volatility else MIN_DEVIATION_PCT

    def spike_exchange(self) -> tuple:
        best_spike = (None, 0.0)
        for exchange, hist in self.history.items():
            cutoff = time.time() - SPIKE_WINDOW_SEC
            past   = [(ts, p) for ts, p in hist if ts >= cutoff]
            if len(past) < 2:
                continue
            oldest  = past[0][1]
            current = self.prices.get(exchange, 0)
            if oldest > 0 and current > 0:
                pct = abs((current - oldest) / oldest) * 100
                if pct > best_spike[1]:
                    best_spike = (exchange, pct)
        return best_spike

    def cheap_exchange(self) -> str:
        return min(self.prices, key=self.prices.get) if self.prices else ""

    def expensive_exchange(self) -> str:
        return max(self.prices, key=self.prices.get) if self.prices else ""

    def can_alert(self) -> bool:
        return (time.time() - self.last_alert) >= ALERT_COOLDOWN


states: dict[str, CoinState] = {coin: CoinState(coin=coin) for coin in COINS}

# ─── Telegram ────────────────────────────────────────────────────────────────
async def send_telegram(text: str):
    if not BOT_TOKEN or CHAT_ID == 0:
        logger.warning("Telegram не налаштований")
        return
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
    except Exception as e:
        logger.error(f"Telegram помилка: {e}")


# ─── Алерт девіації ──────────────────────────────────────────────────────────
async def check_and_alert(state: CoinState):
    if not state.can_alert():
        return

    deviation = state.deviation()
    if deviation < state.threshold():
        return

    if time.time() - state.last_logged > 30:
        prices_str = " | ".join(f"{ex}: ${p:.4f}" for ex, p in state.prices.items())
        logger.info(f"{state.coin}: {prices_str} → девіація {deviation:.2f}% (поріг {state.threshold():.1f}%)")
        state.last_logged = time.time()

    spike_ex, spike_pct = state.spike_exchange()
    cheap_ex     = state.cheap_exchange()
    expensive_ex = state.expensive_exchange()
    cheap_price  = state.prices.get(cheap_ex, 0)
    exp_price    = state.prices.get(expensive_ex, 0)

    vol_badge = f"⚡ ВИСОКА ВОЛАТИЛЬНІСТЬ ({state.change_24h:+.1f}% за 24h)\n" if state.high_volatility else ""

    if spike_ex:
        direction = f"різкий рух на <b>{spike_ex.upper()}</b> ({spike_pct:+.1f}% за {SPIKE_WINDOW_SEC}с)"
    else:
        direction = "стабільне відхилення між біржами"

    text = (
        f"🚨 <b>{state.coin}/USDT — АРБІТРАЖ</b>\n"
        f"{vol_badge}\n"
        f"📊 Відхилення: <b>{deviation:.2f}%</b>\n"
        f"⚡ Причина: {direction}\n\n"
        f"💰 Ціни:\n"
    )
    for ex, price in sorted(state.prices.items(), key=lambda x: x[1]):
        marker = " ← купи (ask)" if ex == cheap_ex else (" ← продай (bid)" if ex == expensive_ex else "")
        text += f"   {ex.upper()}: <code>${price:.4f}</code>{marker}\n"

    text += (
        f"\n✅ <b>Дія:</b>\n"
        f"   Купи  <b>{cheap_ex.upper()}</b> @ ${cheap_price:.4f} (ask)\n"
        f"   Продай <b>{expensive_ex.upper()}</b> @ ${exp_price:.4f} (bid)\n"
        f"   Прибуток: ~{deviation:.2f}% (до комісій)\n\n"
        f"⏱ Вікно: ~30-60 секунд\n"
        f"🕐 {time.strftime('%H:%M:%S')}"
    )

    await send_telegram(text)
    state.last_alert = time.time()
    logger.info(f"🚨 Алерт: {state.coin} девіація {deviation:.2f}% (поріг {state.threshold():.1f}%)")


# ─── Моніторинг 24h волатильності ────────────────────────────────────────────
async def monitor_volatility():
    """
    Кожні 10 хв перевіряє 24h зміну ціни через Binance REST.
    Якщо монета >10% → режим HIGH VOLATILITY → поріг знижується до 0.3%.
    Надсилає Telegram алерт при вході в режим.
    """
    await asyncio.sleep(30)  # Чекаємо поки WebSocket підключиться

    while True:
        try:
            # Оновлюємо список монет (hot_coins кешує, реально оновлює раз на 15 хв)
            await hot_coins.refresh()
            current_coins = hot_coins.get_current_coins()

            # Додаємо нові монети в states якщо з'явились
            for coin in current_coins:
                if coin not in states:
                    states[coin] = CoinState(coin=coin)
                    logger.info(f"🔥 Нова монета в моніторингу: {coin}")

            symbols = [f"{coin}USDT" for coin in current_coins]
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    "https://api.binance.com/api/v3/ticker/24hr",
                    params={"symbols": json.dumps(symbols)},
                    timeout=10
                )
                tickers = r.json()

            new_alerts = []

            for t in tickers:
                symbol = t.get("symbol", "")
                coin   = symbol.replace("USDT", "")
                change = float(t.get("priceChangePercent", 0))

                if coin not in states:
                    continue

                state         = states[coin]
                was_high_vol  = state.high_volatility
                state.change_24h     = change
                state.high_volatility = abs(change) >= VOLATILITY_THRESHOLD

                # Щойно ввійшла в режим high vol
                if state.high_volatility and not was_high_vol:
                    direction = "🚀 зростає" if change > 0 else "📉 падає"
                    new_alerts.append(
                        f"⚡ <b>{coin}/USDT {direction}</b>\n"
                        f"   24h зміна: <b><code>{change:+.1f}%</code></b>\n"
                        f"   Поріг девіації знижено до {HIGH_VOL_DEVIATION}%"
                    )
                    logger.info(f"⚡ {coin}: ВИСОКА ВОЛАТИЛЬНІСТЬ {change:+.1f}%")

                elif not state.high_volatility and was_high_vol:
                    logger.info(f"✅ {coin}: волатильність нормалізувалась ({change:+.1f}%)")

            if new_alerts:
                text = (
                    "⚡ <b>ВИСОКА ВОЛАТИЛЬНІСТЬ — СЛІДКУЙ ЗА АРБІТРАЖЕМ</b>\n\n"
                    + "\n\n".join(new_alerts)
                    + f"\n\n🕐 {time.strftime('%H:%M:%S')}"
                )
                await send_telegram(text)

            # Лог стану
            vol_info = " | ".join(
                f"{c} {states[c].change_24h:+.1f}%{'⚡' if states[c].high_volatility else ''}"
                for c in COINS
            )
            logger.info(f"📊 24h: {vol_info}")

        except Exception as e:
            logger.error(f"Волатильність помилка: {e}")

        await asyncio.sleep(VOLATILITY_CHECK_SEC)


# ─── WebSocket: Binance ───────────────────────────────────────────────────────
async def binance_ws():
    streams = "/".join(f"{coin.lower()}usdt@miniTicker" for coin in COINS)
    url = f"wss://stream.binance.com:9443/stream?streams={streams}"

    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                logger.info(f"✅ Binance WS підключено ({len(COINS)} монет)")
                async for msg in ws:
                    data   = json.loads(msg)
                    ticker = data.get("data", {})
                    symbol = ticker.get("s", "")
                    price  = float(ticker.get("c", 0))
                    coin   = symbol.replace("USDT", "")
                    if coin in states and price > 0:
                        states[coin].update("binance", price)
                        await check_and_alert(states[coin])
        except Exception as e:
            logger.warning(f"Binance WS помилка: {e}. Перепідключення за 5 сек...")
            await asyncio.sleep(5)


# ─── WebSocket: Gate.io ───────────────────────────────────────────────────────
async def gate_ws():
    url   = "wss://api.gateio.ws/ws/v4/"
    pairs = [f"{coin}_USDT" for coin in COINS]

    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                await ws.send(json.dumps({
                    "time": int(time.time()),
                    "channel": "spot.tickers",
                    "event": "subscribe",
                    "payload": pairs,
                }))
                logger.info(f"✅ Gate.io WS підключено ({len(COINS)} монет)")
                async for msg in ws:
                    data   = json.loads(msg)
                    if data.get("event") != "update":
                        continue
                    result = data.get("result", {})
                    pair   = result.get("currency_pair", "")
                    price  = float(result.get("last", 0))
                    coin   = pair.replace("_USDT", "")
                    if coin in states and price > 0:
                        states[coin].update("gate", price)
                        await check_and_alert(states[coin])
        except Exception as e:
            logger.warning(f"Gate WS помилка: {e}. Перепідключення за 5 сек...")
            await asyncio.sleep(5)


# ─── Таблиця статусу кожні 60 сек ────────────────────────────────────────────
async def status_logger():
    await asyncio.sleep(15)
    while True:
        print(f"\n{'─'*75}")
        print(f"{'Монета':<6} {'Binance':>10} {'Gate':>10} {'Відхил':>8} {'24h':>8}  Режим")
        print(f"{'─'*75}")
        for coin, state in states.items():
            b   = state.prices.get("binance", 0)
            g   = state.prices.get("gate", 0)
            dev = state.deviation()
            b_str  = f"${b:.4f}" if b else "—"
            g_str  = f"${g:.4f}" if g else "—"
            ch_str = f"{state.change_24h:+.1f}%" if state.change_24h else "—"
            thr    = state.threshold()

            if state.high_volatility:
                mode = f"⚡ HIGH VOL (поріг >{thr}%)"
            elif dev >= thr:
                mode = "🚨 МОЖЛИВІСТЬ"
            else:
                mode = f"✅ норма (поріг >{thr}%)"

            print(f"{coin:<6} {b_str:>10} {g_str:>10} {dev:>7.2f}% {ch_str:>7}  {mode}")
        print(f"{'─'*75}")
        print(f"🕐 {time.strftime('%H:%M:%S')} | Норм: >{MIN_DEVIATION_PCT}% | High vol: >{HIGH_VOL_DEVIATION}% | Cooldown: {ALERT_COOLDOWN}s")
        await asyncio.sleep(60)


# ─── Головна функція ─────────────────────────────────────────────────────────
async def main():
    logger.info("🤖 WebSocket монітор + Volatility Tracker запущено")
    logger.info(f"   Монети:           {', '.join(COINS)}")
    logger.info(f"   Поріг норма:      >{MIN_DEVIATION_PCT}%")
    logger.info(f"   Поріг high vol:   >{HIGH_VOL_DEVIATION}%")
    logger.info(f"   Волатильність:    >{VOLATILITY_THRESHOLD}% за 24h → high vol режим")
    logger.info(f"   Перевірка vol:    кожні {VOLATILITY_CHECK_SEC//60} хв")
    logger.info(f"   Telegram: chat_id={CHAT_ID}")

    await asyncio.gather(
        binance_ws(),
        gate_ws(),
        monitor_volatility(),
        status_logger(),
    )


if __name__ == "__main__":
    asyncio.run(main())
