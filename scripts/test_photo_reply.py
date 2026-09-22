"""Тест _reply_with_photos: скриншот уходит ВМЕСТЕ с ответом (подпись кадра),
при длинном тексте — прежний путь (текст отдельно, фото следом).
Telegram API — фейковый message. Запуск: python -m scripts.test_photo_reply"""

import asyncio
import sys
from types import SimpleNamespace


class FakeMessage:
    def __init__(self):
        self.calls = []
        self.chat = SimpleNamespace(send_action=lambda *a, **kw: asyncio.sleep(0))

    async def reply_photo(self, photo=None, caption=None, parse_mode=None):
        self.calls.append(("photo", photo, caption, parse_mode))
        return SimpleNamespace(message_id=len(self.calls))

    async def reply_text(self, text, parse_mode=None):
        self.calls.append(("text", text, parse_mode))
        return SimpleNamespace(message_id=100 + len(self.calls))


def main():
    ok = 0

    def check(name, cond):
        nonlocal ok
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        ok = ok + 1 if cond else ok - 100

    from app import telegram_bot as tb
    tb._send_delay = lambda part: 0  # без пауз «набора»

    PH = [{"data": b"jpeg1", "caption": "Так выглядит страница (x.com)"},
          {"data": b"jpeg2", "caption": "второй кадр"}]

    # 1. Короткий ответ + кадр → одно сообщение: фото с текстом в подписи
    m = FakeMessage()
    ids = asyncio.run(tb._reply_with_photos(m, "Готово, открыл ютуб.", PH[:1]))
    check("короткий: фото с подписью-ответом, текст отдельно не шлём",
          len(m.calls) == 1 and m.calls[0][0] == "photo"
          and "Готово, открыл ютуб." in (m.calls[0][2] or "")
          and m.calls[0][3] == "HTML" and ids)

    # 2. Два кадра: первый несёт ответ, второй — отдельно со своей подписью
    m = FakeMessage()
    asyncio.run(tb._reply_with_photos(m, "Вот страница.", PH))
    kinds = [c[0] for c in m.calls]
    check("два кадра: ответ на первом, второй отдельным",
          kinds == ["photo", "photo"]
          and "Вот страница." in (m.calls[0][2] or "")
          and m.calls[1][2] == "второй кадр")

    # 3. Длинный ответ (>1024 после HTML) → текст отдельно, кадр со своей подписью
    m = FakeMessage()
    long_text = "Текст ответа. " * 200
    asyncio.run(tb._reply_with_photos(m, long_text, PH[:1]))
    kinds = [c[0] for c in m.calls]
    check("длинный: текст отдельно, кадр следом со своей подписью",
          kinds[0] == "text" and "photo" in kinds
          and m.calls[-1][2] == "Так выглядит страница (x.com)")

    # 4. Кадр без текста — просто фото
    m = FakeMessage()
    asyncio.run(tb._reply_with_photos(m, "", PH[:1]))
    check("без текста: кадр уходит со своей подписью",
          len(m.calls) == 1 and m.calls[0][0] == "photo")

    # 5. Текст без кадров — обычный ответ
    m = FakeMessage()
    asyncio.run(tb._reply_with_photos(m, "Обычный ответ.", []))
    check("без кадров: обычный текст", m.calls and m.calls[0][0] == "text")

    print(f"\nИтог: {ok} проверок")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
