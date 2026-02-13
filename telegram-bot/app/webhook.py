import json
import os
import hmac
import hashlib
from pathlib import Path
from datetime import datetime
from html import escape as html_escape
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse, FileResponse
from redis import Redis
from typing import Optional

from .log_buffer import log

from .config import (
    WEBHOOK_SECRET,
    REDIS_URL,
    QUEUE_KEY,
    BOT_DEEPLINK_BASE,
    BOT_PUBLIC_URL,
    FACEBOOK_PIXEL_BROWSER_ID,
    PORTAL_BASE_URL,
    ADMIN_DASHBOARD_TOKEN,
    STRIPE_WEBHOOK_SECRET,
)
from .tracking import save_utms_token
from .portal_content import DEFAULT_MIN_AGE, DEFAULT_MAX_AGE, FAMOUS_PEOPLE, VIDEOS
from .portal_access import get_access_info, get_access_utms
from .pix_payment import create_pix_payment, check_payment_status_by_identifier
from .log_buffer import log
from .pix_payment import PIX_PENDING_SET
from .redis_client import redis
from .funnel_metrics import (
    get_funnel_counters,
    get_day_counters_utc,
    record_funnel_event,
    FUNNEL_EVENTS_KEY,
    FUNNEL_COUNTERS_KEY,
    FUNNEL_DAY_PREFIX,
)

app = FastAPI()

UPSELL_KEY_PREFIX = "tg:upsell:key:"
UPSELL_IDENTIFIER_MAP_KEY = "tg:upsell:identifier_map"
UPSELL_EVENTS_KEY = "tg:upsell:events"
UPSELL_INDEX_ZSET = "tg:upsell:index"


def _upsell_key(access_key: str) -> str:
    return f"{UPSELL_KEY_PREFIX}{access_key}"


def _iso_now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")


def _upsell_event(event: str, payload: dict) -> None:
    rec = {
        "ts": _iso_now(),
        "event": event,
        **payload,
    }
    try:
        redis.lpush(UPSELL_EVENTS_KEY, json.dumps(rec, ensure_ascii=False))
        redis.ltrim(UPSELL_EVENTS_KEY, 0, 999)
    except Exception:
        pass


def _upsell_auth(token: Optional[str]) -> None:
    if not ADMIN_DASHBOARD_TOKEN:
        raise HTTPException(status_code=403, detail="ADMIN_DASHBOARD_TOKEN não configurado")
    if token != ADMIN_DASHBOARD_TOKEN:
        raise HTTPException(status_code=401, detail="Token inválido")


def _map_gateway_status(raw: Optional[str]) -> str:
    """Normaliza status de qualquer gateway para formato interno."""
    s = (raw or "").upper()
    paid = {"OK", "COMPLETED", "TRANSACTION_PAID", "PAID", "APPROVED"}
    pending = {"PENDING", "TRANSACTION_CREATED", "WAITING_PAYMENT", "CREATED", "PROCESSING", "OPEN", "UNPAID"}
    failed = {"FAILED", "CANCELED", "CANCELLED", "EXPIRED", "REFUNDED", "CHARGEBACK", "ERROR"}
    if s in paid:
        return "OK"
    if s in pending or not s:
        return "PENDING"
    if s in failed:
        return s
    return s

def get_redis() -> Redis:
    # cria a conexão quando precisar (evita crash no import)
    return Redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=3, socket_timeout=3)

@app.get("/")
async def health():
    return {
        "ok": True,
        "service": "telegram-webhook",
        "endpoints": {
            "webhook": "/telegram/webhook",
            "stripe_callback": "/stripe/webhook",
            "mangofy_callback": "/mangofy/callback",
            "debug": "/debug"
        }
    }


@app.get("/debug")
async def debug():
    """Endpoint de debug para verificar status do serviço."""
    from .config import BOT_TOKEN, BASE_URL, REDIS_URL, QUEUE_KEY
    
    r = get_redis()
    
    try:
        # Tenta fazer ping no Redis
        r.ping()
        redis_status = "✅ Conectado"
        queue_size = r.llen(QUEUE_KEY)
    except Exception as e:
        redis_status = f"❌ Erro: {str(e)}"
        queue_size = "N/A"
    
    return {
        "status": "running",
        "bot_configured": bool(BOT_TOKEN),
        "webhook_url_configured": bool(BASE_URL),
        "redis_status": redis_status,
        "queue_size": queue_size,
        "queue_key": QUEUE_KEY,
    }


@app.get("/admin/ops")
async def admin_ops(token: Optional[str] = None, x_admin_token: Optional[str] = Header(default=None)):
    _upsell_auth(token or x_admin_token)
    try:
        queue_size = redis.llen(QUEUE_KEY)
    except Exception:
        queue_size = -1
    try:
        pix_pending = redis.scard(PIX_PENDING_SET)
    except Exception:
        pix_pending = -1
    try:
        followup_due = redis.zcard("tg:campaign:due")
    except Exception:
        followup_due = -1
    return {
        "ok": True,
        "queue_size": int(queue_size),
        "pix_pending_count": int(pix_pending),
        "followup_due_count": int(followup_due),
    }


@app.get("/meta.json")
async def meta_json():
    # Evita ruído de 404 em scanners/preloads.
    return {"ok": True}


@app.get("/r")
async def redirect_with_utms(request: Request):
    """
    Captura UTMs via querystring e redireciona para o bot com start=<token>.
    Use no anúncio para evitar limite de 64 chars do /start.
    Ex: https://seu-webhook/r?utm_source=FB&utm_campaign=...
    """
    utms = {k: v for k, v in request.query_params.items()}
    if not utms:
        return {"ok": False, "error": "missing query params"}

    token = save_utms_token(utms)
    # redirect to intermediate page for browser pixel, then to Telegram
    return RedirectResponse(url=f"/p?token={token}")


@app.head("/r")
async def head_r():
    return {"ok": True}


