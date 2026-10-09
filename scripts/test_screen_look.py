"""Тест «посмотри на экран» и кадра с камеры (app/features/screen_look.py,
_prepare_turn_input в app/api/server.py):

  - просьбы посмотреть на экран узнаются (RU/EN), посторонние фразы и
    экран телефона — нет;
  - без разрешения macOS на запись экрана — понятный отказ и системный
    запрос; снимок ужимается до MAX_SIDE и уходит JPEG;
  - описание — только локальная модель: нет её — отказ, а не облако;
  - ход веба: обычная картинка — каскад как раньше, кадр камеры и экран —
    только локально, в истории «📷 …»/«🖥 …»; из скина и в режиме
    управления экран не снимается.

Снимок экрана и модели не вызываются — всё подменено.
Запуск: PYTHONPATH=. python3 -m scripts.test_screen_look
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["VPC_DATA_DIR"] = tempfile.mkdtemp(prefix="screen_look_")
os.environ["OLLAMA_URL"] = "http://127.0.0.1:9"

ok = 0
failures = 0


def check(name, cond):
    global ok, failures
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok += 1
    if not cond:
        failures += 1


def section(title):
    print(f"\n── {title} ──")


class FakeLocal:
    def __init__(self, answer="TEXT: Traceback\nDESCRIPTION: терминал с ошибкой", available=True):
        self.answer = answer
        self._available = available
        self.calls = []

    def ocr_image(self, image_bytes, question="", lang=None, subject="image"):
        self.calls.append({"bytes": image_bytes, "question": question, "lang": lang, "subject": subject})
        return self.answer


def test_detect():
    section("A. Узнаём просьбу посмотреть на экран")
    from app.features.screen_look import is_screen_request
    yes = [
        "посмотри на экран",
        "Посмотри на мой экран, что за ошибка?",
        "глянь, пожалуйста, на экран",
        "можешь посмотреть на экран?",
        "взгляни на монитор",
        "что у меня на экране?",
        "что сейчас на экране",
        "что ты видишь на моём экране",
        "посмотри на рабочий стол",
        "сделай скриншот",
        "look at my screen",
        "can you check the screen?",
        "what's on my screen?",
        "what’s on my screen?",
        "check the screen, what is this error?",
        "take a screenshot please",
    ]
    no = [
        "привет, как дела",
        "я долго смотрел на экран и устал",
        "экран у ноутбука яркий",
        "посмотри на экран телефона",
        "посмотри фильм про экраны",
        "напомни через 2 минуты проверить уведомления",
        "what a nice screen saver",
        "",
    ]
    bad_yes = [t for t in yes if not is_screen_request(t)]
    bad_no = [t for t in no if is_screen_request(t)]
    check(f"просьбы узнаны ({len(yes) - len(bad_yes)}/{len(yes)}) {bad_yes or ''}", not bad_yes)
    check(f"посторонние фразы не тронуты {bad_no or ''}", not bad_no)


def test_capture():
    section("B. Снимок экрана: разрешение и размер")
    from PIL import Image
    from app.features import screen_look

    asked = []
    with mock.patch.object(screen_look, "screen_permission", return_value=False), \
            mock.patch.object(screen_look, "_request_permission", side_effect=lambda: asked.append(1)):
        try:
            screen_look.capture_screen()
            got = None
        except screen_look.ScreenLookError as e:
            got = str(e)
    check("нет разрешения — отказ с подсказкой, где включить",
          got == screen_look.NO_PERMISSION and "Запись экрана" in got)
    check("…и системный запрос разрешения", asked == [1])

    big = Image.new("RGB", (3456, 2234), (30, 60, 90))
    with mock.patch.object(screen_look, "screen_permission", return_value=True), \
            mock.patch("PIL.ImageGrab.grab", return_value=big):
        data = screen_look.capture_screen()
    img = Image.open(__import__("io").BytesIO(data))
    check("снимок — JPEG", img.format == "JPEG")
    check(f"длинная сторона ужата до {screen_look.MAX_SIDE} с сохранением пропорций",
          max(img.size) == screen_look.MAX_SIDE and abs(img.size[0] / img.size[1] - 3456 / 2234) < 0.01)

    with mock.patch.object(screen_look, "screen_permission", return_value=None), \
            mock.patch("PIL.ImageGrab.grab", side_effect=OSError("no display")):
        try:
            screen_look.capture_screen()
            got = None
        except screen_look.ScreenLookError as e:
            got = str(e)
    check("снимок не удался (нет дисплея) — понятный отказ", got is not None and "no display" in got)


def test_describe():
    section("C. Описание — только локальной моделью")
    from app.features import screen_look

    local = FakeLocal()
    with mock.patch.object(screen_look, "capture_screen", return_value=b"jpeg-bytes"):
        text = screen_look.screen_input(local, "посмотри на экран, что за ошибка?", "ru")
    check("снимок ушёл в локальную модель как экран",
          local.calls and local.calls[0]["subject"] == "screen" and local.calls[0]["bytes"] == b"jpeg-bytes")
    check("вопрос и язык переданы", local.calls[0]["question"].startswith("посмотри") and local.calls[0]["lang"] == "ru")
    check("текст хода: просьба + пометка + описание",
          text.startswith("посмотри на экран") and screen_look.SCREEN_NOTE in text and "Traceback" in text)

    for router, expected, name in [
        (None, screen_look.NO_LOCAL_MODEL, "локального роутера нет — «Ollama недоступна»"),
        (FakeLocal(answer=None, available=False), screen_look.NO_LOCAL_MODEL, "Ollama не отвечает — «Ollama недоступна»"),
        (FakeLocal(answer=None, available=True), screen_look.LOCAL_FAILED, "модель есть, но не ответила — «попробуйте ещё раз»"),
    ]:
        try:
            screen_look.describe_locally(router, b"x", "", None, "image")
            got = None
        except screen_look.ScreenLookError as e:
            got = str(e)
        check(name, got == expected)


def test_turn_input():
    section("D. Ход веба: картинка, камера, экран")
    from fastapi import HTTPException
    from app.api import server
    from app.api.schemas import ChatRequest
    from app.features import screen_look

    def make_bot(local=None, control=False):
        bot = mock.MagicMock()
        bot._local_router = local
        bot.control_mode_on.return_value = control
        bot.describe_image.return_value = "TEXT: нет\nDESCRIPTION: кот (облако)"
        bot.chat_user_language.return_value = "ru"
        return bot

    img = "data:image/jpeg;base64,/9j/AAAA"

    bot = make_bot(local=FakeLocal())
    llm, shown = server._prepare_turn_input(bot, ChatRequest(persona="connor", message="что это?", image=img))
    check("обычная картинка — каскад через vision основного роутера (как раньше)",
          bot.describe_image.called and "кот (облако)" in llm and shown == "📷 что это?")

    local = FakeLocal(answer="TEXT: нет\nDESCRIPTION: чашка кофе")
    bot = make_bot(local=local)
    llm, shown = server._prepare_turn_input(
        bot, ChatRequest(persona="connor", message="", image=img, image_source="camera"))
    check("кадр камеры — облачный vision не вызывается", not bot.describe_image.called)
    check("…смотрит локальная модель", local.calls and local.calls[0]["subject"] == "image")
    check("…в ходе — пометка камеры и описание, в истории «📷»",
          screen_look.CAMERA_NOTE in llm and "чашка кофе" in llm and shown == "📷 (изображение)")

    bot = make_bot(local=None)
    try:
        server._prepare_turn_input(bot, ChatRequest(persona="connor", message="", image=img, image_source="camera"))
        err = None
    except HTTPException as e:
        err = e
    check("кадр камеры без локальной модели — 503 «Ollama недоступна», а не облако",
          err is not None and err.status_code == 503 and err.detail == screen_look.NO_LOCAL_MODEL
          and not bot.describe_image.called)

    local = FakeLocal()
    bot = make_bot(local=local)
    with mock.patch.object(screen_look, "capture_screen", return_value=b"shot") as cap:
        llm, shown = server._prepare_turn_input(
            bot, ChatRequest(persona="connor", message="Посмотри на экран, что за ошибка?"))
    check("«посмотри на экран» — снимок и локальное описание",
          cap.called and local.calls and local.calls[0]["subject"] == "screen")
    check("…в ходе — пометка экрана, в истории «🖥 …»",
          screen_look.SCREEN_NOTE in llm and shown == "🖥 Посмотри на экран, что за ошибка?")
    check("…облачный vision не вызывается", not bot.describe_image.called)

    with mock.patch.object(screen_look, "capture_screen", side_effect=screen_look.ScreenLookError(screen_look.NO_PERMISSION)):
        try:
            server._prepare_turn_input(make_bot(local=FakeLocal()), ChatRequest(persona="connor", message="посмотри на экран"))
            err = None
        except HTTPException as e:
            err = e
    check("нет разрешения на запись экрана — 503 с подсказкой",
          err is not None and err.status_code == 503 and "Запись экрана" in err.detail)

    for req, bot, name in [
        (ChatRequest(persona="connor", message="посмотри на экран", from_skin=True), make_bot(local=FakeLocal()),
         "из скина экран не снимается (скин прочитал бы описание)"),
        (ChatRequest(persona="connor", message="посмотри на экран"), make_bot(local=FakeLocal(), control=True),
         "в режиме управления — обычный ход (экран — страница агента)"),
        (ChatRequest(persona="connor", message="привет"), make_bot(local=FakeLocal()), "обычная реплика — как есть"),
    ]:
        with mock.patch.object(screen_look, "capture_screen", return_value=b"shot") as cap:
            llm, shown = server._prepare_turn_input(bot, req)
        check(name, not cap.called and llm == req.message and shown is None)


def main():
    test_detect()
    test_capture()
    test_describe()
    test_turn_input()
    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
