from redis import ConnectionPool, Redis

from .config import REDIS_URL

# Thread-safe connection pool — handles hundreds of concurrent operations
# without bottlenecking on a single socket.
_pool = ConnectionPool.from_url(
    REDIS_URL,
    decode_responses=True,
    max_connections=50,
    socket_connect_timeout=5,
    socket_timeout=5,
    retry_on_timeout=True,
)
redis = Redis(connection_pool=_pool)

