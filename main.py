"""
P2P Market Dashboard Bot — точка входу.
Запускає scheduler loop і Telegram bot одночасно.
"""
import asyncio
import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram.ext import ApplicationBuilder, CommandHandler

import config
from collectors.binance_p2p import BinanceP2PCollector
from collectors.bybit_p2p import BybitP2PCollector
from collectors.nbu import get_usd_uah_rate
from db.database import init_db, get_db
from db.queries import save_snapshot, save_api_error, count_recent_errors, cleanup_old_data
from engine.price_cache import price_cache
from engine.history_buffer import history_buffer
from engine.alert_manager import alert_manager
from engine.market import mid_price, spike_detected
from utils.logger import get_logger

logger = get_logger(__name__)

# Колектори
binance = BinanceP2PCollector()
bybit = BybitP2PCollector()

# Лічильники помилок платформ (in-memory)
_error_streak: dict[str, int] = {"binance_p2p": 0, "bybit_p2p": 0}
_disabled_platforms: set[str] = set()


async def collect_and_process():
    """
    Основний цикл: збір даних → кеш → history → алерти.
    Викликається scheduler кожні POLLING_INTERVAL_SEC секунд.
    """
    asset = config.DEFAULT_ASSET
    fiat = config.DEFAULT_FIAT

    # 1. Збираємо дані паралельно
    results = await asyncio.gather(
        binance.fetch(asset, fiat),
        bybit.fetch(asset, fiat),
        return_exceptions=True,
    )

    valid_ask_snaps = []
    db = await get_db()

    try:
        for platform_name, result in zip(["binance_p2p", "bybit_p2p"], results):
            if isinstance(result, Exception):
                logger.error(f"{platform_name} виняток: {result}")
                _error_streak[platform_name] = _error_streak.get(platform_name, 0) + 1
                await save_api_error(db, platform_name, "exception", str(result))
                continue

            ask_snap, bid_snap = result

            # 2. Перевіряємо помилки
            for snap, side in [(ask_snap, "ask"), (bid_snap, "bid")]:
                if snap.error:
                    _error_streak[platform_name] = _error_streak.get(platform_name, 0) + 1
                    await save_api_error(db, platform_name, snap.error, snap.error)
                    if _error_streak[platform_name] >= 3:
                        logger.warning(f"{platform_name}: 3+ помилки підряд, виключаємо")
                        _disabled_platforms.add(platform_name)
                else:
                    _error_streak[platform_name] = 0
                    _disabled_platforms.discard(platform_name)

            if platform_name in _disabled_platforms:
                continue

            # 3. Оновлюємо кеш
            await price_cache.set(ask_snap, "ask")
            await price_cache.set(bid_snap, "bid")

            if ask_snap.is_valid:
                valid_ask_snaps.append(ask_snap)

            # 4. Зберігаємо в БД
            m = mid_price(valid_ask_snaps) if valid_ask_snaps else 0.0
            await save_snapshot(db, ask_snap, m)
            await save_snapshot(db, bid_snap, m)

        # 5. Оновлюємо HistoryBuffer
        if valid_ask_snaps:
            current_mid = mid_price(valid_ask_snaps)
            if current_mid > 0:
                await history_buffer.append(asset, current_mid)

                # 6. Перевіряємо spike для кожного підписника
                history = await history_buffer.get(asset)
                if len(history) >= 2:
                    detected, change_pct = spike_detected(
                        current_mid,
                        history,
                        threshold_pct=config.DEFAULT_SPIKE_THRESHOLD_PCT,
                        window_minutes=config.SPIKE_WINDOW_MINUTES,
                    )
                    if detected:
                        # Беремо ціну з початку вікна (10 хв тому), а не -1 цикл
                        import time as _time
                        cutoff = _time.time() - (config.SPIKE_WINDOW_MINUTES * 60)
                        past = [(ts, p) for ts, p in history if ts >= cutoff]
                        price_before = past[0][1] if past else history[0][1]
                        await _send_spike_alerts(
                            db, asset, change_pct, current_mid, history, price_before
                        )

        logger.debug(
            f"Цикл завершено. Платформ активних: "
            f"{2 - len(_disabled_platforms)}. "
            f"Mid: {mid_price(valid_ask_snaps) if valid_ask_snaps else 'N/A'}"
        )

    finally:
        await db.close()


async def _send_spike_alerts(db, asset, change_pct, current_mid, history, price_before):
    """Надіслати алерти про різку зміну всім підписникам."""
    from db.queries import get_all_alert_users, save_alert
    from bot.notifier import send_spike_alert

    direction = "SPIKE_UP" if current_mid > price_before else "SPIKE_DOWN"
    users = await get_all_alert_users(db)

    for user in users:
        telegram_id = user["telegram_id"]
        threshold = user.get("spike_threshold_pct", config.DEFAULT_SPIKE_THRESHOLD_PCT)

        if change_pct < threshold:
            continue

        can_send = await alert_manager.should_send(
            telegram_id, direction, asset,
            cooldown_sec=config.ALERT_COOLDOWN_SEC,
        )
        if not can_send:
            continue

        msg = await send_spike_alert(
            telegram_id, asset, direction, change_pct, price_before, current_mid
        )
        if msg:
            await alert_manager.mark_sent(telegram_id, direction, asset)
            await save_alert(
                db, user["id"], direction, asset,
                change_pct, price_before, current_mid, msg
            )


async def scheduled_cleanup():
    """Щоночі чистимо старі дані."""
    db = await get_db()
    try:
        await cleanup_old_data(db)
    finally:
        await db.close()


async def main():
    logger.info("Запуск P2P Market Dashboard Bot...")

    # 1. Ініціалізація БД
    await init_db()

    # 2. Відновлення стану після рестарту
    db = await get_db()
    try:
        await alert_manager.restore_from_db(db, cooldown_sec=config.ALERT_COOLDOWN_SEC)
        await history_buffer.restore_from_db(db, asset=config.DEFAULT_ASSET)
    finally:
        await db.close()

    # 3. Перший збір даних одразу
    await collect_and_process()

    # 4. Scheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        collect_and_process,
        trigger="interval",
        seconds=config.POLLING_INTERVAL_SEC,
        id="collect_loop",
    )
    scheduler.add_job(
        scheduled_cleanup,
        trigger="cron",
        hour=3, minute=0,
        id="cleanup",
    )
    scheduler.start()
    logger.info(f"Scheduler запущено: поллінг кожні {config.POLLING_INTERVAL_SEC} сек")

    # 5. Telegram bot
    from bot.handlers import register_handlers

    app = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).build()
    register_handlers(app)

    logger.info("Бот запущено. Очікую команди...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    # Тримаємо живим
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Зупинка бота...")
    finally:
        scheduler.shutdown()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
