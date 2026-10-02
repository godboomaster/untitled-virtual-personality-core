"""Английские формы команд режима управления (парсеры, без LLM и браузера).

Для каждого семейства — английская форма разбирается так же, как русская
(та же структура результата), и обычная английская речь командой НЕ
становится: «close your eyes», «type of music do you like?», «go to sleep»,
«set the alarm to 7», «what did mom say», «save my number» и т.п.
Русские формы здесь только как опора сравнения: их полное покрытие —
в test_computer_control / test_cc_parsers.

Запуск: python3 -m scripts.test_cc_english
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("OLLAMA_URL", "http://127.0.0.1:9")

import app.features.computer_control as c  # noqa: E402
from app.features.computer_control import PAGE_REF  # noqa: E402
from app.features.scenario_manager import ScenarioManager as SM  # noqa: E402
from app.features import scenario_manager as sm_mod  # noqa: E402
from app.features.task_agent import parse_task_request  # noqa: E402

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


def cases(fn, table, label):
    for text, want in table:
        got = fn(text)
        check(f"{label}: {text!r} → {want!r}" + ("" if got == want else f" (получено {got!r})"),
              got == want)


def main():
    section("подтверждение, стоп, выбор")
    cases(c.classify_confirmation, [
        ("do it", "YES"), ("go for it", "YES"), ("let's go", "YES"), ("proceed", "YES"),
        ("alright", "YES"), ("of course", "YES"), ("yes please", "YES"),
        ("don't", "NO"), ("don't do it", "NO"), ("never mind", "NO"), ("abort", "NO"),
        ("I don't want to", "NO"), ("nah", "NO"),
        ("should I do it", "UNKNOWN"), ("I do not know", "UNKNOWN"), ("do it later?", "UNKNOWN"),
        ("да", "YES"), ("не надо", "NO"),
    ], "confirm")
    for t in ("that's enough", "never mind", "stop it", "cancel the task", "don't", "enough"):
        check(f"STOP: {t!r}", bool(c.STOP_CMD_RE.match(t)))
    for t in ("stop the music festival", "enough is enough for today"):
        check(f"не STOP: {t!r}", not c.STOP_CMD_RE.match(t))
    cases(c.parse_choice, [("let's go with the second", 2), ("I'll take the third", 3),
                           ("pick 2", 2), ("1 or 2", None)], "choice")

    section("номера в выдаче и следующее видео")
    cases(c.ordinal_recipe, [
        ("third video", "search_pick:3"), ("the third video", "search_pick:3"),
        ("3rd video", "search_pick:3"), ("video 3", "search_pick:3"),
        ("11th video", "search_pick:11"), ("the second result in the playlist", "playlist_pick:2"),
        ("first video in shorts", "shorts_pick:1"), ("the video", None), ("video 99", None),
        ("третье видео", "search_pick:3"),
    ], "ordinal")
    cases(c.next_video_recipe, [("play next video", "youtube_next"), ("skip to the next video", "youtube_next"),
                                ("next song", "youtube_next"), ("next", None)], "next")

    section("закрыть окно/попап")
    cases(c.parse_close_request, [
        ("close the popup", ("close the popup", None)), ("close it", ("close it", None)),
        ("close the ad on youtube", ("close the ad", "youtube")),
        ("dismiss the notification", ("dismiss the notification", None)),
        ("minimize the player", ("minimize the player", None)),
        ("close the tab", None), ("close all tabs", None),
        ("close your eyes", None), ("close the deal", None),
    ], "close")
    check("close: generic «the popup» сводится к крестику",
          bool(c._CLOSE_GENERIC_RE.fullmatch("popup")) and bool(c._CLOSE_VERB_RE.match("close the popup")))

    section("масштаб, стирание, вкладки, досылка")
    cases(c.parse_zoom_request, [("zoom in", ("in", None)), ("zoom out on youtube", ("out", "youtube")),
                                 ("reset zoom", ("reset", None)), ("zoom 100%", ("reset", None)),
                                 ("make the page bigger", ("in", None)), ("zoom", None)], "zoom")
    cases(c.parse_erase_request, [
        ("delete 3 characters", (("Backspace", 3, "erase"), None)),
        ("erase the last two letters", (("Backspace", 2, "erase"), None)),
        ("press backspace 3 times", (("Backspace", 3, "erase"), None)),
        ("hit backspace twice", (("Backspace", 2, "erase"), None)),
        ("delete 2 characters on this page", (("Backspace", 2, "erase"), PAGE_REF)),
        ("delete the file", None),
    ], "erase")
    cases(c.parse_tab_list_query, [("what tabs are open", True), ("list tabs", True),
                                   ("show me my open tabs", True), ("I like tabs", False)], "tabs")
    for t, want in (("more", True), ("show me more", True), ("send the rest", True), ("more coffee", False)):
        check(f"more photos: {t!r} → {want}", bool(c._MORE_PHOTOS_RE.match(t)) == want)

    section("чтение и что на странице")
    cases(c.parse_read_request, [("what did the bot say", ("last", "bot")),
                                 ("what did chatgpt reply", ("last", "chatgpt")),
                                 ("what did mom say", None)], "read")
    cases(c.parse_page_view_request, [
        ("what can you see", (None, False, False)), ("show me the page", (None, False, False)),
        ("send me a screenshot", (None, True, False)), ("screenshot of youtube", ("youtube", True, False)),
        ("show me the whole page", (None, True, True)), ("take a full page screenshot", (None, True, True)),
    ], "page_view")

    section("клавиши, ползунки, ввод, отправка, скачивание")
    cases(c.parse_key_request, [("hit enter", ("Enter", None)), ("press the enter key", ("Enter", None)),
                                ("press spacebar", ("Space", None)),
                                ("press enter on this page", ("Enter", PAGE_REF)),
                                ("press conference", None)], "key")
    cases(c.parse_slider_request, [
        ("set the volume to 50%", (("volume", 50, "pct"), None)),
        ("skip to 2 minutes", (("перемотка", 2, "min"), None)),
        ("drag the working hours slider to 8", (("working hours", 8, ""), None)),
        ("set the brightness to 70%", (("brightness", 70, "pct"), None)),
        ("set the alarm to 7", None), ("move the meeting to 3", None),
    ], "slider")
    cases(c.parse_type_request, [
        ("write hello in the chat", "hello in the chat"),
        ("fill in my email into the email field", "my email into the email field"),
        ("type of music do you like?", None), ("enter the dragon is a movie", None),
        ("write me a story", None),
    ], "type")
    check("type: «and press enter» — отправка", bool(c._TYPE_SUBMIT_RE.search("hello into the search and press enter")))
    cases(c.parse_send_request, [("send the message", ("send", None)), ("submit the form", ("send", None)),
                                 ("send me a picture", None)], "send")
    cases(c.parse_download_request, [("download the pdf", ("pdf", None)), ("save the image", ("image", None)),
                                     ("save my number", None),
                                     ("download the report on this page", ("report", PAGE_REF))], "download")

    section("клик, наведение, элементы интерфейса, поиск на сайте")
    cases(c.parse_click_request, [("hit subscribe", ("subscribe", None)),
                                  ("click login on this page", ("login", PAGE_REF)),
                                  ("turn on subtitles", ("subtitles", None))], "click")
    cases(c.parse_hover_request, [("hover the mouse over settings", ("settings", None)),
                                  ("mouse over settings", ("settings", None)),
                                  ("move the mouse to settings", ("settings", None))], "hover")
    cases(c.parse_search_on_site, [
        ("search for interstellar on youtube", ("interstellar", "youtube", False)),
        ("search youtube for interstellar", ("interstellar", "youtube", False)),
        ("look up interstellar on youtube", ("interstellar", "youtube", False)),
        ("open the comments on youtube", None),
    ], "search_on_site")

    section("открыть, перейти, вкладки, режим управления")
    cases(c.parse_open_many, [("open the youtube", ["youtube"]), ("open youtube for me", ["youtube"]),
                              ("start over", None), ("start recording", None)], "open")
    cases(c.parse_tab_switch, [
        ("go to youtube", ("youtube", False)), ("switch to github", ("github", False)),
        ("visit the github website", ("github", False)),
        ("go to sleep", None), ("go to the store", None),
        ("go back to what we were talking about", None), ("go to the top of the page", None),
    ], "tab_switch")
    cases(c.parse_tab_op, [("go back on youtube", ("back", "youtube")), ("refresh", ("reload", None))], "tab_op")
    cases(c.parse_control_mode, [("turn control mode on", True), ("turn control mode off", False),
                                 ("control mode", True)], "mode")

    section("медиа и листание")
    cases(c.parse_media_request, [("make it louder", ("ArrowUp", 2, "vol_up")),
                                  ("continue the video", ("Space", 1, "toggle")),
                                  ("pause the music", ("Space", 1, "toggle")),
                                  ("play music", None)], "media")
    cases(c.parse_scroll_request, [
        ("keep scrolling", ("start", None, None, None, None)),
        ("scroll further", ("start", None, None, None, None)),
        ("scroll the left panel", ("start", None, "left", None, None)),
        ("scroll through the comments", ("start", None, None, None, "comments")),
    ], "scroll")
    cases(c.parse_scroll_to_goal, [("scroll all the way down", "bottom"), ("go to the top of the page", "top"),
                                   ("scroll until you see drinks", "drinks"),
                                   ("search the page for pepperoni", "pepperoni"),
                                   ("look for pepperoni on the page", "pepperoni")], "scroll_goal")

    section("составные команды")
    cases(c.split_compound_command, [
        ("press enter and scroll down", ["press enter", "scroll down"]),
        ("go to github and click login", ["go to github", "click login"]),
        ("click Save and close", ["click Save and close"]),
        ("нажми энтер и пролистай вниз", ["нажми энтер", "пролистай вниз"]),
    ], "split")
    cases(lambda t: c.normalize_command(t, ("Connor",)), [
        ("just open youtube", "open youtube"), ("go ahead and click login", "click login"),
        ("would you mind opening youtube", "open youtube"),
    ], "normalize")

    section("корзина, сценарии, агент, браузер")
    cases(c.parse_cart_request, [
        ("remove hawaiian from the cart", ("remove", "hawaiian")),
        ("remove one cola from my cart", ("decrease", "cola")),
        ("add one more cola", ("increase", "cola")),
        ("change pesto in the cart", ("edit", "pesto")),
        ("remove a comment", None),
    ], "cart")
    check("scenario: save as X", SM.parse_save_request("save this scenario as pizza order") == "pizza order")
    check("scenario: start recording", SM.parse_start_record("start recording a scenario") == "")
    check("scenario: stop recording", SM.parse_stop_record("cancel the recording"))
    for t, rx in (("try again", sm_mod._RETRY_RE), ("next", sm_mod._SKIP_RE),
                  ("that's all", sm_mod._NO_RE), ("thanks", sm_mod._CLOSE_RE),
                  ("never mind", sm_mod._CANCEL_RE)):
        check(f"scenario answer: {t!r}", bool(rx.match(t)))
    check("task: «do it yourself: …»",
          parse_task_request("do it yourself: order a pizza") == "order a pizza")
    from app.bot_instance import _RESCUE_BROWSER_RE
    for t in ("fix the browser", "solve the captcha"):
        check(f"rescue: {t!r}", bool(_RESCUE_BROWSER_RE.match(t)))
    check("rescue: «fix the bug» — нет", not _RESCUE_BROWSER_RE.match("fix the bug"))

    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
