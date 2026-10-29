from __future__ import annotations
import asyncio
import threading

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import httpx
import pytest

from server.limits import RequestLimits, bounded_thread


def test_body_and_shape_limits_precede_handler():
    app = FastAPI()
    calls = []
    @app.post('/api/batch')
    async def handle(request: Request):
        calls.append(True)
        return await request.json()
    app.add_middleware(RequestLimits, tokens={'seat-token': 0}, max_body=1024)
    client = TestClient(app)
    auth = {'Authorization': 'Bearer seat-token'}
    assert client.post('/api/batch', content=b'x' * 1025, headers=auth).status_code == 413
    assert client.post('/api/batch', content='[' * 70 + '0' + ']' * 70, headers=auth).status_code == 400
    assert client.post('/api/batch', json={'items': [None] * 101}, headers=auth).status_code == 400
    assert client.post('/api/batch', json={'items': []}).status_code == 401
    assert calls == []
    assert client.post('/api/batch', json={'items': []}, headers=auth).status_code == 200
    assert calls == [True]


def test_special_headers_and_partial_body_do_not_bypass_limits():
    async def exercise():
        called = False
        async def downstream(scope, receive, send):
            nonlocal called
            called = True
        guard = RequestLimits(downstream, {'t': 0}, max_body=4, body_seconds=0.01)
        async def receive():
            await asyncio.sleep(0.1)
            return {'type': 'http.request', 'body': b'x', 'more_body': True}
        sent = []
        async def send(message):
            sent.append(message)
        scope = {'type': 'http', 'path': '/api/batch', 'method': 'POST',
                 'headers': [(b'authorization', b'Bearer t')]}
        await guard(scope, receive, send)
        assert sent[0]['status'] == 408 and not called
        sent.clear()
        scope['headers'] += [(b'authorization', b'Bearer t')]
        await guard(scope, receive, send)
        assert sent[0]['status'] == 401 and not called
    asyncio.run(exercise())


def test_cancelled_requests_hold_their_worker_slot():
    async def exercise():
        app = FastAPI()
        started = threading.Event()
        release = threading.Event()
        def work():
            started.set()
            assert release.wait(5)
            return {'done': True}
        @app.get('/api/block')
        async def block():
            return await bounded_thread(work)
        @app.get('/api/admin/status')
        async def status():
            return {'ready': True}
        app.add_middleware(RequestLimits, tokens={'seat': 0, 'admin': 'admin'}, slots=1)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://local') as client:
            headers = {'Authorization': 'Bearer seat'}
            task = asyncio.create_task(client.get('/api/block', headers=headers))
            try:
                assert await asyncio.to_thread(started.wait, 2)
                task.cancel()
                await asyncio.sleep(0)
                denied = await client.get('/api/block', headers=headers)
                assert denied.status_code == 429
                admin = await client.get('/api/admin/status', headers={'Authorization': 'Bearer admin'})
                assert admin.status_code == 200
            finally:
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
    asyncio.run(exercise())
