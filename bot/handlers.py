"""
Обробники команд Telegram бота.
"""
import time
import asyncio
import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

import config
from db.database import get_db
from db.queries import get_or_create_user, get_user, update_user_threshold, update_user_alerts
from engine.price_cache import price_cache
from engine.history_buffer import history_buffer
from engine.market import mid_price, trend, volatility, advice_price, sparkline
from utils.logger import get_logger
from arb_monitor import COINS, EXCHANGE_FETCHERS, EXCHANGE_FEES, MIN_NET_PCT, MIN_DEPTH_USD, best_spread_for_coin

logger = get_logger(__name__)

BOT_START_TIME = time.time()
DISCLAIMER = "\n\n⚠️ <i>Тільки інформація. Не фінансова порада.</i>"


# ─── /start ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db = await get_db()
    try:
        existing = await get_user(db, user.id)
        await get_or_create_user(db, user.id, user.username, user.first_name)
    finally:
        await db.close()

    if not existing:
        text = (
            f"👋 Привіт, <b>{user.first_name or 'трейдер'}</b>!\n\n"
            "Я стежу за ринком <b>USDT/UAH</b> на Binance P2P та Bybit P2P "
            "і оновлюю дані кожні 30 секунд.\n\n"
            "📊 /market — повна картина ринку\n"
            "💡 /advice — яку ціну поставити зараз\n"
            "📈 /trend — куди рухається ринок\n"
            "💱 /price — ціни на платформах\n"
            "⚡ /alerts on — сповіщення при різких змінах\n\n"
            + DISCLAIMER
        )
    else:
        text = "З поверненням! Дані оновлюються кожні 30 сек.\n/market — актуальна картина."

    await update.message.reply_text(text, parse_mode="HTML")


# ─── /help ───────────────────────────────────────────────────────────────────

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 <b>Команди бота</b>\n\n"
        "💱 /price — ціни купівлі/продажу P2P\n"
        "📊 /market — дашборд: ціни + тренд + волатильність\n"
        "💡 /advice — рекомендована ціна для оголошення\n"
        "📈 /trend — напрямок ринку за 30/60 хв\n\n"
        "🔀 /arb — арбітраж Gate/OKX/MEXC прямо зараз\n\n"
        "⚡ /alerts on|off — увімк/вимк сповіщення\n"
        "🎚 /setthreshold 0.8 — поріг зміни для алерту (%)\n\n"
        "📋 /status — стан бота\n"
        "❓ /help — ця довідка"
    )
    await update.message.reply_text(text, parse_mode="HTML")


# ─── /price ──────────────────────────────────────────────────────────────────

async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    asset = config.DEFAULT_ASSET

    b_ask = await price_cache.get("binance_p2p", asset, "ask")
    b_bid = await price_cache.get("binance_p2p", asset, "bid")
    y_ask = await price_cache.get("bybit_p2p", asset, "ask")
    y_bid = await price_cache.get("bybit_p2p", asset, "bid")

    if not any([b_ask, b_bid, y_ask, y_bid]):
        await update.message.reply_text("⏳ Збираю дані... Спробуй за 30 секунд.")
        return

    def fmt(snap, side):
        if snap and snap.is_valid:
            price = snap.ask_price if side == "ask" else snap.bid_price
            return f"{price:.2f} ₴"
        return "N/A"

    valid_asks = [s for s in [b_ask, y_ask] if s and s.is_valid]
    market = mid_price(valid_asks) if valid_asks else 0

    text = (
        f"💱 <b>USDT/UAH</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"🟡 <b>Binance P2P</b>\n"
        f"   Купити:   {fmt(b_ask, 'ask')}\n"
        f"   Продати:  {fmt(b_bid, 'bid')}\n"
        f"   Оголошень: {b_ask.sellers_count if b_ask else '–'}\n\n"
        f"🔵 <b>Bybit P2P</b>\n"
        f"   Купити:   {fmt(y_ask, 'ask')}\n"
        f"   Продати:  {fmt(y_bid, 'bid')}\n"
        f"   Оголошень: {y_ask.sellers_count if y_ask else '–'}\n\n"
        f"📊 Ринкова ціна: <b>{market:.2f} ₴</b>"
        + DISCLAIMER
    )
    await update.message.reply_text(text, parse_mode="HTML")


