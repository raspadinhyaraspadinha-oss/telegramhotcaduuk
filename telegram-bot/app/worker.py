import asyncio
import json

from aiogram import Bot, Dispatcher
from aiogram.types import Update

from .config import (
    BOT_TOKEN,
    REDIS_URL,
    QUEUE_KEY,
    BASE_URL,
    WEBHOOK_SECRET,
    WORKER_MAX_CONCURRENT_UPDATES,
    WORKER_QUEUE_BLPOP_TIMEOUT,
)
from .bot_handlers import router
from .campaign import campaign_due_loop
from .redis_client import redis
from .pix_payment import PIX_PENDING_SET, check_payment_status
from .campaign import mark_paid
from .tracking import get_utms, send_facebook_event, send_to_utmify_order, process_utmify_retries
from .config import (
    DEFAULT_CLIENT_DOCUMENT,
    DEFAULT_CLIENT_EMAIL,
    DEFAULT_CLIENT_NAME,
    DEFAULT_CLIENT_PHONE,
)
from .log_buffer import log
from .access_delivery import deliver_access_if_needed
from .funnel_metrics import record_funnel_event


async def setup_webhook(bot: Bot):
    # garante webhook apontando pro serviço web (BASE_URL do webhook service)
    if BASE_URL:
        await bot.set_webhook(
            url=f"{BASE_URL}/telegram/webhook",
            secret_token=WEBHOOK_SECRET,
            drop_pending_updates=False,
        )


async def run_worker():
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    # configura webhook (uma vez por boot)
    await setup_webhook(bot)

    # loop em background para followups (5 em 5 min) via Redis
    asyncio.create_task(campaign_due_loop(bot))

    # loop de polling para pagamentos (fallback caso webhook falhe)
    async def poll_payments():
        poll_sem = asyncio.Semaphore(10)

        async def process_pending_user(uid: str):
            async with poll_sem:
                try:
                    user_id = int(uid)
                except ValueError:
                    redis.srem(PIX_PENDING_SET, uid)
                    return
                status = await check_payment_status(user_id)
                if status == "OK":
                    # mark paid & stop followups
                    mark_paid(user_id)
                    redis.srem(PIX_PENDING_SET, uid)
                    # notify user
                    chat_id = redis.hget(f"tg:user:{user_id}", "chat_id")
                    if chat_id:
                        await deliver_access_if_needed(bot, user_id, int(chat_id), resend_if_sent=False)
                    record_funnel_event("payment_confirmed", user_id=user_id)
                    # tracking paid (if webhook failed)
                    try:
                        utms = get_utms(user_id)
                        amount_str = redis.hget(f"tg:pix:{user_id}", "amount") or "0"
                        amount = float(amount_str)
                        identifier = redis.hget(f"tg:pix:{user_id}", "identifier") or ""
                        await send_to_utmify_order(
                            order_id=identifier,
                            status="paid",
                            amount=amount,
                            customer={
                                "name": DEFAULT_CLIENT_NAME or "",
                                "email": DEFAULT_CLIENT_EMAIL or "",
                                "phone": DEFAULT_CLIENT_PHONE or "",
                                "document": DEFAULT_CLIENT_DOCUMENT or "",
                            },
                            utms=utms,
                            platform="Telegram-UK",
                            payment_method="stripe_card",
                        )
                        await send_facebook_event(
                            event_name="Purchase",
                            event_id=identifier,
                            amount=amount,
                            currency="GBP",
                            customer={
                                "name": DEFAULT_CLIENT_NAME or "",
                                "email": DEFAULT_CLIENT_EMAIL or "",
                                "phone": DEFAULT_CLIENT_PHONE or "",
                                "document": DEFAULT_CLIENT_DOCUMENT or "",
                            },
                            utms=utms,
                        )
                        log("[POLL] paid tracking sent", {"user_id": user_id, "identifier": identifier})
                    except Exception as e:
                        log("[POLL] tracking erro", type(e).__name__, str(e))
                elif status and status not in ("PENDING", "WAITING_PAYMENT"):
                    # status terminal (failed/canceled/expired): remove da fila pendente
                    redis.srem(PIX_PENDING_SET, uid)
                    record_funnel_event("payment_failed", user_id=user_id, gateway_status=str(status))

        while True:
            try:
                user_ids = list(redis.smembers(PIX_PENDING_SET))[:50]
                if not user_ids:
                    await asyncio.sleep(15)
                    continue
                await asyncio.gather(*(process_pending_user(uid) for uid in user_ids))
                await asyncio.sleep(20)
            except Exception as e:
                log("[POLL] erro", type(e).__name__, str(e))
                await asyncio.sleep(20)

    asyncio.create_task(poll_payments())

    # loop de retry para UTMify (fallback em caso de timeout)
    async def retry_utmify():
        while True:
            try:
                await process_utmify_retries()
            except Exception as e:
                log("[UTMIFY] RETRY LOOP ERRO", type(e).__name__, str(e))
            await asyncio.sleep(30)

    asyncio.create_task(retry_utmify())

    update_sem = asyncio.Semaphore(max(1, WORKER_MAX_CONCURRENT_UPDATES))
    in_flight: set[asyncio.Task] = set()

    async def process_update(raw: str):
        try:
            data = json.loads(raw)
            update = Update.model_validate(data)
            await dp.feed_update(bot, update)
        except Exception as e:
            log("[WORKER] update erro", type(e).__name__, str(e))
        finally:
            update_sem.release()

    while True:
        # BLPOP síncrono em thread para não travar event loop.
        item = await asyncio.to_thread(redis.blpop, QUEUE_KEY, WORKER_QUEUE_BLPOP_TIMEOUT)
        if not item:
            await asyncio.sleep(0.01)
            continue

        _, raw = item
        # Backpressure real: só cria nova task quando há slot livre.
        await update_sem.acquire()
        task = asyncio.create_task(process_update(raw))
        in_flight.add(task)
        task.add_done_callback(in_flight.discard)


if __name__ == "__main__":
    asyncio.run(run_worker())
