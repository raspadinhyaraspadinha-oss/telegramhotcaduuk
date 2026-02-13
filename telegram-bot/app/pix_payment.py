import hashlib
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional

import httpx

from .config import (
    BASE_URL,
    STRIPE_CANCEL_URL,
    STRIPE_CURRENCY,
    STRIPE_SECRET_KEY,
    STRIPE_SUCCESS_URL,
)
from .redis_client import redis

PIX_KEY_PREFIX = "tg:pix:"
PIX_ERR_PREFIX = "tg:pixerr:"
PIX_IDENTIFIER_MAP_KEY = "tg:pix:identifier_map"
PIX_PENDING_SET = "tg:pix:pending"

_PAID_STATUSES = {"PAID", "COMPLETE", "OK"}
_PENDING_STATUSES = {"UNPAID", "OPEN", "PENDING"}


def _normalize_gateway_status(raw: str) -> str:
    s = (raw or "").upper()
    if s in _PAID_STATUSES:
        return "OK"
    if s in _PENDING_STATUSES or not s:
        return "PENDING"
    return s


def _pix_key(user_id: int) -> str:
    return f"{PIX_KEY_PREFIX}{user_id}"


def _pix_err_key(user_id: int) -> str:
    return f"{PIX_ERR_PREFIX}{user_id}"


def _to_2(amount: float) -> str:
    d = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{d:.2f}"


def _only_digits(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())


def _cpf_check_digits(base9: str) -> str:
    nums = [int(x) for x in base9]
    s1 = sum(nums[i] * (10 - i) for i in range(9))
    d1 = (s1 * 10) % 11
    d1 = 0 if d1 == 10 else d1
    nums2 = nums + [d1]
    s2 = sum(nums2[i] * (11 - i) for i in range(10))
    d2 = (s2 * 10) % 11
    d2 = 0 if d2 == 10 else d2
    return f"{d1}{d2}"


def generate_valid_cpf(seed: str) -> str:
    """Gera um CPF válido determinístico a partir de um seed."""
    h = hashlib.sha256(seed.encode()).hexdigest()
    base9 = "".join(str(int(c, 16) % 10) for c in h[:9])
    if len(set(base9)) == 1:
        base9 = "123456789"
    dv = _cpf_check_digits(base9)
    cpf = base9 + dv
    return f"{cpf[0:3]}.{cpf[3:6]}.{cpf[6:9]}-{cpf[9:11]}"


def generate_unique_identifier(user_id: int, amount: float) -> str:
    """Gera um ID único para a transação (usado como external_code)."""
    raw = f"{user_id}:{amount}:{time.time()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _amount_to_cents(amount: float) -> int:
    """Converte valor decimal para centavos."""
    d = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(d * 100)


def _stripe_headers() -> dict:
    return {"Authorization": f"Bearer {STRIPE_SECRET_KEY}"}


def _success_url() -> str:
    if STRIPE_SUCCESS_URL:
        return STRIPE_SUCCESS_URL
    base = (BASE_URL or "").rstrip("/")
    return f"{base}/portal?checkout=ok&session_id={{CHECKOUT_SESSION_ID}}" if base else "https://example.com/success?session_id={CHECKOUT_SESSION_ID}"


def _cancel_url() -> str:
    if STRIPE_CANCEL_URL:
        return STRIPE_CANCEL_URL
    base = (BASE_URL or "").rstrip("/")
    return f"{base}/portal?checkout=cancel" if base else "https://example.com/cancel"


