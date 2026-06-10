"""Відправка повідомлень у Telegram."""
import asyncio
from telegram import Bot
from telegram.error import Forbidden, RetryAfter, NetworkError
from config import TELEGRAM_BOT_TOKEN
from utils.logger import get_logger

logger = get_logger(__name__)

_bot = Bot(token=TELEGRAM_BOT_TOKEN)


async def safe_send(chat_id: int, text: str, db=None) -> str | None:
    """Надіслати повідомлення з обробкою помилок. Повертає текст або None."""
    try:
        await _bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        return text
    except Forbidden:
        logger.warning(f"Юзер {chat_id} заблокував бота — вимикаємо алерти")
        if db:
            from db.queries import update_user_alerts
            await update_user_alerts(db, telegram_id=chat_id, enabled=False)
        return None
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after)
        await _bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        return text
    except NetworkError as e:
        logger.warning(f"NetworkError → {chat_id}: {e}")
        return None


async def send_spike_alert(
    telegram_id: int, asset: str, direction: str,
    change_pct: float, price_before: float, price_after: float,
) -> str | None:
    arrow = "📈" if direction == "SPIKE_UP" else "📉"
    sign = "+" if direction == "SPIKE_UP" else "-"
    delta = abs(price_after - price_before)

    text = (
        f"⚡ <b>РІЗКА ЗМІНА РИНКУ</b>\n\n"
        f"{arrow} {asset}/UAH змінився на <b>{sign}{change_pct:.2f}%</b>\n\n"
        f"Було:   <code>{price_before:.2f} ₴</code>\n"
        f"Стало:  <code>{price_after:.2f} ₴</code>\n"
        f"Зміна:  <code>{sign}{delta:.2f} ₴</code>\n\n"
        f"/market — детальна картина\n"
        f"/advice — рекомендована ціна для оголошення"
    )
    return await safe_send(telegram_id, text)