# ─── /market ─────────────────────────────────────────────────────────────────

async def cmd_market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    asset = config.DEFAULT_ASSET

    valid_asks = await price_cache.get_all(asset)
    if not valid_asks:
        await update.message.reply_text("⏳ Збираю дані... Спробуй за 30 секунд.")
        return

    current_mid = mid_price(valid_asks)
    history = await history_buffer.get(asset)

    # Тренд
    has_trend = await history_buffer.has_enough_data(asset, minutes=30)
    if has_trend:
        trend_pct, trend_dir = trend(current_mid, history, minutes=60)
        trend_icon = "📈" if trend_dir == "up" else ("📉" if trend_dir == "down" else "➡️")
        sign = "+" if trend_pct >= 0 else ""
        trend_line = f"{trend_icon} Тренд (1 год): <b>{sign}{trend_pct:.2f}%</b>"
    else:
        trend_line = "📊 Тренд: збираю дані (потрібно ~30 хв)"

    # Волатильність
    vol = volatility(history, minutes=60) if history else 0.0
    vol_label = "низька" if vol < 0.5 else ("середня" if vol < 1.5 else "висока")

    # Найкращі ціни
    best_buy = min((s.ask_price for s in valid_asks), default=0)
    b_bid = await price_cache.get("binance_p2p", asset, "bid")
    y_bid = await price_cache.get("bybit_p2p", asset, "bid")
    bids = [s.bid_price for s in [b_bid, y_bid] if s and s.is_valid and s.bid_price > 0]
    best_sell = max(bids) if bids else 0

    sellers_total = sum(s.sellers_count for s in valid_asks)

    text = (
        f"📊 <b>РИНКОВИЙ ДАШБОРД USDT/UAH</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💰 <b>Ціни прямо зараз</b>\n"
        f"   Купити:    <code>{best_buy:.2f} ₴</code>\n"
        f"   Продати:   <code>{best_sell:.2f} ₴</code>\n"
        f"   Середній:  <code>{current_mid:.2f} ₴</code>\n\n"
        f"{trend_line}\n\n"
        f"⚡ <b>Активність</b>\n"
        f"   Оголошень: {sellers_total}\n"
        f"   Волатильність: {vol:.2f}% ({vol_label})"
        + DISCLAIMER
    )
    await update.message.reply_text(text, parse_mode="HTML")


# ─── /advice ─────────────────────────────────────────────────────────────────

async def cmd_advice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    asset = config.DEFAULT_ASSET
    valid_asks = await price_cache.get_all(asset)

    if not valid_asks:
        await update.message.reply_text("⏳ Збираю дані... Спробуй за 30 секунд.")
        return

    # Збираємо bid снапшоти теж
    b_bid = await price_cache.get("binance_p2p", asset, "bid")
    y_bid = await price_cache.get("bybit_p2p", asset, "bid")
    all_snaps = valid_asks + [s for s in [b_bid, y_bid] if s and s.bid_price > 0]

    price_sell = advice_price(all_snaps, side="sell", offset_pct=0.1)
    price_buy = advice_price(all_snaps, side="buy", offset_pct=0.1)
    best_competitor = min((s.ask_price for s in valid_asks), default=0)

    history = await history_buffer.get(asset)
    has_trend = await history_buffer.has_enough_data(asset, minutes=30)
    if has_trend:
        _, direction = trend(mid_price(valid_asks), history, minutes=60)
        trend_advice = {
            "up": "\n⚠️ Ринок зростає — не поспішай продавати.",
            "down": "\n⚠️ Ринок падає — не затягуй з продажем.",
            "flat": "",
        }.get(direction, "")
    else:
        trend_advice = ""

    text = (
        f"💡 <b>РЕКОМЕНДАЦІЯ ДЛЯ ОГОЛОШЕННЯ</b>\n\n"
        f"Найдешевший конкурент: <code>{best_competitor:.2f} ₴</code>\n\n"
        f"✅ <b>Хочеш ПРОДАТИ USDT:</b>\n"
        f"   Постав ціну: <code>{price_sell:.2f} ₴</code>\n"
        f"   Будеш першим у списку продавців\n\n"
        f"✅ <b>Хочеш КУПИТИ USDT (оголошення покупця):</b>\n"
        f"   Постав ціну: <code>{price_buy:.2f} ₴</code>\n"
        f"   Продавці побачать тебе першим"
        f"{trend_advice}"
        + DISCLAIMER
    )
    await update.message.reply_text(text, parse_mode="HTML")


