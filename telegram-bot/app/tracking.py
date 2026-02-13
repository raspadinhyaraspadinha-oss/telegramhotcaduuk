import base64
import hashlib
import json
import time
from typing import Any, Dict, Optional
from urllib.parse import parse_qs

import httpx

from .config import (
    BOT_PUBLIC_URL,
    FACEBOOK_ACCESS_TOKEN,
    FACEBOOK_GRAPH_API_URL,
    FACEBOOK_PIXEL_ID,
    FACEBOOK_TEST_EVENT_CODE,
    UTMIFY_API_TOKEN,
    UTMIFY_API_URL,
)
from .redis_client import redis
from .log_buffer import log

UTM_KEY_PREFIX = "tg:utm:"
UTM_TOKEN_PREFIX = "tg:utm:token:"
UTMIFY_RETRY_LIST = "tg:utmify:retry"


def _utm_key(user_id: int) -> str:
    return f"{UTM_KEY_PREFIX}{user_id}"


def _utm_token_key(token: str) -> str:
    return f"{UTM_TOKEN_PREFIX}{token}"


def _sha256_lower(value: str) -> Optional[str]:
    if not value:
        return None
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()


def _normalize_phone_e164_br(phone: str) -> Optional[str]:
    if not phone:
        return None
    digits = "".join(ch for ch in phone if ch.isdigit())
    if not digits:
        return None
    # Keep full international numbers as-is.
    # If local-only short number is provided, return digits (no hardcoded BR prefix).
    return digits


def parse_start_payload(payload: str) -> Dict[str, str]:
    """
    Accepts raw payload from /start <payload>.
    Supports raw querystring (utm_source=..&fbclid=..)
    or base64url-encoded querystring.
    """
    if not payload:
        return {}

    raw = payload.strip()

    def _try_parse(qs: str) -> Dict[str, str]:
        parsed = parse_qs(qs, keep_blank_values=True)
        return {k: str(v[0]) for k, v in parsed.items() if v}

    # direct qs?
    if "=" in raw:
        return _try_parse(raw)

    # try base64url decode
    try:
        padded = raw + "=" * (-len(raw) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode()).decode("utf-8", errors="ignore")
        if "=" in decoded:
            return _try_parse(decoded)
    except Exception:
        pass

    # fallback: store as raw payload (token)
    return {"payload": raw}


def save_utms_token(utms: Dict[str, str], ttl_seconds: int = 7 * 24 * 60 * 60) -> str:
    """
    Save UTMs under a short token (for Telegram start payload length limit).
    Returns the token.
    """
    token = hashlib.sha256(f"{time.time()}:{utms}".encode()).hexdigest()[:10]
    key = _utm_token_key(token)
    try:
        redis.setex(key, ttl_seconds, json.dumps(utms))
        log("[UTMIFY] TOKEN GERADO", token, utms)
    except Exception as e:
        log("[UTMIFY] TOKEN ERRO", type(e).__name__, str(e))
    return token


def get_utms_token(token: str) -> Dict[str, str]:
    key = _utm_token_key(token)
    try:
        raw = redis.get(key)
        if not raw:
            return {}
        return json.loads(raw)
    except Exception:
        return {}


def resolve_start_payload(payload: str) -> Dict[str, str]:
    """
    Resolve payload as:
    - querystring (utm_source=...)
    - base64url querystring
    - short token saved in Redis
    """
    if not payload:
        return {}

    parsed = parse_start_payload(payload)
    # if we got a querystring, return directly
    if parsed and "payload" not in parsed:
        return parsed

    token = parsed.get("payload") if parsed else ""
    if token:
        return get_utms_token(token)
    return {}


def save_utms(user_id: int, utms: Dict[str, str]) -> None:
    if not utms:
        return
    data = {k: str(v) for k, v in utms.items()}
    data["ts"] = str(int(time.time()))
    redis.hset(_utm_key(user_id), mapping=data)


def get_utms(user_id: int) -> Dict[str, str]:
    data = redis.hgetall(_utm_key(user_id))
    return data or {}


def _tracking_parameters(utms: Dict[str, str]) -> Dict[str, Optional[str]]:
    keys = [
        "src",
        "sck",
        "utm_source",
        "utm_campaign",
        "utm_medium",
        "utm_content",
        "utm_term",
        "fbclid",
        "fbp",
    ]
    return {k: utms.get(k) for k in keys}


def _now_utc_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _normalize_utmify_payment_method(method: str) -> str:
    """
    UTMify only accepts: credit_card, boleto, pix, paypal, free_price, unknown.
    Stripe card flows must be reported as credit_card.
    """
    normalized = (method or "").strip().lower()
    if normalized in {"stripe_card", "card", "credit", "creditcard"}:
        return "credit_card"
    if normalized in {"credit_card", "boleto", "pix", "paypal", "free_price", "unknown"}:
        return normalized
    return "credit_card"


