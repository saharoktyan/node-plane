from __future__ import annotations

import logging
from time import perf_counter
from uuid import uuid4

import aiohttp

from .errors import ApiError

log = logging.getLogger(__name__)


class Client:
    """Requests to one API endpoint with one bearer credential.

    `on_request(method, path, started, request_id)` is called after every
    request, also a failed one, for the caller's timings.
    """

    def __init__(self, session: aiohttp.ClientSession, base_url: str, token: str, *, on_request=None):
        self.session = session
        self.base_url = base_url.rstrip('/')
        self.token = token
        self.on_request = on_request

    async def request(self, method: str, path: str, *, telegram_user_id: int | None = None,
                      body: dict | None = None, command: bool = False,
                      revision: int | None = None, command_key: str | None = None,
                      timeout_seconds: int | None = None, content: bytes | None = None,
                      binary: bool = False) -> dict | bytes:
        """One request. `telegram_user_id` acts for that person (adapter
        credentials only); `command` sends an `Idempotency-Key`, reusing
        `command_key` on retries; `revision` is the record's expected
        revision; `content` sends raw bytes and `binary` returns them."""
        started = perf_counter()
        request_id = '-'
        headers = {'Authorization': f'Bearer {self.token}'}
        if telegram_user_id is not None:
            headers['X-Node-Plane-Telegram-User-ID'] = str(telegram_user_id)
        if command:
            headers['Idempotency-Key'] = command_key or str(uuid4())
        if revision is not None:
            headers['If-Match'] = f'"{revision}"'
        options = {'headers': headers, 'json': body} if content is None else {'headers': headers, 'data': content}
        if timeout_seconds is not None:
            options['timeout'] = aiohttp.ClientTimeout(total=timeout_seconds)
        try:
            async with self.session.request(method, self.base_url + path, **options) as response:
                if binary and response.status < 400:
                    return await response.read()
                data = await response.json()
                request_id = response.headers.get('X-Request-ID', '-')
                if response.status >= 400:
                    error = data.get('error', {}) if isinstance(data, dict) else {}
                    code = error.get('code', 'backend_unavailable')
                    log.warning('API request failed: %s %s status=%s code=%s request_id=%s',
                                method, path.split('?')[0], response.status, code, request_id)
                    raise ApiError(code, response.status, request_id)
                return data
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise ApiError('backend_unavailable', 503) from exc
        finally:
            if self.on_request is not None:
                self.on_request(method, path, started, request_id)
