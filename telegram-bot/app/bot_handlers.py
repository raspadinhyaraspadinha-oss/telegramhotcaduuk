import re
import asyncio

from aiogram import Router
from aiogram.types import CallbackQuery, Message

from .campaign import (
    mark_paid,
    schedule_next_followup,
    send_after_click_flow,
    send_latest_pix_code,
    send_plan_options_message,
    send_start2_preview_video,
    send_post_preview_payment_buttons,
    send_pix_reminder_preview,
    send_start,
    mark_unpaid,
    reset_start_interaction,
    mark_start_interaction,
    has_start_interaction,
    is_paid,
    _format_username,
    START_FOLLOWUP_DELAY_SECONDS,
)
from .tracking import resolve_start_payload, save_utms, send_facebook_event
from .access_delivery import deliver_access_if_needed
from .funnel_metrics import record_funnel_event
from .config import (
    DEFAULT_CLIENT_NAME,
    DEFAULT_CLIENT_EMAIL,
    DEFAULT_CLIENT_PHONE,
    DEFAULT_CLIENT_DOCUMENT,
)
from .log_buffer import dump_file

router = Router()


_START_RE = re.compile(r"^/start(?:@\w+)?(?:\s|$)", re.IGNORECASE)


@router.message(lambda m: bool(m.text and _START_RE.match(m.text.strip())))
async def start(m: Message):
    import asyncio

    # ── PRIORIDADE: enviar primeira mensagem o mais rápido possível ──
    await send_start(m.bot, m.chat.id, m.from_user)

    # ── Tudo abaixo é pós-envio: tracking, Redis, followups (não atrasa UX) ──
    try:
        raw = (m.text or "").strip()
        parts = raw.split(" ", 1)
        payload = parts[1] if len(parts) > 1 else ""
        utms = resolve_start_payload(payload)
        if m.from_user and utms:
            save_utms(m.from_user.id, utms)
            if DEFAULT_CLIENT_EMAIL or DEFAULT_CLIENT_PHONE or DEFAULT_CLIENT_DOCUMENT:
                asyncio.create_task(send_facebook_event(
                    event_name="PageView",
                    event_id=f"pv_{m.from_user.id}_{int(m.date.timestamp())}",
                    amount=None,
                    currency="GBP",
                    customer={
                        "name": DEFAULT_CLIENT_NAME or m.from_user.full_name,
                        "email": DEFAULT_CLIENT_EMAIL or "",
                        "phone": DEFAULT_CLIENT_PHONE or "",
                        "document": DEFAULT_CLIENT_DOCUMENT or "",
                    },
                    utms=utms,
                ))
    except Exception:
        pass

    record_funnel_event("start_received", user_id=m.from_user.id if m.from_user else None)
    if m.from_user:
        reset_start_interaction(m.from_user.id)
        mark_unpaid(m.from_user.id, m.chat.id, reset_cycle=True)
        schedule_next_followup(m.from_user.id, START_FOLLOWUP_DELAY_SECONDS)


@router.message()
async def any_message(m: Message):
    if m.text and m.text.strip().lower() == "/ping":
        await m.answer("pong 🏓")
        return
    # Não responda "Recebido" para comandos (ex: /pixerr)
    if m.text and m.text.strip().startswith("/"):
        return
    await m.answer("Received ✅")


@router.callback_query(lambda c: (c.data or "").startswith("cta:"))
async def on_cta_click(cq: CallbackQuery):
    # Usuário clicou no botão do /start ou de um followup: manda o fluxo do Pix e inicia o timer.
    await cq.answer()
    if not cq.from_user or not cq.message:
        return
    mark_start_interaction(cq.from_user.id)
    
    # Extrai o valor do callback (formato: cta:buy ou cta:buy:18.90)
    parts = (cq.data or "").split(":")
    amount = 14.99  # default UK plan
    action = parts[1] if len(parts) >= 2 else "buy"
    if action == "plans":
        base_amount = amount
        if len(parts) >= 3:
            try:
                base_amount = float(parts[2])
            except ValueError:
                pass
        await send_plan_options_message(cq.bot, cq.message.chat.id, base_amount)
        record_funnel_event("plan_options_clicked", user_id=cq.from_user.id)
        return
    if action != "buy":
        return
    if len(parts) >= 3:
        try:
            amount = float(parts[2])
        except ValueError:
            pass

    username = _format_username(cq.from_user)
    record_funnel_event("cta_buy_clicked", user_id=cq.from_user.id, amount=amount)
    await send_after_click_flow(cq.bot, cq.from_user.id, cq.message.chat.id, amount, username)


@router.callback_query(lambda c: (c.data or "") == "start2:preview:1")
async def on_start2_preview_1(cq: CallbackQuery):
    await cq.answer()
    if not cq.message or not cq.from_user:
        return
    mark_start_interaction(cq.from_user.id)
    record_funnel_event("preview_clicked", user_id=cq.from_user.id)
    username = _format_username(cq.from_user)
    await send_start2_preview_video(cq.bot, cq.message.chat.id, 1, username=username)


