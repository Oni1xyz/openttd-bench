"""Bound HTTP work before JSON/schema parsing and retain cancelled worker slots."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping

from starlette.responses import JSONResponse


async def bounded_thread(function, *args, executor=None):
    """Cancellation must not release admission while a blocking worker still runs."""
    future = asyncio.get_running_loop().run_in_executor(executor, function, *args)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(future)
            break
        except asyncio.CancelledError:
            cancelled = True
            if future.cancelled():
                raise
    if cancelled:
        raise asyncio.CancelledError
    return result


def _check_json(raw: bytes | bytearray, batch: bool) -> None:
    # Limit nesting before json.loads can recurse. Ignore brackets inside strings.
    depth = 0
    quoted = escaped = False
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            if depth > 64:
                raise ValueError("JSON is too deeply nested")
        elif byte in (93, 125):
            depth -= 1
    value = json.loads(raw)
    if batch and isinstance(value, dict) and isinstance(value.get("items"), list) and len(value["items"]) > 100:
        raise ValueError("batch exceeds 100 items")
    stack = [value]
    nodes = 0
    while stack:
        node = stack.pop()
        nodes += 1
        if nodes > 10000:
            raise ValueError("JSON exceeds node budget")
        children = node.values() if isinstance(node, dict) else node if isinstance(node, list) else ()
        if nodes + len(stack) + len(children) > 10000:
            raise ValueError("JSON exceeds node budget")
        stack.extend(children)


class RequestLimits:
    """Fixed token buckets; no attacker-controlled quota-map growth or queued admission."""

    def __init__(self, app, tokens: Mapping[str, int | str], *, max_body=1024 * 1024, slots=2, body_seconds=10):
        self.app = app
        self.tokens = dict(tokens)
        self.active = {role: 0 for role in tokens.values()}
        self.max_body, self.slots, self.body_seconds = max_body, slots, body_seconds

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in ("/health", "/api/docs"):
            return await self.app(scope, receive, send)
        headers = [v for k, v in scope["headers"] if k.lower() == b"authorization"]
        role = None
        if len(headers) == 1 and len(headers[0]) <= 512:
            text = headers[0].decode("latin1")
            if text.lower().startswith("bearer "):
                role = self.tokens.get(text[7:].strip())
        async def reject(status, detail):
            await JSONResponse({"detail": detail}, status_code=status)(scope, receive, send)
        if role is None or (scope["path"].startswith("/api/admin/") and role != "admin"):
            return await reject(401, "valid bearer token required")
        if self.active[role] >= self.slots:
            return await reject(429, "too many active requests for this seat")
        self.active[role] += 1
        try:
            raw = bytearray()
            try:
                async with asyncio.timeout(self.body_seconds):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        chunk = message.get("body", b"")
                        if len(raw) + len(chunk) > self.max_body:
                            return await reject(413, "request body too large")
                        raw.extend(chunk)
                        if not message.get("more_body", False):
                            break
            except TimeoutError:
                return await reject(408, "request body deadline exceeded")
            if raw:
                try:
                    _check_json(raw, scope["path"] == "/api/batch")
                except (ValueError, UnicodeError, RecursionError):
                    return await reject(400, "invalid or over-budget JSON")
            sent = False
            async def replay():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": bytes(raw), "more_body": False}
                return await receive()
            await self.app(scope, replay, send)
        finally:
            self.active[role] -= 1