# ─── /trend ──────────────────────────────────────────────────────────────────

async def cmd_trend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    asset = config.DEFAULT_ASSET
    history = await history_buffer.get(asset)
    valid_asks = await price_cache.get_all(asset)

    if not valid_asks:
        await update.message.reply_text("⏳ Збираю дані... Спробуй за 30 секунд.")
        return

    current_mid = mid_price(valid_asks)

    if not await history_buffer.has_enough_data(asset, minutes=30):
        await update.message.reply_text(
            "⏳ Збираю дані для тренду...\n"
            f"Поточна ціна: {current_mid:.2f} ₴\n"
            "Тренд буде доступний через ~30 хв після запуску."
        )
        return

    t30, d30 = trend(current_mid, history, minutes=30)
    t60, d60 = trend(current_mid, history, minutes=60)
    vol = volatility(history, minutes=60)
    spark = sparkline(history, minutes=60)

    def fmt_trend(pct, d):
        icon = "📈" if d == "up" else ("📉" if d == "down" else "➡️")
        sign = "+" if pct >= 0 else ""
        return f"{icon} {sign}{pct:.2f}%"

    text = (
        f"📈 <b>ТРЕНД USDT/UAH</b>\n\n"
        f"За 30 хвилин: {fmt_trend(t30, d30)}\n"
        f"За 1 годину:  {fmt_trend(t60, d60)}\n\n"
        f"Зміна (остання година):\n"
        f"<code>{spark}</code>\n\n"
        f"Волатильність: {vol:.2f}%"
        + DISCLAIMER
    )
    await update.message.reply_text(text, parse_mode="HTML")


# ─── /alerts ─────────────────────────────────────────────────────────────────

async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args or args[0].lower() not in ("on", "off"):
        await update.message.reply_text("Використання: /alerts on або /alerts off")
        return

    enabled = args[0].lower() == "on"
    db = await get_db()
    try:
        await update_user_alerts(db, update.effective_user.id, enabled)
    finally:
        await db.close()

    status = "увімкнено ✅" if enabled else "вимкнено ❌"
    await update.message.reply_text(f"Сповіщення {status}.")


# ─── /setthreshold ───────────────────────────────────────────────────────────

async def cmd_setthreshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        pct = float(context.args[0].replace(",", "."))
        assert 0.1 <= pct <= 10.0
    except (IndexError, ValueError, AssertionError):
        await update.message.reply_text("Використання: /setthreshold 0.8 (від 0.1 до 10)")
        return

    db = await get_db()
    try:
        await update_user_threshold(db, update.effective_user.id, pct)
    finally:
        await db.close()

    await update.message.reply_text(f"✅ Поріг встановлено: <b>{pct}%</b>", parse_mode="HTML")


# ─── /arb ────────────────────────────────────────────────────────────────────

