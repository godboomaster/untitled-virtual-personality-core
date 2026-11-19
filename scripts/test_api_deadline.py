"""API: общий срок с heartbeat, один повтор, fallback и закрытие соединений.

Настоящий AsyncOpenAI + httpx.MockTransport: без сети, ключей и ожидания минуты.
Запуск: python3 -m scripts.test_api_deadline
"""

import asyncio
import json
import threading
import time
import unittest
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI

from app.core import api_request, router, turn_cancel


class SlowBody(httpx.AsyncByteStream):
    def __init__(self, prefix=b"", heartbeat=b" ", interval=0.005):
        self.prefix, self.heartbeat, self.interval = prefix, heartbeat, interval
        self.closed = False
        self.ticks = 0

    async def __aiter__(self):
        if self.prefix:
            yield self.prefix
        while True:
            await asyncio.sleep(self.interval)
            self.ticks += 1
            yield self.heartbeat

    async def aclose(self):
        self.closed = True


def answer(text="Ответ."):
    return httpx.Response(200, json={
        "id": "test", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
    })


def event(text):
    return ("data: " + json.dumps({
        "id": "test", "object": "chat.completion.chunk", "created": 0, "model": "m",
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    }) + "\n\n").encode()


class APIDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.calls, self.options, self.clients = [], [], []
        self.r = router.ModelRouter.__new__(router.ModelRouter)
        self.r.model_overrides, self.r._last_key_index, self.r._provider_sems = {}, {}, {}
        self.cfg = {"api_keys": ["test-key", "other-key"],
                    "base_url": "https://primary.invalid/v1", "model": "m"}
        self.r.available = {"primary": self.cfg,
                            "backup": {**self.cfg, "base_url": "https://backup.invalid/v1"}}
        self.r.active_provider = "primary"
        self.r.excluded = {"local"}
        self.r._get_full_order = lambda: ["primary", "backup"]
        self.handler = lambda request: answer()
        self.messages = [{"role": "user", "content": "Привет"}]

        async def dispatch(request):
            self.calls.append(request)
            result = self.handler(request)
            return await result if asyncio.iscoroutine(result) else result

        def factory(**kw):
            self.options.append(kw)
            http_client = httpx.AsyncClient(transport=httpx.MockTransport(dispatch))
            self.clients.append(http_client)
            return AsyncOpenAI(**kw, http_client=http_client)

        for target, value in (
            ("AsyncOpenAI", factory), ("internet_available", lambda: True),
            ("note_internet_suspect", lambda: None), ("note_internet_ok", lambda: None),
        ):
            p = patch.object(router, target, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(api_request, "API_ATTEMPT_TIMEOUT_SEC", 0.3)
        p.start()
        self.addCleanup(p.stop)

    def call(self, stream=False, timeout=60):
        if stream:
            return self.r.get_response_stream(self.messages, self.received.append, timeout=timeout)
        return self.r.get_response(self.messages, timeout=timeout)

    def assert_closed(self):
        self.assertTrue(all(c.is_closed for c in self.clients))

    def test_default_budget_is_60_seconds(self):
        with patch.object(api_request, "API_ATTEMPT_TIMEOUT_SEC", 60):
            self.assertEqual(self.call(timeout=150), "Ответ.")
        self.assertEqual(self.options[0]["timeout"], 60)
        self.assertEqual(self.options[0]["max_retries"], 0)
        self.assert_closed()

    def test_shorter_caller_budget_is_preserved(self):
        self.assertEqual(self.call(timeout=0.04), "Ответ.")
        self.assertEqual(self.options[0]["timeout"], 0.04)

    def test_heartbeat_does_not_extend_deadline_and_one_retry_succeeds(self):
        body = SlowBody()
        self.handler = lambda req: httpx.Response(200, stream=body) if len(self.calls) == 1 else answer()
        started = time.monotonic()
        self.assertEqual(self.call(), "Ответ.")
        self.assertLess(time.monotonic() - started, 1.3)
        self.assertGreater(body.ticks, 1)
        self.assertTrue(body.closed)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual([r.headers["authorization"] for r in self.calls],
                         ["Bearer test-key"] * 2)
        self.assertEqual(self.r._last_provider, "primary")
        self.assert_closed()

    def test_two_timeouts_fall_back_without_trying_more_keys(self):
        bodies = []

        def handler(req):
            if req.url.host == "backup.invalid":
                self.assertTrue(all(b.closed for b in bodies))
                return answer("Запасная модель.")
            body = SlowBody()
            bodies.append(body)
            return httpx.Response(200, stream=body)

        self.handler = handler
        started = time.monotonic()
        self.assertEqual(self.call(), "Запасная модель.")
        self.assertLess(time.monotonic() - started, 1.6)
        self.assertEqual([r.url.host for r in self.calls],
                         ["primary.invalid", "primary.invalid", "backup.invalid"])
        self.assertTrue(all(b.closed for b in bodies))
        self.assertEqual(self.r._last_provider, "backup")
        self.assert_closed()

    def test_waiting_for_headers_is_also_bounded(self):
        cancelled = []

        async def handler(req):
            if req.url.host == "backup.invalid":
                return answer()
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.append(True)

        self.handler = handler
        self.assertEqual(self.call(), "Ответ.")
        self.assertEqual(len(cancelled), 2)
        self.assertEqual(len(self.calls), 3)
        self.assert_closed()

    def test_sdk_read_timeout_gets_exactly_one_controlled_retry(self):
        def handler(req):
            if len(self.calls) == 1:
                raise httpx.ReadTimeout("no data", request=req)
            return answer()

        self.handler = handler
        self.assertEqual(self.call(), "Ответ.")
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all(o["max_retries"] == 0 for o in self.options))
        self.assert_closed()

    def test_forced_api_is_not_retried_again_in_fallback_chain(self):
        async def handler(req):
            if req.url.host == "backup.invalid":
                return answer()
            await asyncio.sleep(10)

        self.handler = handler
        self.assertEqual(self.r.get_response(self.messages, force_provider="primary"), "Ответ.")
        self.assertEqual([r.url.host for r in self.calls],
                         ["primary.invalid", "primary.invalid", "backup.invalid"])
        self.assert_closed()

    def test_sse_comments_do_not_extend_deadline(self):
        self.received = []
        bodies = []

        def handler(req):
            if req.url.host == "backup.invalid":
                return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                      content=event("Запасная.") + b"data: [DONE]\n\n")
            body = SlowBody(heartbeat=b": heartbeat\n\n")
            bodies.append(body)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)

        self.handler = handler
        self.assertEqual(self.call(stream=True), "Запасная.")
        self.assertEqual(self.received, ["Запасная."])
        self.assertEqual(len(self.calls), 3)
        self.assertTrue(all(b.closed and b.ticks > 1 for b in bodies))
        self.assert_closed()

    def test_partial_stream_is_returned_without_duplicates_or_retry(self):
        self.received = []
        body = SlowBody(prefix=event("Начало."), heartbeat=b": heartbeat\n\n")
        self.handler = lambda req: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=body)
        self.assertEqual(self.call(stream=True), "Начало.")
        self.assertEqual(self.received, ["Начало."])
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(body.closed)
        self.assertEqual(self.r._last_provider, "primary")
        self.assert_closed()

    def test_stop_cancels_silent_request_without_retry(self):
        body = SlowBody(interval=10)
        self.handler = lambda req: httpx.Response(200, stream=body)
        tok = turn_cancel.open_turn("deadline:stop")
        timer = threading.Timer(0.02, tok.cancel)
        try:
            with turn_cancel.scope(tok):
                timer.start()
                with self.assertRaises(turn_cancel.TurnCancelled):
                    self.call()
        finally:
            timer.join()
            turn_cancel.close_turn(tok)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(body.closed)
        self.assert_closed()

    def test_auth_errors_rotate_keys_without_timeout_retry(self):
        self.handler = lambda req: (httpx.Response(401, json={"error": {"message": "bad key"}})
                                    if len(self.calls) == 1 else answer())
        # Здесь проверяем ключи, а не скорость первой ленивой загрузки
        # схем SDK: она может занять больше укороченного тестового срока.
        with patch.object(api_request, "API_ATTEMPT_TIMEOUT_SEC", 60):
            self.assertEqual(self.call(), "Ответ.")
        self.assertEqual([r.headers["authorization"] for r in self.calls],
                         ["Bearer test-key", "Bearer other-key"])
        self.assertEqual(self.r._last_key_index["primary"], 1)
        self.assert_closed()

    def test_sync_router_remains_callable_inside_event_loop(self):
        async def run():
            return self.call()

        self.assertEqual(asyncio.run(run()), "Ответ.")
        self.assert_closed()


if __name__ == "__main__":
    unittest.main()