async def create_pix_payment(
    user_id: int,
    amount: float,
    client_name: str = "Cliente Telegram",
    client_email: str = "cliente@telegram.local",
    client_phone: str = "(00) 00000-0000",
    client_document: str = "000.000.000-00",
    utms: Optional[dict] = None,
) -> Optional[dict]:
    """
    Cria checkout Stripe e mantém interface antiga para compatibilidade.
    code = checkout_url
    transactionId = checkout_session_id
    """
    if not STRIPE_SECRET_KEY:
        redis.hset(
            _pix_err_key(user_id),
            mapping={
                "where": "config",
                "error": "STRIPE_SECRET_KEY não configurada",
                "ts": str(int(time.time())),
            },
        )
        return None

    identifier = generate_unique_identifier(user_id, amount)
    try:
        redis.hset(PIX_IDENTIFIER_MAP_KEY, identifier, str(user_id))
    except Exception:
        pass

    amount_cents = _amount_to_cents(amount)
    phone_digits = _only_digits(client_phone) or "0000000000"
    doc = _only_digits(client_document)
    if not doc:
        doc = _only_digits(generate_valid_cpf(str(user_id)))
    currency = (STRIPE_CURRENCY or "gbp").lower()

    # Product name aligned with music CNPJ — varies by plan duration
    product_name = "Digital Music Subscription"
    if amount_cents <= 599:
        product_name = "7-Day Music Streaming Pass"
    elif amount_cents <= 899:
        product_name = "15-Day Premium Music Access"
    elif amount_cents <= 1199:
        product_name = "30-Day Music Collection License"
    else:
        product_name = "Premium Music Equipment Rental"

    form = {
        "mode": "payment",
        # card + Stripe Link (enables Apple Pay / Google Pay via dashboard)
        "payment_method_types[0]": "card",
        "payment_method_types[1]": "link",
        "line_items[0][price_data][currency]": currency,
        "line_items[0][price_data][unit_amount]": str(amount_cents),
        "line_items[0][price_data][product_data][name]": product_name,
        "line_items[0][quantity]": "1",
        "success_url": _success_url(),
        "cancel_url": _cancel_url(),
        "client_reference_id": str(user_id),
        # Force English checkout (prevents Portuguese from BR Stripe account)
        "locale": "en",
        # Let Stripe detect country via IP (suggests UK for UK users, not BR)
        "billing_address_collection": "auto",
        # Statement descriptor: what shows on the bank statement (max 22 chars)
        "payment_intent_data[statement_descriptor]": "DIGITAL SERVICES",
        "metadata[user_id]": str(user_id),
        "metadata[event_id]": identifier,
        "metadata[amount]": _to_2(amount),
        "metadata[document]": doc,
        "metadata[phone]": phone_digits,
        # Expire checkout after 30 min to create real urgency
        "expires_at": str(int(time.time()) + 1800),
    }
    # Do NOT pre-fill customer_email — let the user type their own on Stripe.
    if utms:
        for k, v in utms.items():
            if v:
                form[f"metadata[utm_{k}]"] = str(v)[:400]

    try:
        async with httpx.AsyncClient(timeout=20.0, headers=_stripe_headers()) as client:
            resp = await client.post(
                "https://api.stripe.com/v1/checkout/sessions",
                data=form,
            )

            resp_text = resp.text[:3000] if resp.text else ""
            print(f"[stripe] status={resp.status_code} body={resp_text}")

            if resp.status_code in (200, 201):
                data = resp.json()
                session_id = str(data.get("id") or "")
                checkout_url = str(data.get("url") or "")
                payment_status = str(data.get("payment_status") or "unpaid")

                if not session_id or not checkout_url:
                    redis.hset(
                        _pix_err_key(user_id),
                        mapping={
                            "where": "parse",
                            "status_code": str(resp.status_code),
                            "body": resp_text,
                            "ts": str(int(time.time())),
                            "identifier": identifier,
                            "amount": str(amount),
                            "response_keys": str(list(data.keys())),
                        },
                    )
                    return None

                redis.hset(
                    _pix_key(user_id),
                    mapping={
                        "transaction_id": session_id,
                        "identifier": identifier,
                        "payment_code": session_id,
                        "pix_code": checkout_url,
                        "checkout_url": checkout_url,
                        "stripe_session_id": session_id,
                        "amount": str(amount),
                        "status": _normalize_gateway_status(payment_status),
                        "qr_image": "",
                        "qr_base64": "",
                        "payment_method": "stripe_card",
                        "currency": currency.upper(),
                        "http_status": str(resp.status_code),
                        "created_at": str(int(time.time())),
                        "gateway": "stripe",
                    },
                )

                try:
                    redis.hset(PIX_IDENTIFIER_MAP_KEY, session_id, str(user_id))
                except Exception:
                    pass

                try:
                    redis.sadd(PIX_PENDING_SET, str(user_id))
                except Exception:
                    pass

                return {
                    "code": checkout_url,
                    "transactionId": session_id,
                    "status": _normalize_gateway_status(payment_status),
                    "qr_image": "",
                    "qr_base64": "",
                    "identifier": identifier,
                    "payment_code": session_id,
                    "checkout_url": checkout_url,
                }
            else:
                body = resp_text
                msg = f"[stripe] payment failed status={resp.status_code} body={body}"
                print(msg)
                redis.hset(
                    _pix_err_key(user_id),
                    mapping={
                        "where": "http",
                        "status_code": str(resp.status_code),
                        "body": body,
                        "ts": str(int(time.time())),
                        "identifier": identifier,
                        "amount": str(amount),
                    },
                )
                return None
    except Exception as e:
        print(f"[stripe] exception {type(e).__name__}: {e}")
        redis.hset(
            _pix_err_key(user_id),
            mapping={
                "where": "exception",
                "error": f"{type(e).__name__}: {e}",
                "ts": str(int(time.time())),
                "identifier": identifier,
                "amount": str(amount),
            },
        )
        return None


