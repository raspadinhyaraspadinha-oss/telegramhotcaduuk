import time
from datetime import datetime, timedelta
from html import escape as _html_escape
from pathlib import Path
from typing import Iterable, Literal, Optional, TypedDict
import re
from urllib.parse import quote_plus
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import unicodedata
from zoneinfo import ZoneInfo

import base64 as _b64

from aiogram import Bot
from aiogram.types import BufferedInputFile, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from . import copy as C
from .redis_client import redis
from .config import BOT_ID
from .text_utils import (
    collapse_whitespace_one_line,
    sanitize_telegram_export_text,
    truncate,
)
from .log_buffer import log
from .pix_payment import PIX_PENDING_SET
from .funnel_metrics import record_funnel_event

MEDIA_DIR = Path(__file__).resolve().parent / "media"

USER_KEY_PREFIX = "tg:user:"
DUE_ZSET_KEY = "tg:campaign:due"  # score = unix ts, member = user_id (str)
BLOCKED_SET = "tg:user:blocked"
START_INTERACT_PREFIX = "tg:start:interacted:"
PREVIEW_MSGS_PREFIX = "tg:preview_msgs:"

# Followup timing
START_FOLLOWUP_DELAY_SECONDS = 360  # 6 min after /start (single followup)
FOLLOWUP_DELAY_SECONDS = 360  # kept for legacy compat
FOLLOWUP_2_TO_3_DELAY_SECONDS = 120 * 60  # unused now


def _preview_msgs_key(chat_id: int) -> str:
    return f"{PREVIEW_MSGS_PREFIX}{chat_id}"


def save_preview_msg_id(chat_id: int, msg_id: int) -> None:
    """Salva message_id de preview enviada para exclusão futura."""
    try:
        redis.rpush(_preview_msgs_key(chat_id), str(msg_id))
        redis.expire(_preview_msgs_key(chat_id), 3600)  # expira em 1h
    except Exception:
        pass


def _get_and_clear_preview_msg_ids(chat_id: int) -> list:
    """Retorna e limpa todos os message_ids de previews pendentes."""
    key = _preview_msgs_key(chat_id)
    try:
        raw = redis.lrange(key, 0, -1)
        redis.delete(key)
        return [int(x) for x in raw if x]
    except Exception:
        return []


class Step(TypedDict):
    kind: Literal["video", "photo"]
    media_base: str  # basename without extension, stored in app/media/
    text: str
    button_text: str
    amount: float  # valor do plano com desconto


def _user_key(user_id: int) -> str:
    return f"{USER_KEY_PREFIX}{user_id}"


def _start_interact_key(user_id: int) -> str:
    return f"{START_INTERACT_PREFIX}{user_id}"


def _media_candidates(base: str) -> Iterable[Path]:
    # Prefer mp4/jpg/jpeg/png, but accept any extension present.
    preferred = [".mp4", ".mov", ".webm", ".mkv", ".jpg", ".jpeg", ".png"]
    for ext in preferred:
        yield MEDIA_DIR / f"{base}{ext}"
    # fallback: any file starting with base.
    yield from MEDIA_DIR.glob(f"{base}.*")


def resolve_media_path(media_base: str) -> Optional[Path]:
    for p in _media_candidates(media_base):
        if p.exists() and p.is_file():
            return p
    return None


def kb_single(button_text: str, callback_data: str) -> InlineKeyboardMarkup:
    safe_text = truncate(collapse_whitespace_one_line(sanitize_telegram_export_text(button_text)), 64)
    if not safe_text:
        safe_text = "Continuar"
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=safe_text, callback_data=callback_data)]]
    )


