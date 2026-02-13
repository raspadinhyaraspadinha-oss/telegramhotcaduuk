import json
import time
from datetime import datetime
from typing import Any

from .redis_client import redis

FUNNEL_EVENTS_KEY = "tg:funnel:events"
FUNNEL_COUNTERS_KEY = "tg:funnel:counters"
FUNNEL_DAY_PREFIX = "tg:funnel:day:"


def _day_key(ts: int) -> str:
    day = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
    return f"{FUNNEL_DAY_PREFIX}{day}"


def record_funnel_event(event: str, user_id: int | None = None, amount: float | None = None, **extra: Any) -> None:
    ts = int(time.time())
    payload: dict[str, Any] = {
        "ts": ts,
        "event": event,
    }
    if user_id is not None:
        payload["user_id"] = int(user_id)
    if amount is not None:
        payload["amount"] = float(amount)
    if extra:
        payload.update(extra)

    raw = json.dumps(payload, ensure_ascii=False)
    try:
        redis.lpush(FUNNEL_EVENTS_KEY, raw)
        redis.ltrim(FUNNEL_EVENTS_KEY, 0, 1999)
    except Exception:
        pass

    try:
        redis.hincrby(FUNNEL_COUNTERS_KEY, "events_total", 1)
        redis.hincrby(FUNNEL_COUNTERS_KEY, event, 1)
        redis.hincrby(_day_key(ts), "events_total", 1)
        redis.hincrby(_day_key(ts), event, 1)
        redis.expire(_day_key(ts), 60 * 24 * 60 * 60)  # 60 days
    except Exception:
        pass


def get_funnel_counters() -> dict[str, str]:
    try:
        return redis.hgetall(FUNNEL_COUNTERS_KEY) or {}
    except Exception:
        return {}


def get_day_counters_utc() -> dict[str, str]:
    try:
        return redis.hgetall(_day_key(int(time.time()))) or {}
    except Exception:
        return {}