async def cmd_arb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Перевіряє поточні арбітражні можливості між Gate / OKX / MEXC."""
    msg = await update.message.reply_text("🔍 Перевіряю Gate / OKX / MEXC...")

    async def _fetch():
        async with httpx.AsyncClient() as client:
            all_tasks = {
                ex: [fn(client, c) for c in COINS]
                for ex, fn in EXCHANGE_FETCHERS.items()
            }
            all_results = {
                ex: list(res)
                for ex, res in zip(
                    all_tasks.keys(),
                    await asyncio.gather(*[asyncio.gather(*t) for t in all_tasks.values()])
                )
            }
            results = []
            for i, coin in enumerate(COINS):
                prices = {ex: all_results[ex][i] for ex in EXCHANGE_FETCHERS}
                r = best_spread_for_coin(coin, prices)
                if r:
                    results.append(r)
            return sorted(results, key=lambda x: x.net_spread_pct, reverse=True)

    try:
        spreads = await _fetch()
    except Exception as e:
        await msg.edit_text(f"❌ Помилка: {e}")
        return

    opps = [r for r in spreads if r.is_opportunity]

    lines = ["🔀 <b>Арбітраж Gate / OKX / MEXC</b>\n"]

    if opps:
        lines.append(f"🚨 <b>Знайдено можливостей: {len(opps)}</b>\n")
        for r in opps:
            fees = r.buy_fee_pct + r.sell_fee_pct
            lines += [
                f"━━━━━━━━━━━━━━━━",
                f"🪙 <b>{r.coin}/USDT</b>",
                f"   Gross: <code>{r.gross_spread_pct:+.2f}%</code>  Fees: <code>-{fees:.2f}%</code>  NET: <b><code>{r.net_spread_pct:+.2f}%</code></b>",
                f"   🟢 Купи  <b>{r.buy_exchange.upper()}</b> @ <code>${r.buy_ask:.4f}</code>",
                f"   🔴 Продай <b>{r.sell_exchange.upper()}</b> @ <code>${r.sell_bid:.4f}</code>",
                f"   Угода: <code>${r.max_trade_usd:,.0f}</code>  Прибуток: <b>${r.net_profit_usd:.1f}</b>",
            ]
    else:
        best = spreads[0] if spreads else None
        lines.append("😴 Зараз немає можливостей\n")
        if best:
            lines += [
                f"Найкращий зараз:",
                f"<b>{best.coin}</b> NET <code>{best.net_spread_pct:+.2f}%</code>",
                f"Купи {best.buy_exchange.upper()} ${best.buy_ask:.4f} → Продай {best.sell_exchange.upper()} ${best.sell_bid:.4f}",
            ]

    lines += [
        f"\n━━━━━━━━━━━━━━━━",
        f"<b>Всі монети (NET після комісій):</b>",
    ]
    for r in spreads:
        icon = "🟢" if r.net_spread_pct >= MIN_NET_PCT else ("🟡" if r.net_spread_pct > 0 else "🔴")
        lines.append(
            f"{icon} <b>{r.coin}</b>: {r.net_spread_pct:+.2f}%  "
            f"({r.buy_exchange.upper()}→{r.sell_exchange.upper()})"
        )

    lines.append(f"\n🕐 {time.strftime('%H:%M:%S')} | Поріг: NET >{MIN_NET_PCT}%")

    await msg.edit_text("\n".join(lines), parse_mode="HTML")


# ─── /status ─────────────────────────────────────────────────────────────────

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import time as t
    uptime_sec = int(t.time() - BOT_START_TIME)
    hours, rem = divmod(uptime_sec, 3600)
    minutes, seconds = divmod(rem, 60)

    asset = config.DEFAULT_ASSET
    stale = await price_cache.is_stale(asset)
    last_update = await price_cache.last_updated(asset)
    last_str = (
        f"{int(t.time() - last_update)} сек тому"
        if last_update > 0 else "немає даних"
    )

    history = await history_buffer.get(asset)
    disabled = ", ".join(["binance_p2p", "bybit_p2p"]) or "всі активні"

    text = (
        f"🤖 <b>Статус бота</b>\n\n"
        f"Uptime: {hours}г {minutes}хв {seconds}с\n"
        f"Останнє оновлення: {last_str}\n"
        f"Дані застарілі: {'⚠️ Так' if stale else '✅ Ні'}\n"
        f"Точок в history: {len(history)}\n"
        f"Поллінг: кожні {config.POLLING_INTERVAL_SEC} сек"
    )
    await update.message.reply_text(text, parse_mode="HTML")


# ─── Реєстрація ──────────────────────────────────────────────────────────────

def register_handlers(app: Application):
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("market", cmd_market))
    app.add_handler(CommandHandler("advice", cmd_advice))
    app.add_handler(CommandHandler("trend", cmd_trend))
    app.add_handler(CommandHandler("alerts", cmd_alerts))
    app.add_handler(CommandHandler("setthreshold", cmd_setthreshold))
    app.add_handler(CommandHandler("arb", cmd_arb))
    app.add_handler(CommandHandler("status", cmd_status))
    logger.info("Handlers зареєстровано")
