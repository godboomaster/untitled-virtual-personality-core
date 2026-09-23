"""Тест: разрыв соединения клиента не обрывает генерацию ответа.

Инцидент: пока персона генерировала ответ (~50 с, webchat:deepseek), страницу
перезагрузили. Starlette отменил SSE-генератор, CancelledError прилетел в
await asyncio.to_thread(...), finally сразу снял флаг «генерирует» и отпустил
лок чата — а поток ещё секунды дописывал ответ в STM. Новая страница прочитала
историю без ответа и перехода флага true→false уже не увидела: ответ так и не
показался. Проверяем server._run_generation:

  - отмена вызывающего не снимает флаг и лок, пока fn не вернулся;
  - после завершения fn флаг снят, лок свободен;
  - два запроса в один чат: флаг держится, пока не закончат оба (счётчик);
  - сообщения одного чата сериализуются;
  - ошибка fn доходит до вызывающего и снимает флаг.

Запуск: PYTHONPATH=. python3 scripts/test_chat_generation.py
"""

import asyncio
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

ok = 0
failures = 0


def check(name, cond):
    global ok, failures
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok += 1
    if not cond:
        failures += 1


async def _cancel_keeps_flag(server, chat_lock):
    key = "test_persona:cancel_chat"
    done = threading.Event()

    def fn():
        time.sleep(0.5)
        done.set()
        return "ответ"

    caller = asyncio.create_task(server._run_generation(key, fn))
    await asyncio.sleep(0.1)
    check("флаг поднят во время генерации", key in server._generating)
    caller.cancel()  # клиент перезагрузил страницу
    try:
        await caller
    except asyncio.CancelledError:
        pass
    check("после отмены вызывающего флаг ещё поднят", key in server._generating)
    check("после отмены вызывающего лок чата ещё занят", chat_lock(key).locked())
    check("поток ещё не закончил", not done.is_set())
    for _ in range(50):
        if key not in server._generating:
            break
        await asyncio.sleep(0.05)
    check("поток доработал до конца", done.is_set())
    check("флаг снят только после завершения потока", key not in server._generating)
    check("лок чата освобождён", not chat_lock(key).locked())


async def _counter_and_serialization(server):
    key = "test_persona:queue_chat"
    order = []

    def make(tag, delay):
        def fn():
            order.append(f"{tag}:start")
            time.sleep(delay)
            order.append(f"{tag}:end")
            return tag
        return fn

    t1 = asyncio.create_task(server._run_generation(key, make("a", 0.3)))
    await asyncio.sleep(0.05)
    t2 = asyncio.create_task(server._run_generation(key, make("b", 0.1)))
    r1 = await t1
    check("первый ответ вернулся", r1 == "a")
    check("флаг держится, пока второй запрос в работе", key in server._generating)
    r2 = await t2
    check("второй ответ вернулся", r2 == "b")
    check("запросы одного чата не пересекаются",
          order == ["a:start", "a:end", "b:start", "b:end"])
    check("флаг снят после обоих", key not in server._generating)


async def _error_propagates(server):
    key = "test_persona:error_chat"

    def fn():
        raise RuntimeError("boom")

    try:
        await server._run_generation(key, fn)
        raised = False
    except RuntimeError:
        raised = True
    check("ошибка генерации доходит до вызывающего", raised)
    check("флаг снят после ошибки", key not in server._generating)


def main():
    from app.api import server
    from app.api.runtime import chat_lock

    print("\n── server._run_generation ──")

    async def run_all():
        await _cancel_keeps_flag(server, chat_lock)
        await _counter_and_serialization(server)
        await _error_propagates(server)

    asyncio.run(run_all())
    print(f"\nИтого: {ok - failures}/{ok} OK")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