def kb_start_primary_offer(base_7_amount: float) -> InlineKeyboardMarkup:
    """
    UK start: plans + preview.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="£14.99 / 7 days access", callback_data="cta:buy:14.99")],
            [InlineKeyboardButton(text="£24.99 / 15 days access", callback_data="cta:buy:24.99")],
            [InlineKeyboardButton(text="£49.99 / lifetime access", callback_data="cta:buy:49.99")],
            [InlineKeyboardButton(text="Watch an exclusive preview 🎬", callback_data="start2:preview:1")],
        ]
    )


def kb_pix_actions() -> InlineKeyboardMarkup:
    return kb_pix_actions_with_code(None)


def _make_copy_button(label: str, copy_text: Optional[str]) -> InlineKeyboardButton:
    """
    Usa botão nativo de copiar (quando suportado), com fallback para callback.
    """
    if copy_text:
        try:
            # aiogram 3.24+ (Bot API com botão nativo de copiar)
            from aiogram.types import CopyTextButton

            return InlineKeyboardButton(text=label, copy_text=CopyTextButton(text=copy_text))
        except Exception:
            # fallback para versões antigas/cliente sem suporte
            pass
    return InlineKeyboardButton(text=label, callback_data="pix:show_code")


def kb_pix_actions_with_code(copy_text: Optional[str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_make_copy_button("📋 Copiar Chave Pix", copy_text)],
            [InlineKeyboardButton(text=_safe_btn_text(C.PIX_VERIFY_BUTTON_TEXT, "Verificar pagamento"), callback_data="pay:verify")],
        ]
    )


def reset_start_interaction(user_id: int) -> None:
    try:
        redis.delete(_start_interact_key(user_id))
    except Exception:
        pass


def mark_start_interaction(user_id: int) -> None:
    try:
        # TTL só para limpeza automática de estado antigo.
        redis.set(_start_interact_key(user_id), "1", ex=1800)
    except Exception:
        pass


def has_start_interaction(user_id: int) -> bool:
    try:
        return bool(redis.get(_start_interact_key(user_id)))
    except Exception:
        return False


AFTER_CLICK_IMAGE = "image_32528"
START2_IMAGE = "video2"
START2_PREVIEW_1_VIDEO = "pvnova"
START2_PREVIEW_2_VIDEO = "video2"
START2_PROOF_IMAGE = "prova"
PIX_REMINDER_PREVIEW_VIDEO = "videopb"


def _safe_btn_text(text: str, fallback: str) -> str:
    safe = truncate(collapse_whitespace_one_line(sanitize_telegram_export_text(text)), 64)
    return safe or fallback


def _q2(amount: float) -> float:
    d = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(d)


def _apply_amount_to_button_copy(template_text: str, amount: float) -> str:
    amount_str = format_brl(amount)
    return re.sub(r"R\$\s*[0-9]+(?:[.,][0-9]{2})", f"R${amount_str}", template_text, count=1)


def _scaled_plan_amounts(base_7_amount: float) -> tuple[float, float, float]:
    if START2_AMOUNT_7 <= 0:
        return (_q2(base_7_amount), _q2(START2_AMOUNT_15), _q2(START2_AMOUNT_30))
    ratio = Decimal(str(base_7_amount)) / Decimal(str(START2_AMOUNT_7))
    a7 = _q2(float((Decimal(str(START2_AMOUNT_7)) * ratio).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)))
    a15 = _q2(float((Decimal(str(START2_AMOUNT_15)) * ratio).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)))
    a30 = _q2(float((Decimal(str(START2_AMOUNT_30)) * ratio).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)))
    return (a7, a15, a30)


def kb_payment_options(base_7_amount: float, include_previews: bool = False) -> InlineKeyboardMarkup:
    a7, a15, a30 = _scaled_plan_amounts(base_7_amount)
    text_7 = _apply_amount_to_button_copy(C.START2_BUTTON_TEXT, a7)
    text_15 = _apply_amount_to_button_copy(C.START2_BUTTON_TEXT_15_DAYS, a15)
    text_30 = _apply_amount_to_button_copy(C.START2_BUTTON_TEXT_30_DAYS, a30)
    rows = [
        [InlineKeyboardButton(text=_safe_btn_text(text_7, "R$19,90 (7 dias de acesso)"), callback_data=f"cta:buy:{a7:.2f}")],
        [InlineKeyboardButton(text=_safe_btn_text(text_15, "R$26,90 (15 dias de acesso)"), callback_data=f"cta:buy:{a15:.2f}")],
        [InlineKeyboardButton(text=_safe_btn_text(text_30, "R$35,90 (30 dias de acesso)"), callback_data=f"cta:buy:{a30:.2f}")],
    ]
    if include_previews:
        rows.append(
            [InlineKeyboardButton(text=_safe_btn_text(C.START2_PREVIEW_1_BUTTON_TEXT, "VER PREVIEW VIDEO PLANO BLACK 1"), callback_data="start2:preview:1")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _post_preview_cta_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="£14.99 / 7 days access", callback_data="cta:buy:14.99")],
            [InlineKeyboardButton(text="£24.99 / 15 days access", callback_data="cta:buy:24.99")],
            [InlineKeyboardButton(text="£49.99 / lifetime access", callback_data="cta:buy:49.99")],
        ]
    )

# --- money helpers (derive amounts from copy.psy button texts) ---

_BRL_RE = re.compile(r"R\$\s*([0-9]+(?:[.,][0-9]{2}))")
_VALOR_RE = re.compile(r"(Valor:\s*R\$\s*)([0-9]+(?:[.,][0-9]{2}))", flags=re.IGNORECASE)


def _to_decimal_amount(raw: str) -> Optional[Decimal]:
    try:
        s = (raw or "").strip().replace(".", "").replace(",", ".")
        d = Decimal(s)
        return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None


def _normalize_unicode_digits(text: str) -> str:
    """
    Converts unicode digits (e.g. 𝟭𝟵, １２３) into ASCII digits so regex/Decimal parsing works.
    Keeps non-digits unchanged.
    """
    out: list[str] = []
    for ch in text or "":
        if ch.isdigit():
            try:
                out.append(str(unicodedata.digit(ch)))
                continue
            except Exception:
                # fallback: keep original
                pass
        out.append(ch)
    return "".join(out)


def extract_amount_from_text(text: str, fallback: float) -> float:
    """
    Extract first BRL amount found in the given (possibly HTML) text.
    Example: "por R$19,90" -> 19.90
    """
    clean = _normalize_unicode_digits(sanitize_telegram_export_text(text))
    m = _BRL_RE.search(clean)
    if not m:
        return float(fallback)
    d = _to_decimal_amount(m.group(1))
    return float(d) if d is not None else float(fallback)


def format_brl(amount: float) -> str:
    d = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    # always "19,90"
    return f"{d:.2f}".replace(".", ",")


def apply_amount_to_plan_text(plan_text: str, amount: float) -> str:
    """
    Replace the amount in the 'Valor: R$xx,yy' line with the selected amount.
    Operates on already-sanitized text.
    """
    return _VALOR_RE.sub(lambda m: m.group(1) + format_brl(amount), plan_text, count=1)


def _deadline_sao_paulo_text() -> str:
    tz = ZoneInfo("America/Sao_Paulo")
    now = datetime.now(tz)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    remain = midnight - now
    hours, rem = divmod(int(remain.total_seconds()), 3600)
    mins, _ = divmod(rem, 60)
    return f"{hours:02d}:{mins:02d}"


def _sao_paulo_datetime_text() -> str:
    tz = ZoneInfo("America/Sao_Paulo")
    now = datetime.now(tz)
    return now.strftime("%d/%m/%Y %H:%M")


def _format_username(user) -> str:
    """Retorna @username ou first_name como fallback."""
    if hasattr(user, 'username') and user.username:
        return f"@{user.username}"
    if hasattr(user, 'first_name') and user.first_name:
        return user.first_name
    return "você"


def _personalize_caption(caption: str, username: str) -> str:
    batch_dt = _sao_paulo_datetime_text()
    batch_info = f"(lote 456 - 92/100 - {batch_dt})"
    return caption.format(username=username, batch_info=batch_info)


def _trust_anchor_text(amount: float) -> str:
    deadline = _deadline_sao_paulo_text()
    return (
        f"🔒 Pagamento 100% seguro via Pix.\n"
        f"✅ Reembolso garantido em caso de erro de liberação.\n"
        f"📈 98 acessos confirmados hoje.\n"
        f"⏱️ Pague nos proximos 5 minutos, 2 vagas já foram compradas na sua frente.\n\n"
        f"Valor selecionado: R$ {format_brl(amount)}"
    )


def _qr_fallback_url_from_pix_code(code: str) -> str:
    return f"https://quickchart.io/qr?text={quote_plus(code)}&size=320"


# amount displayed on the start CTA buttons
START2_AMOUNT_7 = 14.99
START2_AMOUNT_15 = extract_amount_from_text(C.START2_BUTTON_TEXT_15_DAYS, 26.90)
START2_AMOUNT_30 = extract_amount_from_text(C.START2_BUTTON_TEXT_30_DAYS, 35.90)
START2_AMOUNT = START2_AMOUNT_7

# Steps after the user clicks the CTAs and receives Pix instructions.
FOLLOWUP_STEPS: list[Step] = [
    {
        "kind": "photo",
        "media_base": "follo1",  # follo1.jpeg
        "text": C.FOLLOWUP_1_TEXT,
        "button_text": C.FOLLOWUP_1_BUTTON,
        "amount": extract_amount_from_text(C.FOLLOWUP_1_BUTTON, 18.90),
    },
    {
        "kind": "video",
        "media_base": "10vagas",
        "text": C.FOLLOWUP_2_TEXT,
        "button_text": C.FOLLOWUP_2_BUTTON,
        "amount": extract_amount_from_text(C.FOLLOWUP_2_BUTTON, 17.91),
    },
    {
        "kind": "video",
        "media_base": "video_32497",
        "text": C.FOLLOWUP_3_TEXT,
        "button_text": C.FOLLOWUP_3_BUTTON,
        "amount": extract_amount_from_text(C.FOLLOWUP_3_BUTTON, 16.92),
    },
    {
        "kind": "video",
        "media_base": "video_32501",
        "text": C.FOLLOWUP_4_TEXT,
        "button_text": C.FOLLOWUP_4_BUTTON,
        "amount": extract_amount_from_text(C.FOLLOWUP_4_BUTTON, 15.92),
    },
    {
        "kind": "video",
        "media_base": "video_32503",
        "text": C.FOLLOWUP_5_TEXT,
        "button_text": C.FOLLOWUP_5_BUTTON,
        "amount": extract_amount_from_text(C.FOLLOWUP_5_BUTTON, 13.93),
    },
    {
        "kind": "video",
        "media_base": "video_32508",
        "text": C.FOLLOWUP_6_TEXT,
        "button_text": C.FOLLOWUP_6_BUTTON,
        "amount": extract_amount_from_text(C.FOLLOWUP_6_BUTTON, 11.94),
    },
    {
        "kind": "video",
        "media_base": "video_32501",
        "text": C.FOLLOWUP_7_TEXT,
        "button_text": C.FOLLOWUP_7_BUTTON,
        "amount": extract_amount_from_text(C.FOLLOWUP_7_BUTTON, 10.97),
    },
    {
        "kind": "video",
        "media_base": "video_32521",
        "text": C.FOLLOWUP_8_TEXT,
        "button_text": C.FOLLOWUP_8_BUTTON,
        "amount": extract_amount_from_text(C.FOLLOWUP_8_BUTTON, 10.94),
    },
]


def mark_unpaid(user_id: int, chat_id: int, reset_cycle: bool = True) -> None:
    key = _user_key(user_id)
    mapping: dict[str, str] = {
        "chat_id": str(chat_id),
        "paid": "0",
        "bot_id": BOT_ID or "",
    }
    if reset_cycle:
        # Full reset (from /start): clear followup state and unschedule.
        mapping["followup_idx"] = "0"
        mapping["cycle_count"] = "0"
        redis.hset(key, mapping=mapping)
        redis.zrem(DUE_ZSET_KEY, str(user_id))
    else:
        # Soft reset (from plan click): keep followup schedule intact.
        redis.hset(key, mapping=mapping)


def mark_paid(user_id: int) -> None:
    redis.hset(_user_key(user_id), mapping={"paid": "1"})
    redis.zrem(DUE_ZSET_KEY, str(user_id))
    try:
        redis.srem(PIX_PENDING_SET, str(user_id))
    except Exception:
        pass


def mark_blocked(user_id: int) -> None:
    try:
        redis.sadd(BLOCKED_SET, str(user_id))
        redis.zrem(DUE_ZSET_KEY, str(user_id))
    except Exception:
        pass


def is_blocked(user_id: int) -> bool:
    try:
        return redis.sismember(BLOCKED_SET, str(user_id))
    except Exception:
        return False


def is_paid(user_id: int) -> bool:
    return redis.hget(_user_key(user_id), "paid") == "1"


def schedule_next_followup(user_id: int, delay_seconds: int = FOLLOWUP_DELAY_SECONDS) -> None:
    due = int(time.time()) + int(delay_seconds)
    redis.zadd(DUE_ZSET_KEY, {str(user_id): due})


async def send_start(bot: Bot, chat_id: int, user) -> None:
    import asyncio

    username = _format_username(user)
    caption_raw = _personalize_caption(C.START2_CAPTION, username)
    caption = truncate(caption_raw, 1024)

    # ── 1) Envia PRIMEIRA mensagem instantaneamente (video/foto + caption) ──
    media = resolve_media_path(START2_IMAGE)
    if media:
        try:
            if media.suffix.lower() in {".mp4", ".mov", ".webm", ".mkv"}:
                await bot.send_video(
                    chat_id,
                    video=FSInputFile(media),
                    caption=caption or None,
                    parse_mode="HTML",
                )
            else:
                await bot.send_photo(
                    chat_id,
                    photo=FSInputFile(media),
                    caption=caption or None,
                    parse_mode="HTML",
                )
        except Exception:
            await bot.send_message(chat_id, caption or "Oferta indisponível no momento.", parse_mode="HTML")
    else:
        await bot.send_message(chat_id, caption or "Oferta indisponível no momento.", parse_mode="HTML")

    record_funnel_event("start_offer_sent")

    # ── 2) Prova social + botões em background (delay humanizado, NÃO bloqueia) ──
    async def _send_social_proof_delayed():
        try:
            await asyncio.sleep(4)

            proof_img = resolve_media_path(START2_PROOF_IMAGE)
            if proof_img:
                try:
                    await bot.send_photo(chat_id, photo=FSInputFile(proof_img))
                except Exception:
                    pass

            await bot.send_message(
                chat_id,
                C.START2_SOCIAL_PROOF,
                reply_markup=kb_start_primary_offer(START2_AMOUNT_7),
            )
        except Exception as e:
            if "Forbidden" in str(e) or "chat not found" in str(e).lower():
                mark_blocked(user.id if user else 0)

    asyncio.create_task(_send_social_proof_delayed())


async def send_plan_options_message(bot: Bot, chat_id: int, base_7_amount: float) -> None:
    await bot.send_message(
        chat_id,
        "Choose one of the plans below",
        reply_markup=kb_payment_options(base_7_amount, include_previews=False),
    )
    record_funnel_event("plan_options_opened")


async def send_pix_reminder(bot: Bot, user_id: int, chat_id: int, username: str) -> None:
    """
    2-minute payment reminder for Stripe checkout.
    """
    import asyncio

    await asyncio.sleep(120)

    if is_paid(user_id):
        return

    data = redis.hgetall(f"tg:pix:{user_id}") or {}
    checkout_url = (data.get("checkout_url") or data.get("pix_code") or "").strip()
    if not checkout_url:
        return

    reminder_text = (
        f"{username}, your spot is still reserved 🔥\n"
        f"People completed checkout in the last few minutes.\n\n"
        f"Complete payment now to unlock full access:"
    )

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Complete Payment", url=checkout_url)],
            [InlineKeyboardButton(text="✅ I've paid - Verify", callback_data="pay:verify")],
        ]
    )
    try:
        await bot.send_message(chat_id, reminder_text, reply_markup=markup)
        record_funnel_event("payment_reminder_sent", user_id=user_id)
    except Exception as e:
        if "Forbidden" in str(e) or "blocked" in str(e).lower():
            mark_blocked(user_id)
        log("[PIX_REMINDER] erro", {"user_id": user_id, "err": str(e)})


async def send_start2_preview_video(bot: Bot, chat_id: int, preview_number: int, resend_offer: bool = True, username: str = "você") -> None:
    media_base = START2_PREVIEW_1_VIDEO
    story_box = (C.START2_PREVIEW_1_STORY_BOX or "").strip()

    media = resolve_media_path(media_base)
    caption = truncate(sanitize_telegram_export_text(C.START2_PREVIEW_VIDEO_CAPTION), 1024)
    if media:
        try:
            msg = await bot.send_video(   
                chat_id,
                video=FSInputFile(media),
                caption=caption or None,
            )
            save_preview_msg_id(chat_id, msg.message_id)
        except Exception:
            await bot.send_message(chat_id, "Não consegui enviar o vídeo agora. Tente novamente.")
            return
    else:
        await bot.send_message(chat_id, "Preview indisponível no momento.")
        return

    if story_box:
        msg2 = await bot.send_message(
            chat_id,
            f"<pre>{_html_escape(story_box)}</pre>",
            parse_mode="HTML",
        )
        save_preview_msg_id(chat_id, msg2.message_id)

    if resend_offer:
        intimate_text = (
            f"{username}, This preview was just a little taste of what awaits you... 🔥\n"
            f"Unlock full access and watch everything uncensored!"
        )
        await bot.send_message(
            chat_id,
            intimate_text,
            reply_markup=_post_preview_cta_keyboard(),
        )
        record_funnel_event("preview_cta_sent")


async def send_post_preview_payment_buttons(bot: Bot, chat_id: int, username: str = "você") -> None:
    intimate_text = (
        f"{username}, that was your last preview... 😏\n"
        f"there are 10 more videos of this chubby student with the teacher, you're not going to miss them, are you?"
    )
    await bot.send_message(
        chat_id,
        intimate_text,
        reply_markup=_post_preview_cta_keyboard(),
    )


async def send_pix_reminder_preview(bot: Bot, chat_id: int) -> None:
    """Envia videopb.mp4 com storytelling (preview secundária via reminder)."""
    media = resolve_media_path(PIX_REMINDER_PREVIEW_VIDEO)
    caption = truncate(sanitize_telegram_export_text(C.START2_PREVIEW_VIDEO_CAPTION), 1024)
    if media:
        try:
            msg = await bot.send_video(chat_id, video=FSInputFile(media), caption=caption or None)
            save_preview_msg_id(chat_id, msg.message_id)
        except Exception:
            await bot.send_message(chat_id, "Não consegui enviar o vídeo agora.")
            return
    else:
        await bot.send_message(chat_id, "Preview indisponível no momento.")
        return
    
    story_box = (C.PIX_REMINDER_PREVIEW_STORY_BOX or "").strip()
    if story_box:
        msg2 = await bot.send_message(chat_id, f"<pre>{_html_escape(story_box)}</pre>", parse_mode="HTML")
        save_preview_msg_id(chat_id, msg2.message_id)


async def _pix_expiry_countdown(bot: Bot, chat_id: int, user_id: int, msg_id: int, total_seconds: int = 600) -> None:
    """
    Background task: edita uma mensagem de timer a cada 60s criando urgência visual.
    Para automaticamente se o usuário pagar ou o tempo expirar.
    """
    import asyncio

    interval = 60
    elapsed = 0

    while elapsed < total_seconds:
        await asyncio.sleep(interval)
        elapsed += interval

        # Se pagou, para o timer
        if is_paid(user_id):
            try:
                await bot.edit_message_text(
                    "✅ Pagamento confirmado! Acesso liberado!",
                    chat_id=chat_id,
                    message_id=msg_id,
                )
            except Exception:
                pass
            return

        remaining = total_seconds - elapsed

        if remaining <= 0:
            try:
                await bot.edit_message_text(
                    "⏰ Tempo esgotado! Seu código PIX expirou.\n"
                    "Clique em \"Liberar Acesso TOTAL 🥵\" para gerar um novo.",
                    chat_id=chat_id,
                    message_id=msg_id,
                )
            except Exception:
                pass
            return

        mins = remaining // 60
        secs = remaining % 60

        if remaining > 300:
            emoji = "⏱️"
            extra = "Pague agora para garantir seu acesso!"
        elif remaining > 120:
            emoji = "⚠️"
            extra = "Tempo acabando! Não perca sua vaga!"
        else:
            emoji = "🚨"
            extra = "ÚLTIMOS MINUTOS! Pague AGORA ou perca o acesso!"

        try:
            await bot.edit_message_text(
                f"{emoji} Código PIX expira em {mins:02d}:{secs:02d}\n{extra}",
                chat_id=chat_id,
                message_id=msg_id,
            )
        except Exception:
            pass


async def send_after_click_flow(bot: Bot, user_id: int, chat_id: int, amount: float = 14.99, username: str = "você") -> None:
    """
    UK Stripe checkout flow.
    """
    from .config import (
        DEFAULT_CLIENT_DOCUMENT,
        DEFAULT_CLIENT_EMAIL,
        DEFAULT_CLIENT_NAME,
        DEFAULT_CLIENT_PHONE,
    )
    from .pix_payment import create_pix_payment, get_reusable_pending_pix
    from .tracking import get_utms, send_facebook_event, send_to_utmify_order

    mark_unpaid(user_id, chat_id, reset_cycle=False)

    client_name = DEFAULT_CLIENT_NAME or f"User {user_id}"
    client_email = DEFAULT_CLIENT_EMAIL or f"user{user_id}@example.com"
    client_phone = DEFAULT_CLIENT_PHONE or "11999999999"
    client_document = DEFAULT_CLIENT_DOCUMENT or "000.000.000-00"

    utms = get_utms(user_id)

    checkout_data = get_reusable_pending_pix(user_id=user_id, amount=amount, max_age_seconds=300)
    reused_checkout = bool(checkout_data)
    if not checkout_data:
        checkout_data = await create_pix_payment(
            user_id=user_id,
            amount=amount,
            client_name=client_name,
            client_email=client_email,
            client_phone=client_phone,
            client_document=client_document,
            utms=utms,
        )

    if not checkout_data or not checkout_data.get("code"):
        record_funnel_event("payment_create_error", user_id=user_id, amount=amount)
        await bot.send_message(
            chat_id,
            "❌ Could not create your checkout. Please tap the button again.",
        )
        schedule_next_followup(user_id, FOLLOWUP_DELAY_SECONDS)
        return

    record_funnel_event(
        "checkout_reused" if reused_checkout else "checkout_created",
        user_id=user_id,
        amount=amount,
        identifier=str(checkout_data.get("identifier") or ""),
    )

    checkout_url = str(checkout_data.get("checkout_url") or checkout_data.get("code") or "")
    if not checkout_url:
        await bot.send_message(chat_id, "❌ Checkout link unavailable. Please try again.")
        return

    checkout_text = (
        "Finish the payment through the link below"
    )
    checkout_text = truncate(checkout_text, 1024)

    checkout_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Make payment 👉", url=checkout_url)],
            [InlineKeyboardButton(text="I've paid - Verify", callback_data="pay:verify")],
        ]
    )

    await bot.send_message(chat_id, checkout_text, reply_markup=checkout_kb)

    record_funnel_event("checkout_viewed", user_id=user_id, amount=amount)

    # ── Tracking ─────────────────────────────────────────────────────
    try:
        identifier = checkout_data.get("identifier") or ""
        log("[TRACKING] PENDING", {"user_id": user_id, "identifier": identifier, "amount": amount})
        if not reused_checkout:
            await send_to_utmify_order(
                order_id=identifier,
                status="waiting_payment",
                amount=amount,
                customer={
                    "name": client_name,
                    "email": client_email,
                    "phone": client_phone,
                    "document": client_document,
                },
                utms=utms,
                platform="Telegram-UK",
                payment_method="credit_card",
            )
            await send_facebook_event(
                event_name="AddToCart",
                event_id=identifier,
                amount=amount,
                currency="GBP",
                customer={
                    "name": client_name,
                    "email": client_email,
                    "phone": client_phone,
                    "document": client_document,
                },
                utms=utms,
            )
    except Exception:
        pass

    # ── Reminder (2 min delay) ───────────────────────────────────────
    import asyncio
    asyncio.create_task(send_pix_reminder(bot, user_id, chat_id, username))

    # ── Apagar previews após 2 min se não pagou (pressão + escassez) ─
    async def _delete_previews_if_unpaid():
        # Temporarily disabled by request.
        # Re-enable later by removing this early return.
        return
        try:
            await asyncio.sleep(120)
            if is_paid(user_id):
                return
            preview_ids = _get_and_clear_preview_msg_ids(chat_id)
            if not preview_ids:
                return
            deleted = 0
            for msg_id in preview_ids:
                try:
                    await bot.delete_message(chat_id, msg_id)
                    deleted += 1
                except Exception:
                    pass
            if deleted > 0:
                await bot.send_message(
                    chat_id,
                    (
                        f"{username}, your previews were removed from chat for privacy 🔒\n\n"
                        f"Complete checkout now to unlock full access immediately."
                    ),
                    reply_markup=checkout_kb,
                )
                record_funnel_event("previews_deleted_pressure", user_id=user_id)
        except Exception as e:
            if "Forbidden" in str(e) or "blocked" in str(e).lower():
                mark_blocked(user_id)

    # Temporarily disabled by request (do not auto-delete preview messages).
    # asyncio.create_task(_delete_previews_if_unpaid())

    # ── Do NOT re-schedule followup here. The /start handler already
    #    scheduled the single followup. Re-scheduling would reset the
    #    timer every time the user clicks a plan button. ──


async def send_latest_pix_code(bot: Bot, user_id: int, chat_id: int) -> bool:
    data = redis.hgetall(f"tg:pix:{user_id}") or {}
    checkout_url = (data.get("checkout_url") or data.get("pix_code") or "").strip()
    if not checkout_url:
        return False
    checkout_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Make payment 👉", url=checkout_url)],
            [InlineKeyboardButton(text="I've paid - Verify", callback_data="pay:verify")],
        ]
    )
    await bot.send_message(
        chat_id,
        "Finish the payment through the link below",
        reply_markup=checkout_kb,
    )
    return True


async def send_next_followup(bot: Bot, user_id: int) -> bool:
    """
    Single followup at 6 min: sends videopb preview + storytelling.
    If user already has a Stripe checkout link → "Complete Payment" button.
    Otherwise → 3 plans with 15% discount.
    No further followups are scheduled after this.
    """
    if is_paid(user_id) or is_blocked(user_id):
        return False

    key = _user_key(user_id)
    raw_chat_id = redis.hget(key, "chat_id")
    if not raw_chat_id:
        return False
    # Ignore legacy entries written by another bot sharing the same Redis.
    owner_bot_id = redis.hget(key, "bot_id")
    if owner_bot_id and BOT_ID and owner_bot_id != BOT_ID:
        redis.zrem(DUE_ZSET_KEY, str(user_id))
        return False
    if not owner_bot_id:
        redis.zrem(DUE_ZSET_KEY, str(user_id))
        return False
    chat_id = int(raw_chat_id)

    # ── Only fire ONCE (idx 0). After that, stop forever. ──
    idx_raw = redis.hget(key, "followup_idx") or "0"
    try:
        idx = int(idx_raw)
    except ValueError:
        idx = 0
    if idx >= 1:
        # Already sent the single followup — remove from queue and stop.
        redis.zrem(DUE_ZSET_KEY, str(user_id))
        return False

    # ── 1) Send videopb preview (not viewable inline) ──
    media = resolve_media_path(PIX_REMINDER_PREVIEW_VIDEO)
    caption = truncate(sanitize_telegram_export_text(C.START2_PREVIEW_VIDEO_CAPTION), 1024)
    if media:
        try:
            await bot.send_video(chat_id, video=FSInputFile(media), caption=caption or None)
        except Exception as e:
            err = str(e)
            log("[FOLLOWUP] erro mídia", {"user_id": user_id, "media": str(media), "err": err})
            err_lower = err.lower()
            if "forbidden" in err_lower or "chat not found" in err_lower:
                mark_blocked(user_id)
                return False

    # ── 2) Storytelling text ──
    story_box = (C.PIX_REMINDER_PREVIEW_STORY_BOX or "").strip()
    if story_box:
        try:
            await bot.send_message(
                chat_id,
                f"<pre>{_html_escape(story_box)}</pre>",
                parse_mode="HTML",
            )
        except Exception as e:
            err = str(e)
            err_lower = err.lower()
            if "forbidden" in err_lower or "chat not found" in err_lower:
                mark_blocked(user_id)
                return False

    # ── 3) Buttons: "Complete Payment" if checkout exists, else 3 plans at 15% off ──
    from .pix_payment import get_pix_code
    existing_checkout = get_pix_code(user_id)

    if existing_checkout:
        # User already generated a Stripe checkout — show single Complete Payment button
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Complete Payment 👉", url=existing_checkout)],
                [InlineKeyboardButton(text="I've paid - Verify", callback_data="pay:verify")],
            ]
        )
        try:
            await bot.send_message(
                chat_id,
                "Finish the payment through the link below",
                reply_markup=kb,
            )
        except Exception as e:
            err_lower = str(e).lower()
            if "forbidden" in err_lower or "chat not found" in err_lower:
                mark_blocked(user_id)
                return False
    else:
        # No checkout yet — show 3 plans with 15% discount
        d7 = _q2(14.99 * 0.85)
        d15 = _q2(24.99 * 0.85)
        d_life = _q2(49.99 * 0.85)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"£{d7:.2f} / 7 days (15% OFF)", callback_data=f"cta:buy:{d7:.2f}")],
                [InlineKeyboardButton(text=f"£{d15:.2f} / 15 days (15% OFF)", callback_data=f"cta:buy:{d15:.2f}")],
                [InlineKeyboardButton(text=f"£{d_life:.2f} / lifetime (15% OFF)", callback_data=f"cta:buy:{d_life:.2f}")],
            ]
        )
        try:
            await bot.send_message(
                chat_id,
                "Choose one of the plans below 🔥",
                reply_markup=kb,
            )
        except Exception as e:
            err_lower = str(e).lower()
            if "forbidden" in err_lower or "chat not found" in err_lower:
                mark_blocked(user_id)
                return False

    # ── Mark as sent and DO NOT reschedule — this is the only followup. ──
    redis.hset(key, "followup_idx", "1")
    redis.zrem(DUE_ZSET_KEY, str(user_id))
    record_funnel_event("single_followup_sent", user_id=user_id)
    return True


async def campaign_due_loop(bot: Bot) -> None:
    """
    Background loop that triggers followups based on Redis ZSET due timestamps.
    """
    import asyncio

    while True:
        try:
            now = int(time.time())
            user_ids = redis.zrangebyscore(DUE_ZSET_KEY, 0, now, start=0, num=50)
            if not user_ids:
                # sleep a bit if nothing due
                await asyncio.sleep(1.0)
                continue

            for uid in user_ids:
                # best-effort: remove first to avoid double-send if loop stalls
                redis.zrem(DUE_ZSET_KEY, uid)
                try:
                    await send_next_followup(bot, int(uid))
                except Exception as e:
                    log("[CAMPAIGN] followup error", {"uid": uid, "err": str(e)})
                    # Single followup model: do NOT re-schedule on error.
                    continue
        except Exception:
            await asyncio.sleep(2.0)