async def send_to_utmify_order(
    *,
    order_id: str,
    status: str,
    amount: float,
    customer: Dict[str, str],
    utms: Dict[str, str],
    platform: str = "Telegram",
    payment_method: str = "pix",
) -> None:
    if not UTMIFY_API_TOKEN:
        return

    total_cents = int(round(amount * 100))
    created_at = _now_utc_str()
    approved_date = created_at if status == "paid" else None

    country = "GB" if "UK" in platform.upper() else "BR"
    order = {
        "orderId": order_id,
        "platform": platform,
        "paymentMethod": _normalize_utmify_payment_method(payment_method),
        "status": status,
        "createdAt": created_at,
        "approvedDate": approved_date,
        "refundedAt": None,
        "customer": {
            "name": customer.get("name", ""),
            "email": customer.get("email", ""),
            "phone": str(customer.get("phone", "")),
            "document": str(customer.get("document", "")),
            "country": country,
            "ip": "",
        },
        "products": [
            {
                "id": order_id,
                "name": "VIP",
                "planId": None,
                "planName": None,
                "quantity": 1,
                "priceInCents": total_cents,
            }
        ],
        "trackingParameters": _tracking_parameters(utms),
        "commission": {
            "totalPriceInCents": total_cents,
            "gatewayFeeInCents": 0,
            "userCommissionInCents": total_cents,
        },
        "isTest": False,
    }

    try:
        log("[UTMIFY] ENVIANDO", json.dumps(order, ensure_ascii=False)[:2000])
        async with httpx.AsyncClient(timeout=12.0) as client:
            res = await client.post(
                UTMIFY_API_URL,
                headers={"Content-Type": "application/json", "x-api-token": UTMIFY_API_TOKEN},
                data=json.dumps(order),
            )
            text = res.text
            log("[UTMIFY] RESPOSTA", res.status_code, text[:1000])
            if res.status_code >= 400:
                enqueue_utmify_retry(order, reason=f"http_{res.status_code}")
    except Exception as e:
        log("[UTMIFY] ERRO", type(e).__name__, str(e))
        enqueue_utmify_retry(order, reason=f"exc_{type(e).__name__}")
        return


def enqueue_utmify_retry(order: Dict[str, Any], reason: str) -> None:
    payload = {
        "order": order,
        "reason": reason,
        "attempt": 1,
        "ts": int(time.time()),
    }
    try:
        redis.rpush(UTMIFY_RETRY_LIST, json.dumps(payload))
        log("[UTMIFY] RETRY ENQUEUE", reason)
    except Exception as e:
        log("[UTMIFY] RETRY ENQUEUE ERRO", type(e).__name__, str(e))


async def process_utmify_retries(max_items: int = 10, max_attempts: int = 3) -> None:
    """
    Retry queue for UTMify (best-effort).
    """
    for _ in range(max_items):
        raw = redis.lpop(UTMIFY_RETRY_LIST)
        if not raw:
            return
        try:
            item = json.loads(raw)
            order = item.get("order")
            attempt = int(item.get("attempt", 1))
        except Exception:
            continue
        if isinstance(order, dict):
            order["paymentMethod"] = _normalize_utmify_payment_method(str(order.get("paymentMethod") or ""))

        try:
            log("[UTMIFY] RETRY SENDING", f"attempt={attempt}")
            async with httpx.AsyncClient(timeout=12.0) as client:
                res = await client.post(
                    UTMIFY_API_URL,
                    headers={"Content-Type": "application/json", "x-api-token": UTMIFY_API_TOKEN},
                    data=json.dumps(order),
                )
                text = res.text
                log("[UTMIFY] RETRY RESP", res.status_code, text[:500])
                if res.status_code < 400:
                    continue
        except Exception as e:
            log("[UTMIFY] RETRY ERRO", type(e).__name__, str(e))

        if attempt < max_attempts:
            item["attempt"] = attempt + 1
            try:
                redis.rpush(UTMIFY_RETRY_LIST, json.dumps(item))
            except Exception:
                pass


async def send_facebook_event(
    *,
    event_name: str,
    event_id: str,
    amount: Optional[float],
    currency: str,
    customer: Dict[str, str],
    utms: Dict[str, str],
) -> None:
    if not FACEBOOK_PIXEL_ID or not FACEBOOK_ACCESS_TOKEN:
        return

    ts = int(time.time())
    email_hashed = _sha256_lower(customer.get("email", ""))
    phone_norm = _normalize_phone_e164_br(customer.get("phone", ""))
    phone_hashed = _sha256_lower(phone_norm) if phone_norm else None
    external_id_hashed = _sha256_lower(customer.get("document", ""))

    user_data: Dict[str, Any] = {}
    if email_hashed:
        user_data["em"] = [email_hashed]
    if phone_hashed:
        user_data["ph"] = [phone_hashed]
    if external_id_hashed:
        user_data["external_id"] = external_id_hashed

    # fbc/fbp from utms
    if utms.get("fbclid"):
        user_data["fbc"] = f"fb.1.{ts}.{utms['fbclid']}"
    if utms.get("fbp"):
        user_data["fbp"] = utms["fbp"]

    event: Dict[str, Any] = {
        "event_name": event_name,
        "event_time": ts,
        "event_id": event_id,
        "action_source": "website",
        "event_source_url": BOT_PUBLIC_URL,
        "user_data": user_data,
    }

    if amount is not None:
        event["custom_data"] = {
            "value": float(amount),
            "currency": currency,
            "content_type": "product",
            "content_ids": [event_id],
        }
        for k in ["utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"]:
            if utms.get(k):
                event["custom_data"][k] = utms[k]

    payload: Dict[str, Any] = {"data": [event]}
    if FACEBOOK_TEST_EVENT_CODE:
        payload["test_event_code"] = FACEBOOK_TEST_EVENT_CODE

    try:
        log("[FACEBOOK] ENVIANDO", json.dumps(payload, ensure_ascii=False)[:2000])
        async with httpx.AsyncClient(timeout=12.0) as client:
            url = f"{FACEBOOK_GRAPH_API_URL}/{FACEBOOK_PIXEL_ID}/events?access_token={FACEBOOK_ACCESS_TOKEN}"
            res = await client.post(url, json=payload)
            text = res.text
            log("[FACEBOOK] RESPOSTA", res.status_code, text[:1000])
    except Exception as e:
        log("[FACEBOOK] ERRO", type(e).__name__, str(e))
        return
