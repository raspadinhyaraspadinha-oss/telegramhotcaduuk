import secrets
import time
from typing import Any, Dict, Optional

from .redis_client import redis
from .tracking import get_utms

KEY_PREFIX = "tg:portal:key:"
KEY_TTL_SECONDS = 7 * 24 * 60 * 60
DEV_FIXED_KEYS = {"testeupsell"}


def _key_redis(access_key: str) -> str:
    return f"{KEY_PREFIX}{access_key}"


def generate_access_key(user_id: int) -> str:
    return secrets.token_urlsafe(10)


def save_access_key(access_key: str, user_id: int) -> None:
    data = {
        "user_id": str(user_id),
        "created_at": str(int(time.time())),
    }
    redis.hset(_key_redis(access_key), mapping=data)
    redis.expire(_key_redis(access_key), KEY_TTL_SECONDS)


def get_access_info(access_key: str) -> Optional[Dict[str, Any]]:
    if access_key in DEV_FIXED_KEYS:
        return {"user_id": 0, "created_at": str(int(time.time()))}
    data = redis.hgetall(_key_redis(access_key))
    if not data:
        return None
    user_id = data.get("user_id")
    return {
        "user_id": int(user_id) if user_id and user_id.isdigit() else None,
        "created_at": data.get("created_at"),
    }


def get_access_utms(access_key: str) -> Dict[str, str]:
    info = get_access_info(access_key)
    if not info or not info.get("user_id"):
        return {}
    return get_utms(info["user_id"])
