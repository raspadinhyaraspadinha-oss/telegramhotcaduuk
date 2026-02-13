from redis import Redis

from .config import REDIS_URL

# Single shared Redis connection (decode_responses=True => str in/out)
redis = Redis.from_url(REDIS_URL, decode_responses=True)