@app.get("/p", response_class=HTMLResponse)
async def pixel_page(token: str):
    """
    Intermediate page: fires FB Pixel PageView, filters bots via JS challenge,
    then redirects real users (including mobile) to Telegram deeplink.
    Bots without JS/touch/mouse never get redirected.
    """
    if not token:
        return HTMLResponse("<html><body>Missing token</body></html>", status_code=400)

    deeplink = f"{BOT_DEEPLINK_BASE}{token}"
    html = f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Redirecting...</title>
    <style>
      body {{ margin:0; background:#000; color:#fff; font-family:sans-serif;
             display:flex; align-items:center; justify-content:center; height:100vh; }}
      .box {{ text-align:center; padding:20px; }}
      .spinner {{ width:36px; height:36px; border:3px solid #333; border-top:3px solid #9b7dff;
                  border-radius:50%; animation:spin .8s linear infinite; margin:0 auto 16px; }}
      @keyframes spin {{ to {{ transform:rotate(360deg) }} }}
      a {{ color:#9b7dff; text-decoration:none; }}
      #fallback {{ display:none; margin-top:16px; }}
    </style>
    <script>
      // ── Facebook Pixel ──
      !function(f,b,e,v,n,t,s)
      {{if(f.fbq)return;n=f.fbq=function(){{n.callMethod?
      n.callMethod.apply(n,arguments):n.queue.push(arguments)}};
      if(!f._fbq)f._fbq=n;n.push=n;n.loaded=!0;n.version='2.0';
      n.queue=[];t=b.createElement(e);t.async=!0;
      t.src=v;s=b.getElementsByTagName(e)[0];
      s.parentNode.insertBefore(t,s)}}(window, document,'script',
      'https://connect.facebook.net/en_US/fbevents.js');
      fbq('init', '{FACEBOOK_PIXEL_BROWSER_ID}');
      fbq('track', 'PageView');

      // ── Bot filter: only redirect if real browser environment ──
      var passed = false;
      function go() {{
        if (passed) return;
        passed = true;
        window.location.href = '{deeplink}';
      }}

      // Real browsers: have screen dimensions, support touch or mouse events
      function check() {{
        var w = window.innerWidth || screen.width || 0;
        var h = window.innerHeight || screen.height || 0;
        // Basic sanity: real device has >0 dimensions and a navigator
        if (w > 0 && h > 0 && navigator.userAgent && navigator.userAgent.length > 10) {{
          // Pass — schedule redirect (short delay for pixel to fire)
          setTimeout(go, 500);
        }} else {{
          // Likely bot — show fallback link but don't auto-redirect
          document.getElementById('fallback').style.display = 'block';
        }}
      }}

      // Run check after DOM is ready
      if (document.readyState === 'loading') {{
        document.addEventListener('DOMContentLoaded', check);
      }} else {{
        check();
      }}

      // Extra safety: if still here after 4s, show manual link
      setTimeout(function() {{
        if (!passed) {{
          document.getElementById('fallback').style.display = 'block';
        }}
      }}, 4000);
    </script>
    <noscript>
      <img height="1" width="1" style="display:none"
           src="https://www.facebook.com/tr?id={FACEBOOK_PIXEL_BROWSER_ID}&ev=PageView&noscript=1"/>
    </noscript>
  </head>
  <body>
    <div class="box">
      <div class="spinner"></div>
      <div>Loading...</div>
      <div id="fallback">
        <a href="{deeplink}">Tap here to continue</a>
      </div>
    </div>
  </body>
</html>"""
    return HTMLResponse(html)


@app.get("/portal", response_class=HTMLResponse)
async def portal_page():
    """
    Página do portal (mobile-friendly) com popup de chave.
    """
    html = f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Portal VIP</title>
    <style>
      :root {{
        --bg: #0b0b0f;
        --card: #14141b;
        --muted: #8a8a9a;
        --accent: #9b7dff;
        --accent-2: #20c997;
        --danger: #ff6b6b;
        --text: #f2f2f6;
        --radius: 16px;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial;
        background: var(--bg); color: var(--text);
      }}
      header {{
        padding: 20px 16px 8px;
      }}
      h1 {{ margin: 0; font-size: 20px; font-weight: 700; }}
      .sub {{ color: var(--muted); font-size: 13px; margin-top: 6px; }}
      .filters {{
        padding: 12px 16px;
        display: grid; gap: 12px;
      }}
      .filter-card {{
        background: var(--card);
        border-radius: var(--radius);
        padding: 12px;
      }}
      .filter-title {{ font-size: 13px; color: var(--muted); margin-bottom: 8px; }}
      .chips {{
        display: flex; flex-wrap: wrap; gap: 8px;
      }}
      .chip {{
        padding: 6px 10px; border-radius: 999px;
        background: #1e1e27; color: var(--text); font-size: 12px; cursor: pointer;
      }}
      .chip.active {{ background: var(--accent); }}
      .range {{
        display: grid; grid-template-columns: 1fr 1fr; gap: 10px;
      }}
      .range input {{
        width: 100%; padding: 10px; border-radius: 10px; border: 1px solid #2a2a35;
        background: #111118; color: var(--text);
      }}
      .list {{
        padding: 8px 16px 120px; display: grid; gap: 12px;
      }}
      .card {{
        display: grid; grid-template-columns: 96px 1fr; gap: 12px;
        background: var(--card); border-radius: var(--radius); padding: 10px; align-items: center;
      }}
      .thumb {{
        width: 96px; height: 64px; border-radius: 12px; overflow: hidden;
        background: linear-gradient(135deg, #f3d6c9, #b68b78);
      }}
      .thumb img {{ width: 100%; height: 100%; object-fit: cover; filter: blur(2px); }}
      .title {{ font-size: 14px; font-weight: 600; }}
      .meta {{ font-size: 12px; color: var(--muted); margin-top: 4px; }}
      .popup {{
        position: fixed; inset: 0; background: rgba(0,0,0,0.65);
        display: flex; align-items: center; justify-content: center; padding: 16px;
      }}
      .popup-card {{
        width: 100%; max-width: 420px; background: #111118; border-radius: 18px;
        padding: 20px; border: 1px solid #262633;
      }}
      .popup-title {{ font-size: 18px; font-weight: 700; }}
      .popup-text {{ color: var(--muted); font-size: 13px; margin: 8px 0 14px; }}
      .input {{
        width: 100%; padding: 12px; border-radius: 10px; border: 1px solid #2a2a35;
        background: #0e0e14; color: var(--text);
      }}
      .btn {{
        width: 100%; padding: 12px; border-radius: 12px; border: 0; margin-top: 10px;
        background: var(--accent); color: white; font-weight: 700; cursor: pointer;
      }}
      .btn.secondary {{ background: #1f1f2a; color: var(--text); }}
      .btn.danger {{ background: var(--danger); }}
      .badge {{
        display: inline-block; padding: 4px 8px; border-radius: 8px;
        background: #1c1c27; color: var(--accent-2); font-size: 12px; margin-top: 8px;
      }}
      .qr {{
        padding: 10px; background: #0e0e14; border-radius: 12px; margin-top: 10px;
        font-size: 12px; word-break: break-all;
      }}
    </style>
  </head>
  <body>
    <header>
      <h1>VIP Content</h1>
      <div class="sub">Explore the full video library</div>
    </header>

    <section class="filters">
      <div class="filter-card">
        <div class="filter-title">Age filter (min / max)</div>
        <div class="range">
          <input id="minAge" type="number" placeholder="Min" />
          <input id="maxAge" type="number" placeholder="Max" />
        </div>
      </div>
      <div class="filter-card">
        <div class="filter-title">Famous people</div>
        <div id="peopleChips" class="chips"></div>
      </div>
    </section>

    <section id="videoList" class="list"></section>

    <div id="popup" class="popup">
      <div class="popup-card" id="popupCard">
        <div class="popup-title">Enter your access key</div>
        <div class="popup-text">You received the key on Telegram after payment.</div>
        <input id="accessKey" class="input" placeholder="Paste your key here" />
        <button id="btnCheck" class="btn">Access</button>
      </div>
    </div>

    <script>
      const state = {{ videos: [], people: [], minAge: {DEFAULT_MIN_AGE}, maxAge: {DEFAULT_MAX_AGE} }};

      async function loadContent() {{
        const res = await fetch("/portal/content");
        const data = await res.json();
        state.videos = data.videos;
        state.people = data.people;
        document.getElementById("minAge").value = data.minAge;
        document.getElementById("maxAge").value = data.maxAge;
        renderPeople();
        renderList();
      }}

      function renderPeople() {{
        const el = document.getElementById("peopleChips");
        el.innerHTML = "";
        state.people.forEach(p => {{
          const chip = document.createElement("div");
          chip.className = "chip";
          chip.innerText = p;
          chip.onclick = () => {{
            chip.classList.toggle("active");
            renderList();
          }};
          el.appendChild(chip);
        }});
      }}

      function renderList() {{
        const list = document.getElementById("videoList");
        const min = parseInt(document.getElementById("minAge").value || state.minAge);
        const max = parseInt(document.getElementById("maxAge").value || state.maxAge);
        const selected = Array.from(document.querySelectorAll(".chip.active")).map(x => x.innerText);
        list.innerHTML = "";
        state.videos.filter(v => {{
          const inAge = v.min_age >= min && v.max_age <= max;
          const inPeople = selected.length === 0 || v.people.some(p => selected.includes(p));
          return inAge && inPeople;
        }}).forEach(v => {{
          const card = document.createElement("div");
          card.className = "card";
          card.innerHTML = `
            <div class="thumb"><img src="${{v.thumbnail}}" /></div>
            <div>
              <div class="title">${{v.title}}</div>
              <div class="meta">${{v.min_age}} - ${{v.max_age}} anos • ${{v.people.join(", ")}}</div>
            </div>
          `;
          list.appendChild(card);
        }});
      }}

      function updatePopupBalance() {{
        const el = document.getElementById("popupCard");
        el.innerHTML = `
          <div class="popup-title">Access validated</div>
          <div class="popup-text">Available balance: <strong>£9.99</strong></div>
          <div class="badge">Silver Plan</div>
          <div class="popup-text" style="margin-top:10px;">All 100 Silver spots have been filled.</div>
          <div class="popup-text">You need <strong>£19.99</strong> more to unlock full access (Black).</div>
          <div class="popup-text">Would you like to upgrade now or request a refund of £9.99?</div>
          <button class="btn" id="btnUpgrade">Upgrade to full access</button>
          <button class="btn secondary" id="btnRefund">Refund</button>
        `;
        document.getElementById("btnUpgrade").onclick = upgradeFlow;
        document.getElementById("btnRefund").onclick = refundFlow;
      }}

      let currentKey = "";

      async function verifyAccessKey(key) {{
        currentKey = key || "";
        if (!currentKey) {{
          alert("Chave inválida");
          return false;
        }}
        const res = await fetch(`/portal/verify?key=${{encodeURIComponent(currentKey)}}`);
        const data = await res.json();
        if (data.ok) {{
          updatePopupBalance();
          return true;
        }}
        alert("Chave inválida");
        return false;
      }}

      async function upgradeFlow() {{
        const key = currentKey;
        if (!key) {{
          alert("Invalid key.");
          return;
        }}
        const el = document.getElementById("popupCard");
        el.innerHTML = `
          <div class="popup-title">Processing...</div>
          <div class="popup-text">Creating your secure payment link...</div>
        `;
        const res = await fetch(`/portal/upsell?key=${{encodeURIComponent(key)}}`, {{ method: "POST" }});
        const data = await res.json();
        if (!data.ok) {{
          el.innerHTML = `
            <div class="popup-title">Payment error</div>
            <div class="popup-text">Please try again in a moment.</div>
            <button class="btn secondary" onclick="window.location.reload()">Close</button>
          `;
          return;
        }}
        const checkoutUrl = data.checkout_url || data.code || "";
        if (checkoutUrl && checkoutUrl.startsWith("http")) {{
          // Redirect to Stripe Checkout
          window.location.href = checkoutUrl;
          return;
        }}
        // Fallback: show link manually
        el.innerHTML = `
          <div class="popup-title">Complete your upgrade</div>
          <div class="popup-text">Pay £19.99 to unlock full access.</div>
          <a href="${{checkoutUrl}}" class="btn" style="display:block;text-align:center;text-decoration:none;">Pay with Stripe</a>
          <button class="btn secondary" id="btnConfirm" style="margin-top:8px;">I've paid - Verify</button>
        `;
        document.getElementById("btnConfirm").onclick = async () => {{
          const r = await fetch(`/portal/check?key=${{encodeURIComponent(key)}}`);
          const s = await r.json();
          if (s.status === "OK") {{
            el.innerHTML = `
              <div class="popup-title">Payment confirmed</div>
              <div class="popup-text">Your full access will be unlocked shortly.</div>
              <button class="btn secondary" onclick="window.location.reload()">Close</button>
            `;
          }} else {{
            alert("Payment still pending. Please wait a moment.");
          }}
        }};
      }}

      function refundFlow() {{
        const el = document.getElementById("popupCard");
        el.innerHTML = `
          <div class="popup-title">Confirm refund</div>
          <div class="popup-text">You will lose access immediately. Do you want to continue?</div>
          <button class="btn danger" id="btnYes">Yes, refund</button>
          <button class="btn secondary" id="btnNo">No, go back</button>
        `;
        document.getElementById("btnYes").onclick = () => {{
          window.location.href = "https://facebook.com.br";
        }};
        document.getElementById("btnNo").onclick = () => {{
          window.location.reload();
        }};
      }}

      document.getElementById("btnCheck").onclick = async () => {{
        const key = document.getElementById("accessKey").value;
        await verifyAccessKey(key);
      }};

      loadContent();
      const queryKey = new URLSearchParams(window.location.search).get("key") || "";
      if (queryKey) {{
        document.getElementById("accessKey").value = queryKey;
        verifyAccessKey(queryKey);
      }}
      document.getElementById("minAge").oninput = renderList;
      document.getElementById("maxAge").oninput = renderList;
    </script>
  </body>
</html>"""
    return HTMLResponse(html)


MEDIA_DIR = Path(__file__).resolve().parent / "media"


@app.get("/portal/media/{filename:path}")
async def portal_media(filename: str):
    """Serve arquivos da pasta app/media para thumbnails do portal."""
    base = MEDIA_DIR.resolve()
    path = (MEDIA_DIR / filename).resolve()
    try:
        path.relative_to(base)
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path)


@app.get("/portal/content")
async def portal_content():
    return {
        "minAge": DEFAULT_MIN_AGE,
        "maxAge": DEFAULT_MAX_AGE,
        "people": FAMOUS_PEOPLE,
        "videos": [v.__dict__ for v in VIDEOS],
    }


@app.get("/portal/verify")
async def portal_verify(key: str):
    info = get_access_info(key)
    if not info:
        _upsell_event("portal_verify_invalid", {"key": key})
        return {"ok": False}
    _upsell_event("portal_verify_ok", {"key": key, "user_id": info.get("user_id")})
    return {"ok": True, "portal_link": f"{PORTAL_BASE_URL}?key={key}"}


@app.post("/portal/upsell")
async def portal_upsell(key: str):
    info = get_access_info(key)
    if not info:
        return {"ok": False, "error": "invalid_key"}

    user_id = info["user_id"]
    utms = get_access_utms(key)
    up_key = _upsell_key(key)
    existing = redis.hgetall(up_key) or {}
    now_ts_int = int(datetime.utcnow().timestamp())
    UPSELL_AMOUNT_GBP = 19.99
    if existing:
        status_existing = _map_gateway_status(existing.get("status"))
        created = int(existing.get("created_at") or "0")
        age = now_ts_int - created if created else 999999
        checkout_url = (existing.get("checkout_url") or existing.get("pix_code") or "").strip()
        if status_existing == "PENDING" and age <= 300 and checkout_url:
            _upsell_event("upsell_reused", {"key": key, "user_id": user_id, "identifier": existing.get("identifier", "")})
            return {
                "ok": True,
                "code": checkout_url,
                "checkout_url": checkout_url,
                "identifier": existing.get("identifier", ""),
                "reused": True,
            }
    # Create Stripe checkout for upgrade (£19.99 GBP)
    pix = await create_pix_payment(
        user_id=user_id,
        amount=UPSELL_AMOUNT_GBP,
        client_name="Portal Upgrade",
        client_email="portal@upgrade.local",
        client_phone="0000000000",
        client_document="000.000.000-00",
        utms=utms,
    )
    if not pix:
        log("[PORTAL] upsell stripe error", {"key": key, "user_id": user_id})
        _upsell_event("upsell_stripe_error", {"key": key, "user_id": user_id})
        return {"ok": False, "error": "stripe_error"}
    identifier = pix.get("identifier") or ""
    checkout_url = pix.get("checkout_url") or pix.get("code") or ""
    now_ts = str(now_ts_int)
    redis.hset(
        up_key,
        mapping={
            "access_key": key,
            "user_id": str(user_id),
            "identifier": identifier,
            "amount": str(UPSELL_AMOUNT_GBP),
            "status": "PENDING",
            "pix_code": checkout_url,
            "checkout_url": checkout_url,
            "portal_link": f"{PORTAL_BASE_URL}?key={key}",
            "created_at": now_ts,
            "updated_at": now_ts,
        },
    )
    redis.expire(up_key, 30 * 24 * 60 * 60)
    if identifier:
        redis.hset(UPSELL_IDENTIFIER_MAP_KEY, identifier, key)
    redis.zadd(UPSELL_INDEX_ZSET, {key: int(now_ts)})
    log("[PORTAL] upsell stripe ok", {"key": key, "user_id": user_id, "identifier": identifier})
    _upsell_event("upsell_stripe_created", {"key": key, "user_id": user_id, "identifier": identifier, "amount": UPSELL_AMOUNT_GBP})
    return {
        "ok": True,
        "code": checkout_url,
        "checkout_url": checkout_url,
        "identifier": identifier,
    }


@app.get("/portal/check")
async def portal_check(key: str):
    info = get_access_info(key)
    if not info:
        return {"ok": False, "error": "invalid_key"}
    user_id = info["user_id"]
    upsell_data = redis.hgetall(_upsell_key(key)) or {}
    identifier = upsell_data.get("identifier") or ""
    status = None
    if identifier:
        status = await check_payment_status_by_identifier(identifier, user_id=user_id)
    if not status:
        status = upsell_data.get("status") or "PENDING"
    status = _map_gateway_status(status)
    redis.hset(_upsell_key(key), mapping={"status": status, "updated_at": str(int(datetime.utcnow().timestamp()))})
    if status == "OK":
        redis.hset(_upsell_key(key), "paid_at", str(int(datetime.utcnow().timestamp())))
        _upsell_event("upsell_paid_check", {"key": key, "user_id": user_id, "identifier": identifier})
    return {"ok": True, "status": status}


@app.get("/admin/upsell", response_class=HTMLResponse)
async def admin_upsell_dashboard(token: Optional[str] = None, x_admin_token: Optional[str] = Header(default=None)):
    _upsell_auth(token or x_admin_token)

    access_keys = redis.zrevrange(UPSELL_INDEX_ZSET, 0, 199)
    rows = []
    total = 0
    pending = 0
    paid = 0
    failed = 0
    paid_amount = 0.0

    for access_key in access_keys:
        data = redis.hgetall(_upsell_key(access_key)) or {}
        if not data:
            continue
        total += 1
        status = (data.get("status") or "PENDING").upper()
        if status == "OK":
            paid += 1
            try:
                paid_amount += float(data.get("amount") or "0")
            except Exception:
                pass
        elif status in ("PENDING", "WAITING_PAYMENT"):
            pending += 1
        else:
            failed += 1
        rows.append(
            {
                "access_key": access_key,
                "user_id": data.get("user_id", ""),
                "status": status,
                "amount": data.get("amount", ""),
                "identifier": data.get("identifier", ""),
                "created_at": data.get("created_at", ""),
                "updated_at": data.get("updated_at", ""),
            }
        )

    event_lines = []
    for raw in redis.lrange(UPSELL_EVENTS_KEY, 0, 99):
        try:
            item = json.loads(raw)
            event_lines.append(html_escape(json.dumps(item, ensure_ascii=False)))
        except Exception:
            event_lines.append(html_escape(str(raw)))
    events_html = "<br/>".join(event_lines) if event_lines else "Sem eventos"

    rows_html = ""
    for row in rows:
        rows_html += (
            "<tr>"
            f"<td>{html_escape(str(row['access_key']))}</td>"
            f"<td>{html_escape(str(row['user_id']))}</td>"
            f"<td>{html_escape(str(row['status']))}</td>"
            f"<td>R$ {html_escape(str(row['amount']))}</td>"
            f"<td>{html_escape(str(row['identifier']))}</td>"
            f"<td>{html_escape(str(row['created_at']))}</td>"
            f"<td>{html_escape(str(row['updated_at']))}</td>"
            "</tr>"
        )

    html = f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Admin Upsell</title>
    <style>
      body {{ font-family: Arial, sans-serif; background:#0e0f14; color:#f3f5ff; margin:0; padding:16px; }}
      .cards {{ display:grid; grid-template-columns: repeat(auto-fit,minmax(180px,1fr)); gap:10px; margin-bottom:14px; }}
      .card {{ background:#161925; border-radius:10px; padding:12px; border:1px solid #23263a; }}
      .label {{ font-size:12px; color:#9ea6c7; }}
      .val {{ font-size:22px; font-weight:700; margin-top:4px; }}
      table {{ width:100%; border-collapse:collapse; background:#161925; border:1px solid #23263a; }}
      th, td {{ border-bottom:1px solid #23263a; padding:8px; text-align:left; font-size:12px; }}
      th {{ background:#1b2031; position:sticky; top:0; }}
      .events {{ margin-top:14px; background:#161925; border:1px solid #23263a; border-radius:10px; padding:12px; font-size:12px; max-height:260px; overflow:auto; }}
    </style>
  </head>
  <body>
    <h1>Dashboard Upsell</h1>
    <div class="cards">
      <div class="card"><div class="label">Total registros</div><div class="val">{total}</div></div>
      <div class="card"><div class="label">Pendentes</div><div class="val">{pending}</div></div>
      <div class="card"><div class="label">Pagos</div><div class="val">{paid}</div></div>
      <div class="card"><div class="label">Falhos</div><div class="val">{failed}</div></div>
      <div class="card"><div class="label">Receita upsell</div><div class="val">R$ {paid_amount:.2f}</div></div>
    </div>
    <table>
      <thead>
        <tr>
          <th>Access Key</th><th>User</th><th>Status</th><th>Valor</th><th>Identifier</th><th>Criado</th><th>Atualizado</th>
        </tr>
      </thead>
      <tbody>{rows_html or "<tr><td colspan='7'>Sem dados</td></tr>"}</tbody>
    </table>
    <div class="events">
      <strong>Eventos recentes</strong><br/><br/>{events_html}
    </div>
  </body>
</html>"""
    return HTMLResponse(html)


@app.get("/admin/funnel", response_class=HTMLResponse)
async def admin_funnel_dashboard(token: Optional[str] = None, x_admin_token: Optional[str] = Header(default=None)):
    _upsell_auth(token or x_admin_token)
    counters = get_funnel_counters()
    day = get_day_counters_utc()
    created = int(counters.get("pix_created", "0") or "0")
    reused = int(counters.get("pix_reused", "0") or "0")
    viewed = int(counters.get("pix_viewed", "0") or "0")
    verify_clicked = int(counters.get("verify_clicked", "0") or "0")
    paid = int(counters.get("payment_confirmed", "0") or "0")

    def pct(a: int, b: int) -> str:
        if b <= 0:
            return "0.0%"
        return f"{(100.0 * a / b):.1f}%"

    rows_html = ""
    for k, v in sorted(counters.items()):
        rows_html += f"<tr><td>{html_escape(str(k))}</td><td>{html_escape(str(v))}</td></tr>"
    today_html = ""
    for k, v in sorted(day.items()):
        today_html += f"<tr><td>{html_escape(str(k))}</td><td>{html_escape(str(v))}</td></tr>"

    events = redis.lrange("tg:funnel:events", 0, 99)
    events_html = ""
    for raw in events:
        events_html += f"{html_escape(raw)}<br/>"
    if not events_html:
        events_html = "Sem eventos"

    html = f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Admin Funnel</title>
    <style>
      body {{ font-family: Arial, sans-serif; background:#0e0f14; color:#f3f5ff; margin:0; padding:16px; }}
      .cards {{ display:grid; grid-template-columns: repeat(auto-fit,minmax(180px,1fr)); gap:10px; margin-bottom:14px; }}
      .card {{ background:#161925; border-radius:10px; padding:12px; border:1px solid #23263a; }}
      .label {{ font-size:12px; color:#9ea6c7; }}
      .val {{ font-size:22px; font-weight:700; margin-top:4px; }}
      table {{ width:100%; border-collapse:collapse; background:#161925; border:1px solid #23263a; margin-top:12px; }}
      th, td {{ border-bottom:1px solid #23263a; padding:8px; text-align:left; font-size:12px; }}
      .events {{ margin-top:14px; background:#161925; border:1px solid #23263a; border-radius:10px; padding:12px; font-size:12px; max-height:260px; overflow:auto; }}
      .grid {{ display:grid; grid-template-columns: 1fr 1fr; gap:12px; }}
    </style>
  </head>
  <body>
    <h1>Dashboard de Conversão</h1>
    <div class="cards">
      <div class="card"><div class="label">Pix criados</div><div class="val">{created}</div></div>
      <div class="card"><div class="label">Pix reutilizados</div><div class="val">{reused}</div></div>
      <div class="card"><div class="label">Pix vistos</div><div class="val">{viewed}</div></div>
      <div class="card"><div class="label">Cliques em verificar</div><div class="val">{verify_clicked}</div></div>
      <div class="card"><div class="label">Pagos</div><div class="val">{paid}</div></div>
      <div class="card"><div class="label">Conversão pagamento</div><div class="val">{pct(paid, created)}</div></div>
      <div class="card"><div class="label">Conversão pós visualização</div><div class="val">{pct(paid, viewed)}</div></div>
      <div class="card"><div class="label">Verify -> Pago</div><div class="val">{pct(paid, verify_clicked)}</div></div>
    </div>
    <div class="grid">
      <table>
        <thead><tr><th>Contador global</th><th>Valor</th></tr></thead>
        <tbody>{rows_html or "<tr><td colspan='2'>Sem dados</td></tr>"}</tbody>
      </table>
      <table>
        <thead><tr><th>Contador de hoje (UTC)</th><th>Valor</th></tr></thead>
        <tbody>{today_html or "<tr><td colspan='2'>Sem dados</td></tr>"}</tbody>
      </table>
    </div>
    <div class="events"><strong>Eventos recentes</strong><br/><br/>{events_html}</div>
  </body>
</html>"""
    return HTMLResponse(html)


@app.post("/admin/funnel/reset")
@app.get("/admin/funnel/reset")
async def admin_funnel_reset(
    token: Optional[str] = None,
    confirm: Optional[str] = None,
    x_admin_token: Optional[str] = Header(default=None),
):
    """
    Reseta métricas do funil.
    Segurança: exige token + confirm=RESET.
    """
    _upsell_auth(token or x_admin_token)
    if (confirm or "").upper() != "RESET":
        return {
            "ok": False,
            "error": "confirm_required",
            "hint": "Use confirm=RESET",
        }

    deleted = []
    try:
        if redis.exists(FUNNEL_EVENTS_KEY):
            redis.delete(FUNNEL_EVENTS_KEY)
            deleted.append(FUNNEL_EVENTS_KEY)
    except Exception:
        pass
    try:
        if redis.exists(FUNNEL_COUNTERS_KEY):
            redis.delete(FUNNEL_COUNTERS_KEY)
            deleted.append(FUNNEL_COUNTERS_KEY)
    except Exception:
        pass

    day_keys = []
    try:
        for k in redis.scan_iter(f"{FUNNEL_DAY_PREFIX}*"):
            day_keys.append(k)
        if day_keys:
            redis.delete(*day_keys)
    except Exception:
        pass

    return {
        "ok": True,
        "deleted_keys": deleted,
        "deleted_day_keys_count": len(day_keys),
        "message": "Métricas do funil resetadas com sucesso.",
    }


@app.head("/p")
async def head_p():
    return {"ok": True}

@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
):
    if WEBHOOK_SECRET and x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        return {"ok": False}

    data = await request.json()
    redis.rpush(QUEUE_KEY, json.dumps(data))
    return {"ok": True}


def _verify_stripe_signature(raw_body: bytes, stripe_signature: str) -> bool:
    """
    Verifica assinatura Stripe (v1) sem depender de SDK externo.
    """
    if not STRIPE_WEBHOOK_SECRET:
        return True
    if not stripe_signature:
        return False
    parts = {}
    for item in stripe_signature.split(","):
        if "=" in item:
            k, v = item.split("=", 1)
            parts[k.strip()] = v.strip()
    timestamp = parts.get("t")
    sig_v1 = parts.get("v1")
    if not timestamp or not sig_v1:
        return False
    signed_payload = f"{timestamp}.{raw_body.decode('utf-8')}".encode("utf-8")
    expected = hmac.new(
        STRIPE_WEBHOOK_SECRET.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, sig_v1)


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request, stripe_signature: Optional[str] = Header(default=None, alias="Stripe-Signature")):
    try:
        raw_body = await request.body()
        if not _verify_stripe_signature(raw_body, stripe_signature or ""):
            log("[STRIPE CALLBACK] assinatura inválida")
            return {"ok": False}

        event = json.loads(raw_body.decode("utf-8"))
        event_type = str(event.get("type") or "")
        obj = (event.get("data") or {}).get("object") or {}
        session_id = str(obj.get("id") or "")
        payment_status = str(obj.get("payment_status") or obj.get("status") or "")

        # mapeia user_id por prioridade
        metadata = obj.get("metadata") or {}
        user_id_str = str(
            obj.get("client_reference_id")
            or metadata.get("user_id")
            or ""
        ).strip()
        event_id = str(metadata.get("event_id") or "")

        if not user_id_str and session_id:
            user_id_str = get_redis().hget("tg:pix:identifier_map", session_id) or ""
        if not user_id_str and event_id:
            user_id_str = get_redis().hget("tg:pix:identifier_map", event_id) or ""
        if not user_id_str:
            log("[STRIPE CALLBACK] user_id ausente", {"session_id": session_id, "event_type": event_type})
            return {"ok": True}

        user_id = int(user_id_str)
        status = _map_gateway_status(payment_status)
        if event_type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
            status = "OK"
        elif event_type in ("checkout.session.expired", "checkout.session.async_payment_failed"):
            status = "FAILED"

        r = get_redis()
        pix_key = f"tg:pix:{user_id}"
        r.hset(
            pix_key,
            mapping={
                "status": status,
                "transaction_id": session_id,
                "payment_code": session_id,
                "stripe_session_id": session_id,
                "identifier": event_id or (r.hget(pix_key, "identifier") or ""),
                "gateway": "stripe",
            },
        )

        # map session/event ids for future callbacks
        try:
            if session_id:
                r.hset("tg:pix:identifier_map", session_id, str(user_id))
            if event_id:
                r.hset("tg:pix:identifier_map", event_id, str(user_id))
        except Exception:
            pass

        from .pix_payment import mark_payment_confirmed
        from .campaign import mark_paid
        from .tracking import get_utms, send_facebook_event, send_to_utmify_order
        from .config import (
            BOT_TOKEN,
            DEFAULT_CLIENT_DOCUMENT,
            DEFAULT_CLIENT_EMAIL,
            DEFAULT_CLIENT_NAME,
            DEFAULT_CLIENT_PHONE,
        )

        if status == "OK":
            mark_payment_confirmed(user_id)
            mark_paid(user_id)
            try:
                redis.srem(PIX_PENDING_SET, str(user_id))
            except Exception:
                pass
            record_funnel_event("payment_confirmed", user_id=user_id)

            # Deliver access key to user immediately via Telegram
            try:
                from .access_delivery import deliver_access_if_needed
                from aiogram import Bot
                _bot = Bot(BOT_TOKEN)
                chat_id_str = r.hget(f"tg:user:{user_id}", "chat_id")
                if chat_id_str:
                    await deliver_access_if_needed(_bot, user_id, int(chat_id_str))
                else:
                    log("[STRIPE CALLBACK] no chat_id for delivery", {"user_id": user_id})
                await _bot.session.close()
            except Exception as e:
                log("[STRIPE CALLBACK] DELIVERY ERRO", type(e).__name__, str(e))

            # Tracking paid (UTMify + Facebook CAPI)
            try:
                utms = get_utms(user_id)
                amount_total = int(obj.get("amount_total") or 0)
                amount = (amount_total / 100.0) if amount_total > 0 else float(r.hget(pix_key, "amount") or "0")
                order_id = str(r.hget(pix_key, "identifier") or event_id or session_id or "")
                customer_payload = {
                    "name": str((obj.get("customer_details") or {}).get("name") or DEFAULT_CLIENT_NAME or ""),
                    "email": str((obj.get("customer_details") or {}).get("email") or DEFAULT_CLIENT_EMAIL or ""),
                    "phone": str((obj.get("customer_details") or {}).get("phone") or DEFAULT_CLIENT_PHONE or ""),
                    "document": str(DEFAULT_CLIENT_DOCUMENT or ""),
                }
                await send_to_utmify_order(
                    order_id=order_id,
                    status="paid",
                    amount=amount,
                    customer=customer_payload,
                    utms=utms,
                    platform="Telegram-UK",
                    payment_method="credit_card",
                )
                await send_facebook_event(
                    event_name="Purchase",
                    event_id=order_id,
                    amount=amount,
                    currency="GBP",
                    customer=customer_payload,
                    utms=utms,
                )
            except Exception as e:
                log("[STRIPE CALLBACK] TRACKING ERRO", type(e).__name__, str(e))
        else:
            if status in ("PENDING", "WAITING_PAYMENT"):
                record_funnel_event("payment_pending", user_id=user_id)
            else:
                try:
                    redis.srem(PIX_PENDING_SET, str(user_id))
                except Exception:
                    pass
                record_funnel_event("payment_failed", user_id=user_id, gateway_status=status)

        return {"ok": True}
    except Exception as e:
        log("[STRIPE CALLBACK] ERRO", type(e).__name__, str(e))
        return {"ok": True}


@app.post("/mangofy/callback")
@app.get("/mangofy/callback")
@app.head("/mangofy/callback")
async def mangofy_callback(request: Request):
    """
    Webhook da Mangofy para notificar alterações de status de pagamento.
    Formato: {"payment_code": "...", "external_code": "...", "payment_status": "approved|pending|refunded|error", ...}
    """
    try:
        if request.method != "POST":
            return {"received": True}

        data = await request.json()
        log("[MANGOFY CALLBACK] RECEBIDO", json.dumps(data)[:2000])

        payment_code = data.get("payment_code") or ""
        external_code = data.get("external_code") or ""
        status_raw = data.get("payment_status") or ""
        customer = data.get("customer") or {}

        # metadata vem do extra.metadata da criação
        metadata = data.get("metadata") or {}
        # Mangofy às vezes aninha: metadata.metadata
        if isinstance(metadata.get("metadata"), dict):
            inner_meta = metadata["metadata"]
            # mescla inner_meta no metadata
            for k, v in inner_meta.items():
                if k not in metadata:
                    metadata[k] = v

        # Encontra user_id: metadata > identifier_map
        user_id_str = metadata.get("user_id")

        # fallback: busca por external_code no mapa identifier -> user_id
        if not user_id_str and external_code:
            try:
                user_id_str = get_redis().hget("tg:pix:identifier_map", external_code)
            except Exception:
                user_id_str = None

        # fallback: busca por payment_code no mapa
        if not user_id_str and payment_code:
            try:
                user_id_str = get_redis().hget("tg:pix:identifier_map", payment_code)
            except Exception:
                user_id_str = None

        if not user_id_str:
            log("[MANGOFY CALLBACK] user_id ausente", {
                "payment_code": payment_code,
                "external_code": external_code,
            })
            return {"received": True}

        user_id = int(user_id_str)

        # Importa aqui para evitar circular import
        from .pix_payment import mark_payment_confirmed
        from .campaign import mark_paid
        from .tracking import get_utms, send_facebook_event, send_to_utmify_order
        from .config import (
            DEFAULT_CLIENT_DOCUMENT,
            DEFAULT_CLIENT_EMAIL,
            DEFAULT_CLIENT_NAME,
            DEFAULT_CLIENT_PHONE,
        )

        r = get_redis()
        pix_key = f"tg:pix:{user_id}"

        # Normaliza status (Mangofy: approved, pending, refunded, error)
        status = _map_gateway_status(str(status_raw))

        # Atualiza status no Redis
        r.hset(pix_key, "status", status)
        if external_code:
            r.hset(pix_key, "identifier", external_code)
        if payment_code:
            r.hset(pix_key, mapping={"transaction_id": payment_code, "payment_code": payment_code})

        # Se for uma cobrança de upsell, mantém status sincronizado por access_key.
        identifier = external_code or payment_code
        if identifier:
            try:
                access_key = r.hget(UPSELL_IDENTIFIER_MAP_KEY, identifier)
                if access_key:
                    up_key = _upsell_key(access_key)
                    mapping = {
                        "status": status,
                        "updated_at": str(int(datetime.utcnow().timestamp())),
                        "transaction_id": str(payment_code or ""),
                    }
                    if status == "OK":
                        mapping["paid_at"] = str(int(datetime.utcnow().timestamp()))
                    r.hset(up_key, mapping=mapping)
                    _upsell_event(
                        "upsell_callback_status",
                        {
                            "key": access_key,
                            "user_id": user_id,
                            "identifier": identifier,
                            "status": status,
                        },
                    )
            except Exception:
                pass

        if status == "OK":
            mark_payment_confirmed(user_id)
            mark_paid(user_id)
            try:
                redis.srem(PIX_PENDING_SET, str(user_id))
            except Exception:
                pass
            log("[MANGOFY CALLBACK] STATUS OK", {"user_id": user_id, "payment_code": payment_code})
            record_funnel_event("payment_confirmed", user_id=user_id)

            # Tracking: UTMify + Facebook CAPI
            try:
                utms = get_utms(user_id)
                # Prioriza utms vindos do webhook
                webhook_utms = metadata.get("utms")
                if webhook_utms and isinstance(webhook_utms, dict):
                    utms = webhook_utms

                # Mangofy envia amount em centavos — converter para reais
                amount_str = r.hget(pix_key, "amount") or "0"
                amount = float(amount_str)
                if amount == 0:
                    raw_cents = data.get("payment_amount") or data.get("sale_amount") or 0
                    if raw_cents and int(raw_cents) > 100:
                        amount = int(raw_cents) / 100.0

                order_id = r.hget(pix_key, "identifier") or external_code or payment_code or ""
                customer_payload = {
                    "name": customer.get("name") or DEFAULT_CLIENT_NAME or "",
                    "email": customer.get("email") or DEFAULT_CLIENT_EMAIL or "",
                    "phone": str(customer.get("phone") or DEFAULT_CLIENT_PHONE or ""),
                    "document": str(customer.get("document") or DEFAULT_CLIENT_DOCUMENT or ""),
                }
                await send_to_utmify_order(
                    order_id=order_id,
                    status="paid",
                    amount=amount,
                    customer=customer_payload,
                    utms=utms,
                )
                await send_facebook_event(
                    event_name="Purchase",
                    event_id=order_id,
                    amount=amount,
                    currency="BRL",
                    customer=customer_payload,
                    utms=utms,
                )
            except Exception as e:
                log("[MANGOFY CALLBACK] TRACKING ERRO", type(e).__name__, str(e))
        else:
            log("[MANGOFY CALLBACK] STATUS", status, {"user_id": user_id, "payment_code": payment_code})
            if status == "PENDING":
                record_funnel_event("payment_pending", user_id=user_id)
            else:
                try:
                    redis.srem(PIX_PENDING_SET, str(user_id))
                except Exception:
                    pass
                record_funnel_event("payment_failed", user_id=user_id, gateway_status=status)

        return {"received": True}
    except Exception as e:
        log("[MANGOFY CALLBACK] ERRO", type(e).__name__, str(e))
        return {"received": True, "error": str(e)}


# Legacy: mantém endpoint Amplopay para transações antigas em andamento
@app.post("/amplopay/callback")
@app.get("/amplopay/callback")
@app.head("/amplopay/callback")
async def amplopay_callback_legacy(request: Request):
    """Redireciona para o handler Mangofy (compatibilidade)."""
    return await mangofy_callback(request)
