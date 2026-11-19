"""Синхронный вызов API с общим сроком, включая чтение тела и SSE heartbeat.

Сетевой read timeout SDK ограничивает только паузу между порциями данных.
Async-клиент позволяет отменить всё чтение и закрыть соединение по общему
сроку, не оставляя заблокированный HTTP-вызов в отдельном потоке.
"""

import asyncio
import contextvars
from concurrent.futures import Future
import threading

from app.core import turn_cancel

API_ATTEMPT_TIMEOUT_SEC = 60.0


class APIAttemptTimeout(TimeoutError):
    pass


def complete(client_factory, client_options: dict, request: dict,
             timeout: float, on_token=None) -> str | None:
    """Одна попытка без повторов SDK; callback выполняется при чтении SSE.

    Более короткий срок вызывающего сохраняется. Отмена хода прекращает
    и молчащий запрос; исключение не превращается в таймаут или fallback.
    """
    budget = min(float(timeout), API_ATTEMPT_TIMEOUT_SEC)
    tok = turn_cancel.current()

    async def generate():
        async with client_factory(**client_options, timeout=budget, max_retries=0) as client:
            if on_token is None:
                response = await client.chat.completions.create(**request)
                return response.choices[0].message.content
            stream = await client.chat.completions.create(**request, stream=True)
            parts = []
            async with stream:
                async for chunk in stream:
                    if tok is not None and tok.cancelled:
                        raise turn_cancel.TurnCancelled(tok.reason or "отменён")
                    if chunk.choices:
                        delta = chunk.choices[0].delta.content
                        if delta:
                            parts.append(delta)
                            on_token(delta)
            return "".join(parts) or None

    async def run():
        turn_cancel.check()
        task = asyncio.create_task(generate())
        try:
            async with asyncio.timeout(budget):
                while True:
                    done, _ = await asyncio.wait({task}, timeout=0.05)
                    turn_cancel.check()
                    if done:
                        return task.result()
        except TimeoutError as exc:
            raise APIAttemptTimeout(f"общий таймаут API {budget:g} с") from exc
        finally:
            if not task.done():
                task.cancel()
            # Забрать и исключение завершившейся задачи: отмена хода могла
            # совпасть с ошибкой HTTP или проверкой отмены внутри SSE.
            await asyncio.gather(task, return_exceptions=True)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(run())
    # Обычно роутер вызывается рабочим потоком. Синхронный вызов из уже
    # работающего event loop тоже допустим, как у прежнего sync SDK.
    result = Future()
    context = contextvars.copy_context()

    def worker():
        try:
            result.set_result(context.run(asyncio.run, run()))
        except BaseException as exc:
            result.set_exception(exc)

    thread = threading.Thread(target=worker, name="api-request")
    thread.start()
    try:
        return result.result()
    finally:
        thread.join()
