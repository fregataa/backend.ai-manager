from __future__ import annotations

from decimal import Decimal
import logging
import time
from typing import (
    Iterable,
    Final,
    Tuple,
)

from aiohttp import web
from aiotools import apartial
from aioredis import Redis
import attr

from ai.backend.common import redis
from ai.backend.common.logging import BraceStyleAdapter

from ..defs import REDIS_RLIM_DB
from .context import RootContext
from .exceptions import RateLimitExceeded
from .types import CORSOptions, WebRequestHandler, WebMiddleware

log = BraceStyleAdapter(logging.getLogger(__name__))

_time_prec: Final = Decimal('1e-3')  # msec
_rlim_window: Final = 60 * 15

# We implement rate limiting using a rolling counter, which prevents
# last-minute and first-minute bursts between the intervals.

_rlim_script = '''
local access_key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local request_id = tonumber(redis.call('INCR', '__request_id'))
if request_id >= 1e12 then
    redis.call('SET', '__request_id', 1)
end
if redis.call('EXISTS', access_key) == 1 then
    redis.call('ZREMRANGEBYSCORE', access_key, 0, now - window)
end
redis.call('ZADD', access_key, now, tostring(request_id))
redis.call('EXPIRE', access_key, window)
return redis.call('ZCARD', access_key)
'''


@web.middleware
async def rlim_middleware(
    app: web.Application,
    request: web.Request,
    handler: WebRequestHandler,
) -> web.StreamResponse:
    # This is a global middleware: request.app is the root app.
    app_ctx: PrivateContext = app['ratelimit.context']
    now = Decimal(time.time()).quantize(_time_prec)
    rr = app_ctx.redis_rlim
    if request['is_authorized']:
        rate_limit = request['keypair']['rate_limit']
        access_key = request['keypair']['access_key']
        ret = await redis.execute_script(
            rr, 'ratelimit', _rlim_script,
            [access_key],
            [str(now), str(_rlim_window)],
        )
        if ret is None:
            remaining = rate_limit
        else:
            rolling_count = int(ret)
            if rolling_count > rate_limit:
                raise RateLimitExceeded
            remaining = rate_limit - rolling_count
        response = await handler(request)
        response.headers['X-RateLimit-Limit'] = str(rate_limit)
        response.headers['X-RateLimit-Remaining'] = str(remaining)
        response.headers['X-RateLimit-Window'] = str(_rlim_window)
        return response
    else:
        # No checks for rate limiting for non-authorized queries.
        response = await handler(request)
        response.headers['X-RateLimit-Limit'] = '1000'
        response.headers['X-RateLimit-Remaining'] = '1000'
        response.headers['X-RateLimit-Window'] = str(_rlim_window)
        return response


@attr.s(slots=True, auto_attribs=True, init=False)
class PrivateContext:
    redis_rlim: Redis
    redis_rlim_script: str


async def init(app: web.Application) -> None:
    root_ctx: RootContext = app['_root.context']
    app_ctx: PrivateContext = app['ratelimit.context']
    rr = await redis.connect_with_retries(
        str(root_ctx.shared_config.get_redis_url(db=REDIS_RLIM_DB)),
        timeout=3.0,
        encoding='utf8',
    )
    app_ctx.redis_rlim = rr
    app_ctx.redis_rlim_script = await rr.script_load(_rlim_script)


async def shutdown(app: web.Application) -> None:
    app_ctx: PrivateContext = app['ratelimit.context']
    try:
        await app_ctx.redis_rlim.flushdb()
    except (ConnectionResetError, ConnectionRefusedError):
        pass
    app_ctx.redis_rlim.close()
    await app_ctx.redis_rlim.wait_closed()


def create_app(default_cors_options: CORSOptions) -> Tuple[web.Application, Iterable[WebMiddleware]]:
    app = web.Application()
    app['api_versions'] = (1, 2, 3, 4)
    app['ratelimit.context'] = PrivateContext()
    app.on_startup.append(init)
    app.on_shutdown.append(shutdown)
    # middleware must be wrapped by web.middleware at the outermost level.
    return app, [web.middleware(apartial(rlim_middleware, app))]