@router.callback_query(lambda c: (c.data or "") == "preview:more_before_pay")
async def on_preview_more_before_pay(cq: CallbackQuery):
    await cq.answer()
    if not cq.message or not cq.from_user:
        return
    record_funnel_event("preview_more_clicked", user_id=cq.from_user.id)
    username = _format_username(cq.from_user)
    chat_id = cq.message.chat.id
    bot = cq.bot
    # Send preview immediately, then buttons after short delay in background
    await send_pix_reminder_preview(bot, chat_id)

    async def _delayed_buttons():
        await asyncio.sleep(2.5)
        await send_post_preview_payment_buttons(bot, chat_id, username=username)

    asyncio.create_task(_delayed_buttons())


@router.callback_query(lambda c: (c.data or "") == "pix:show_code")
async def on_pix_show_code(cq: CallbackQuery):
    await cq.answer()
    if not cq.message or not cq.from_user:
        return
    ok = await send_latest_pix_code(cq.bot, cq.from_user.id, cq.message.chat.id)
    if ok:
        record_funnel_event("pix_code_reshown", user_id=cq.from_user.id)
    else:
        await cq.bot.send_message(cq.message.chat.id, "No pending payment found. Please select a plan first.")


@router.callback_query(lambda c: (c.data or "") == "preview:limit_reached")
async def on_preview_limit_reached(cq: CallbackQuery):
    await cq.answer(
        "Preview limit reached, pay for full access.",
        show_alert=True,
    )
    if cq.from_user:
        record_funnel_event("preview_limit_popup", user_id=cq.from_user.id)


@router.callback_query(lambda c: (c.data or "") == "pix:reminder_preview")
async def on_pix_reminder_preview(cq: CallbackQuery):
    await cq.answer(
        "Você já gerou 2 previews disponiveis na nossa plataforma, compre o acesso para ver mais!",
        show_alert=True,
    )
    if cq.from_user:
        record_funnel_event("pix_reminder_preview_clicked", user_id=cq.from_user.id)


@router.callback_query(lambda c: (c.data or "") == "pay:verify")
async def on_verify_payment(cq: CallbackQuery):
    from .pix_payment import check_payment_status

    if not cq.from_user:
        await cq.answer("Erro ao verificar pagamento.", show_alert=True)
        return

    record_funnel_event("verify_clicked", user_id=cq.from_user.id)
    status = await check_payment_status(cq.from_user.id)
    if status == "OK":
        mark_paid(cq.from_user.id)
        await cq.answer("✅ Payment confirmed! Access unlocked.", show_alert=True)
        if cq.message:
            await deliver_access_if_needed(cq.bot, cq.from_user.id, cq.message.chat.id)
        record_funnel_event("payment_confirmed", user_id=cq.from_user.id)
    elif status == "PENDING":
        await cq.answer("⏳ Payment still pending. Please wait a moment.", show_alert=True)
        record_funnel_event("verify_pending", user_id=cq.from_user.id)
    else:
        await cq.answer("❌ Payment not found or expired.", show_alert=True)
        record_funnel_event("verify_failed", user_id=cq.from_user.id, status=str(status))


@router.callback_query(lambda c: (c.data or "") == "pay:mark_paid")
async def admin_mark_paid(cq: CallbackQuery):
    # Atalho manual (útil psara teste): marque como pago e pare os followups.
    await cq.answer("Marcado como pago (teste).", show_alert=False)
    if cq.from_user:
        mark_paid(cq.from_user.id)


@router.message(lambda m: bool(m.text and m.text.strip().lower() == "/pixerr"))
async def debug_pix_error(m: Message):
    """
    Debug rápido: mostra o último erro da Amplopay (salvo no Redis) para esse user_id.
    Use apenas em chat privado para depurar.
    """
    from .redis_client import redis

    if not m.from_user:
        return
    key = f"tg:pixerr:{m.from_user.id}"
    data = redis.hgetall(key)
    if not data:
        await m.answer("Sem erro recente salvo para este usuário.")
        return
    # Limita tamanho pra não estourar limite do Telegram
    body = (data.get("body") or "")[:1500]
    msg = (
        "Último erro Pix (debug):\n"
        f"- where: {data.get('where')}\n"
        f"- status_code: {data.get('status_code')}\n"
        f"- error: {data.get('error')}\n"
        f"- identifier: {data.get('identifier')}\n"
        f"- amount: {data.get('amount')}\n"
        f"- body: {body}\n"
    )
    await m.answer(msg)


@router.message(lambda m: bool(m.text and m.text.strip().lower() == "/logs"))
async def send_logs(m: Message):
    """
    Envia os últimos logs do buffer em um arquivo .txt
    """
    if not m.from_user:
        return
    try:
        log_file = dump_file("bot-logs.txt")
        await m.bot.send_document(m.chat.id, log_file)
    except Exception as e:
        await m.answer(f"Erro ao enviar logs: {e}")


@router.message(lambda m: bool(m.text and m.text.strip().lower() == "/logs30"))
async def send_logs_30(m: Message):
    """
    Envia os últimos 30 logs do buffer em um arquivo .txt
    """
    if not m.from_user:
        return
    try:
        log_file = dump_file("bot-logs-30.txt", limit=30)
        await m.bot.send_document(m.chat.id, log_file)
    except Exception as e:
        await m.answer(f"Erro ao enviar logs: {e}")