async def check_payment_status(user_id: int) -> Optional[str]:
    """
    Verifica o status do pagamento no Redis (atualizado via callback ou polling).
    Fallback: consulta Mangofy por payment_code.
    Retorna "OK", "PENDING", "FAILED", etc., ou None se não encontrado.
    """
    status = redis.hget(_pix_key(user_id), "status")
    if status and status != "PENDING":
        return status

    session_id = redis.hget(_pix_key(user_id), "stripe_session_id") or redis.hget(_pix_key(user_id), "transaction_id")
    if not session_id:
        return status

    try:
        async with httpx.AsyncClient(timeout=12.0, headers=_stripe_headers()) as client:
            res = await client.get(
                f"https://api.stripe.com/v1/checkout/sessions/{session_id}",
            )
            if res.status_code == 200:
                data = res.json()
                gateway_status = data.get("payment_status") or data.get("status") or "unpaid"
                normalized = _normalize_gateway_status(gateway_status)
                if normalized != "PENDING":
                    redis.hset(_pix_key(user_id), "status", normalized)
                return normalized
    except Exception:
        return status

    return status


async def check_payment_status_by_identifier(identifier: str, user_id: Optional[int] = None) -> Optional[str]:
    """
    Verifica status pelo payment_code ou external_code.
    Se user_id for informado, também espelha o status em tg:pix:{user_id}.
    """
    if not identifier:
        return None
    try:
        session_id = identifier
        if not session_id.startswith("cs_"):
            mapped_uid = redis.hget(PIX_IDENTIFIER_MAP_KEY, identifier)
            if mapped_uid:
                session_id = redis.hget(_pix_key(int(mapped_uid)), "stripe_session_id") or session_id
        async with httpx.AsyncClient(timeout=12.0, headers=_stripe_headers()) as client:
            res = await client.get(
                f"https://api.stripe.com/v1/checkout/sessions/{session_id}",
            )
            if res.status_code != 200:
                return None
            data = res.json()
            gateway_status = data.get("payment_status") or data.get("status") or "unpaid"
            mapped = _normalize_gateway_status(gateway_status)
            if user_id is not None and mapped:
                redis.hset(_pix_key(user_id), mapping={"status": mapped, "identifier": identifier, "stripe_session_id": session_id})
            return mapped
    except Exception:
        return None


def mark_payment_confirmed(user_id: int) -> None:
    """Marca o pagamento como confirmado (usado pelo callback/webhook)."""
    redis.hset(_pix_key(user_id), "status", "OK")


def get_pix_code(user_id: int) -> Optional[str]:
    return redis.hget(_pix_key(user_id), "checkout_url") or redis.hget(_pix_key(user_id), "pix_code")


def get_reusable_pending_pix(user_id: int, amount: float, max_age_seconds: int = 1800) -> Optional[dict]:
    """
    Reuse an existing pending Stripe checkout if it's still fresh.
    Stripe sessions last 24h, so we use a generous 30-min window by default
    to avoid creating duplicate checkouts when users tap buttons repeatedly.
    Amount is NOT checked — the last created checkout is always reused while fresh.
    """
    data = redis.hgetall(_pix_key(user_id)) or {}
    if not data:
        return None
    status = (data.get("status") or "").upper()
    if status not in ("", "PENDING", "WAITING_PAYMENT"):
        return None

    code = data.get("checkout_url") or data.get("pix_code") or ""
    if not code:
        return None

    created_at = data.get("created_at") or "0"
    try:
        age = int(time.time()) - int(created_at)
    except Exception:
        age = max_age_seconds + 1
    if age > max_age_seconds:
        return None

    return {
        "code": code,
        "transactionId": data.get("transaction_id") or data.get("payment_code") or "",
        "status": status or "PENDING",
        "qr_image": data.get("qr_image") or "",
        "qr_base64": data.get("qr_base64") or "",
        "identifier": data.get("identifier") or "",
        "payment_code": data.get("payment_code") or "",
        "checkout_url": code,
        "reused": True,
    }
