import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BASE_URL = os.getenv("BASE_URL", "")  # ex: https://seu-servico.up.railway.app
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change-me")

# Valkey/Redis (Railway costuma fornecer REDIS_URL / VALKEY_URL dependendo do plugin)
REDIS_URL = (
    os.getenv("REDIS_URL")
    or os.getenv("VALKEY_URL")
    or os.getenv("UPSTASH_REDIS_REST_URL")  # se for outro
    or ""
)

QUEUE_KEY = os.getenv("QUEUE_KEY", "tg:updates")

# Mangofy Pix Gateway (configure via variáveis de ambiente)
MANGOFY_API_URL = os.getenv("MANGOFY_API_URL", "https://checkout.mangofy.com.br/api/v1")
MANGOFY_AUTHORIZATION = os.getenv("MANGOFY_AUTHORIZATION", "")
MANGOFY_STORE_CODE_HEADER = os.getenv("MANGOFY_STORE_CODE_HEADER", "")
MANGOFY_STORE_CODE_BODY = os.getenv("MANGOFY_STORE_CODE_BODY", "")
MANGOFY_POSTBACK_URL = os.getenv("MANGOFY_POSTBACK_URL", "")  # URL do webhook: https://SEU-DOMINIO/mangofy/callback

# Stripe Checkout (UK)
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_CURRENCY = os.getenv("STRIPE_CURRENCY", "gbp")
STRIPE_SUCCESS_URL = os.getenv("STRIPE_SUCCESS_URL", "")
STRIPE_CANCEL_URL = os.getenv("STRIPE_CANCEL_URL", "")

# Dados padrão do pagador (use SOMENTE dados do próprio cliente/pagador).
# Se ficar vazio, o sistema usa nome do Telegram + CPF/telefone gerados/normalizados.
DEFAULT_CLIENT_NAME = os.getenv("DEFAULT_CLIENT_NAME", "")
DEFAULT_CLIENT_EMAIL = os.getenv("DEFAULT_CLIENT_EMAIL", "")
DEFAULT_CLIENT_PHONE = os.getenv("DEFAULT_CLIENT_PHONE", "")
DEFAULT_CLIENT_DOCUMENT = os.getenv("DEFAULT_CLIENT_DOCUMENT", "")

# Tracking / Pixels
UTMIFY_API_URL = os.getenv("UTMIFY_API_URL", "https://api.utmify.com.br/api-credentials/orders")
UTMIFY_API_TOKEN = os.getenv("UTMIFY_API_TOKEN", "fKoyOKl8UkZN9Y5r0OMXRaXHEVtcUvps0qdP")

FACEBOOK_PIXEL_ID = os.getenv("FACEBOOK_PIXEL_ID", "1430620985127388")
FACEBOOK_ACCESS_TOKEN = os.getenv("FACEBOOK_ACCESS_TOKEN", "")
FACEBOOK_GRAPH_API_URL = os.getenv("FACEBOOK_GRAPH_API_URL", "https://graph.facebook.com/v19.0")
FACEBOOK_TEST_EVENT_CODE = os.getenv("FACEBOOK_TEST_EVENT_CODE", "")

# URL pública do bot (para event_source_url no Facebook)
BOT_PUBLIC_URL = os.getenv("BOT_PUBLIC_URL", "https://t.me/vazadosblackmarket_bot")
# Link base para deep link do Telegram (usado no redirect de tracking)
BOT_DEEPLINK_BASE = os.getenv("BOT_DEEPLINK_BASE", "https://t.me/vazadosblackmarket_bot?start=")

# Pixel (browser) pageview
FACEBOOK_PIXEL_BROWSER_ID = os.getenv("FACEBOOK_PIXEL_BROWSER_ID", FACEBOOK_PIXEL_ID)

# Página de portal pós-pagamento
PORTAL_BASE_URL = os.getenv(
    "PORTAL_BASE_URL", "https://telegram-webhook-production-e13d.up.railway.app/portal"
)

# Dashboard admin (webhook service)
ADMIN_DASHBOARD_TOKEN = os.getenv("ADMIN_DASHBOARD_TOKEN", "")

# Worker performance tuning
WORKER_MAX_CONCURRENT_UPDATES = int(os.getenv("WORKER_MAX_CONCURRENT_UPDATES", "100"))
WORKER_QUEUE_BLPOP_TIMEOUT = int(os.getenv("WORKER_QUEUE_BLPOP_TIMEOUT", "1"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN não definido")
if not REDIS_URL:
    raise RuntimeError("REDIS_URL/VALKEY_URL não definido")
