import time

from aiogram import Bot

from .config import PORTAL_BASE_URL
from .copy import PAID_CONFIRMATION_TEXT
from .portal_access import generate_access_key, save_access_key
from .redis_client import redis

DELIVERY_KEY_PREFIX = "tg:access:delivery:"


def _delivery_key(user_id: int) -> str:
    return f"{DELIVERY_KEY_PREFIX}{user_id}"


def _build_paid_message(portal_link: str, access_key: str) -> str:
    return f"{PAID_CONFIRMATION_TEXT}\n\nAcesse: {portal_link}\nChave: {access_key}"


async def deliver_access_if_needed(
    bot: Bot, user_id: int, chat_id: int | None = None, resend_if_sent: bool = True
) -> tuple[bool, str]:
    """
    Entrega o link/chave de acesso de forma idempotente.
    Returns: (sent_now, access_key)
    """
    if not chat_id:
        raw_chat_id = redis.hget(f"tg:user:{user_id}", "chat_id")
        chat_id = int(raw_chat_id) if raw_chat_id else None
    if not chat_id:
        return (False, "")

    key = _delivery_key(user_id)
    data = redis.hgetall(key) or {}
    access_key = data.get("access_key", "")
    sent = data.get("sent") == "1"

    if not access_key:
        access_key = generate_access_key(user_id)
        save_access_key(access_key, user_id)

    portal_link = f"{PORTAL_BASE_URL}?key={access_key}"
    msg = _build_paid_message(portal_link, access_key)

    # Se já foi enviado antes, só reenvia quando explicitamente solicitado.
    if sent and not resend_if_sent:
        return (False, access_key)

    await bot.send_message(int(chat_id), msg)

    redis.hset(
        key,
        mapping={
            "access_key": access_key,
            "sent": "1",
            "updated_at": str(int(time.time())),
        },
    )
    redis.expire(key, 30 * 24 * 60 * 60)
    return (not sent, access_key)

