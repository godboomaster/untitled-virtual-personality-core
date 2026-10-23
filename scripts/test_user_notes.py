"""Тест: заметки пользователя («запиши в заметки …», «что у меня в
заметках?», «удали заметку 2») и блок «Заметки» на главной.

  - разбор реплики (app/features/user_notes.parse_request): запись — текст
    после «в заметки», до него, до глагола («мука, яйца — запиши в
    заметки»), многострочный, «что …» и вежливые вставки срезаются;
    «запиши это» — ссылка на прошлое сообщение; удаление по номеру, тексту,
    «последнюю», «все (про X)», «очисти заметки»; показ списка; вопрос по
    заметкам; ru и en; «запиши купить хлеб», «открой заметки», «note that …»,
    «не записывай …» — не запись; длинный ввод разбирается за миллисекунды;
  - книжка: запись/правка/удаление, пустой текст, предел числа заметок,
    обрезка длинного текста, два экземпляра на один файл (веб и Telegram —
    разные процессы) видят записи друг друга, битый id персоны;
  - кто что видит: владелец — свои и с главной, другой собеседник — только
    свои (и удалить чужую не может);
  - выбор заметок для удаления (номера, диапазон, текст по основам слов,
    неоднозначность — ничего);
  - ход бота (_notes_turn): запись, «запиши это» из цитаты/последнего
    ответа, «что записать?», список досылкой, удаление, переспрос;
    проводка в конвейер (дела, напоминания, правило, веб-поиск, режим
    управления) и блок в системном промпте;
  - API главной: список свежими сверху, запись, правка, удаление, 404/422,
    notes: false, число заметок в сводке /api/home; /notes в веб-чате.

LLM, Ollama и сеть не вызываются; data/ — временный каталог.
Запуск: PYTHONPATH=. python3 -m scripts.test_user_notes
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_TMP = tempfile.mkdtemp(prefix="user_notes_")
os.environ["VPC_DATA_DIR"] = _TMP
ROOT = Path(__file__).parent.parent

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


NAMES = ["коннор", "Коннор", "connor"]


def test_parse():
    section("1. Разбор реплики")
    from app.features.user_notes import parse_request as pr

    def add(text, want, ref=False):
        r = pr(text, NAMES)
        check(f"запись: {text!r} → {want!r}",
              r is not None and r.kind == "add" and r.text == want and r.ref == ref)

    add("запиши в заметки купить молоко", "Купить молоко")
    add("Коннор, запиши в заметки: код домофона 1234", "Код домофона 1234")
    add("запиши в заметки, что завтра приедет мама", "Завтра приедет мама")
    add("запиши купить молоко в заметки", "Купить молоко")
    add("запиши мне в заметки пожалуйста позвонить Васе", "Позвонить Васе")
    add("запиши номер Васи в заметки: 89001234567", "Номер Васи: 89001234567")
    add("в заметки запиши: сдать отчёт до пятницы", "Сдать отчёт до пятницы")
    add("сделай заметку: проверить масло", "Проверить масло")
    add("новая заметка — купить подарок", "Купить подарок")
    add("заметка: пароль на роутере", "Пароль на роутере")
    add("можешь записать в заметки, что встреча в 15:00?", "Встреча в 15:00")
    add("Мука, яйца, молоко — запиши в заметки", "Мука, яйца, молоко")
    add("запомни в заметках: у Пети день рождения 5 мая", "У Пети день рождения 5 мая")
    add("сохрани в блокнот идею для стартапа", "Идею для стартапа")
    add("запиши в заметки рецепт:\nмука 200 г\nяйца 2 шт", "Рецепт:\nмука 200 г\nяйца 2 шт")
    add("запиши в заметки напомнить маме про лекарства", "Напомнить маме про лекарства")
    add("запиши в заметки", "")
    add("Ты можешь записать в заметки?", "")
    add("запиши это в заметки", "", ref=True)
    add("запиши этот рецепт в заметки", "", ref=True)
    add("запиши в заметки то, что ты сейчас сказал", "", ref=True)
    add("add buy milk to my notes", "Buy milk")
    add("Connor, add to my notes: call mom tomorrow", "Call mom tomorrow")
    add("please write down in my notes that the meeting is at 3", "The meeting is at 3")
    add("take a note: buy a gift", "Buy a gift")
    add("make a note of this", "", ref=True)
    add("запиши в заметки всё, что ты перечислил выше", "", ref=True)
    add("add your last answer to my notes", "", ref=True)
    add("запиши в заметки: Петя сказал, что ты сказал ему правду",
        "Петя сказал, что ты сказал ему правду")
    add("запиши в заметки купить хлеб, пожалуйста, спасибо", "Купить хлеб")
    add("can you add this to my notes?", "", ref=True)

    def kind(text, want, target=None, all_=None):
        r = pr(text, NAMES)
        got = r.kind if r else None
        good = got == want
        if good and target is not None:
            good = r.text == target
        if good and all_ is not None:
            good = r.all == all_
        check(f"{want}: {text!r}" + (f" → {target!r}" if target is not None else ""), good)

    kind("удали заметку 2", "delete", "2", False)
    kind("удали из заметок код домофона", "delete", "код домофона")
    kind("удали заметку про домофон", "delete", "домофон")
    kind("удали последнюю заметку", "delete", "последнюю")
    kind("убери заметки 1 и 3", "delete", "1 и 3")
    kind("удали все заметки", "delete", "", True)
    kind("очисти заметки", "delete", "", True)
    kind("удали все заметки про работу", "delete", "работу", True)
    kind("delete note 2", "delete", "2")
    kind("remove the last note", "delete", "last")
    kind("clear my notes", "delete", "", True)
    kind("что у меня в заметках?", "show")
    kind("покажи заметки", "show")
    kind("мои заметки", "show")
    kind("Коннор, что в заметках", "show")
    kind("what's in my notes?", "show")
    kind("show my notes", "show")
    kind("посмотри в заметках, какой код от домофона", "ask")
    kind("что я записывал в заметки про машину?", "ask")
    kind("what did I write in my notes about the car?", "ask")
    kind("не записывай это в заметки", "ask")
    kind("don't add this to my notes", "ask")
    for text in ("запиши купить хлеб", "добавь в список дел купить хлеб",
                 "напомни через час позвонить", "открой заметки", "note that I'm late",
                 "привет", ""):
        check(f"не про заметки: {text!r}", pr(text, NAMES) is None)


def test_parse_speed():
    section("1б. Длинный ввод — быстро (без квадратичного и экспоненциального перебора)")
    import time
    from app.features.user_notes import parse_request as pr
    cases = {
        "длинная заметка": ("запиши в заметки: " + "слово " * 3000, "add"),
        "текст до глагола": ("мука яйца " * 2000 + "\n— запиши в заметки", "add"),
        "много глаголов без «в заметки»": (("запиши добавь сохрани " * 1500) + " заметки", "ask"),
        "en: много глаголов": (("add write save " * 2000) + " my notes", "ask"),
        "серия пробелов перед буквой": ("запиши в заметки купить" + " " * 5000 + "x", "add"),
        "50 тыс. символов с упоминанием": ("а " * 25000 + " заметки", "ask"),
    }
    for name, (text, want) in cases.items():
        t0 = time.perf_counter()
        r = pr(text, NAMES)
        dt = time.perf_counter() - t0
        check(f"{name}: {dt * 1000:.1f} мс, {want}", r is not None and r.kind == want and dt < 0.1)
    r = pr("запиши в заметки: " + "слово " * 3000, NAMES)
    check("длинная заметка — целиком", r.text.startswith("Слово слово") and len(r.text) > 17000)


def test_store():
    section("2. Книжка: запись, правка, удаление, общий файл")
    from app.features import user_notes as un

    a = un.UserNotes("notes_store")
    b = un.UserNotes("notes_store")  # «другой процесс» на тот же файл
    check("путь — data/api_<персона>/notes/notes.json",
          a.path == Path(_TMP) / "api_notes_store" / "notes" / "notes.json")
    n1 = a.add("  Купить молоко  ", author_id="1", author_name="Макс", owner=True)
    check("запись: текст без краевых пробелов, автор, источник",
          n1 and n1["text"] == "Купить молоко" and n1["author_id"] == "1"
          and n1["owner"] is True and n1["source"] == "chat" and un.valid_note_id(n1["id"]))
    check("второй экземпляр видит запись первого", [n["id"] for n in b.all()] == [n1["id"]])
    n2 = b.add("Код домофона 2580", author_id="web_user", owner=True, source="home")
    check("и наоборот", [n["text"] for n in a.all()] == ["Купить молоко", "Код домофона 2580"])
    check("пустой текст — не записывается", a.add("   ") is None and len(a.all()) == 2)
    long = a.add("x" * (un.MAX_TEXT + 500))
    check("длинный текст обрезан до MAX_TEXT", long and len(long["text"]) == un.MAX_TEXT)
    a.remove([long["id"]])
    upd = a.update(n1["id"], "Купить молоко и хлеб")
    check("правка: текст и updated_at", upd and upd["text"] == "Купить молоко и хлеб" and upd["updated_at"])
    check("правка несуществующей / пустым — None",
          a.update("ffffffffffff", "x") is None and a.update(n1["id"], " ") is None)
    removed = b.remove([n2["id"], "ffffffffffff"])
    check("удаление: только существующие", [n["id"] for n in removed] == [n2["id"]]
          and [n["id"] for n in a.all()] == [n1["id"]])
    check("недопустимый id персоны — ValueError", _raises(lambda: un.UserNotes("../x")))
    check("id заметки: только hex", not un.valid_note_id("../../x") and not un.valid_note_id(""))

    c = un.UserNotes("notes_full")
    old = un.MAX_NOTES
    un.MAX_NOTES = 3
    try:
        for i in range(3):
            c.add(f"n{i}")
        check("книжка заполнена — новая не пишется", c.add("лишняя") is None and len(c.all()) == 3
              and c.is_full())
    finally:
        un.MAX_NOTES = old


def _raises(fn):
    try:
        fn()
    except ValueError:
        return True
    return False


def test_visibility_and_resolve():
    section("3. Кто что видит; что удалить")
    from app.features import user_notes as un

    nb = un.UserNotes("notes_vis")
    nb.add("Код домофона 2580", author_id="100", author_name="Владелец", owner=True)
    nb.add("С главной", author_id="web_user", owner=True, source="home")
    nb.add("Чужая заметка", author_id="200", author_name="Гость", owner=False)
    check("владелец: свои и с главной, без чужой",
          [n["text"] for n in nb.visible("100", True)] == ["Код домофона 2580", "С главной"])
    check("гость: только свои", [n["text"] for n in nb.visible("200", False)] == ["Чужая заметка"])
    check("незнакомец: ничего", nb.visible("300", False) == [])

    notes = [{"id": str(i), "text": t} for i, t in enumerate(
        ["Купить молоко", "Код от домофона 2580", "Масло для машины", "Позвонить маме", "Купить хлеб"], 1)]
    R = lambda text, all_=False: un.resolve_delete(un.NotesRequest("delete", text=text, all=all_), notes)  # noqa: E731
    ids = lambda r: [n["id"] for n in r] if r else r  # noqa: E731
    check("номер", ids(R("2")) == ["2"])
    check("номера списком и диапазоном", ids(R("1, 3")) == ["1", "3"] and ids(R("2-4")) == ["2", "3", "4"])
    check("«1 и 3»", ids(R("1 и 3")) == ["1", "3"])
    check("номер вне списка — None", R("9") is None)
    check("последняя / первая", ids(R("последнюю")) == ["5"] and ids(R("first")) == ["1"])
    check("текст по основам слов: «домофон» → «домофона»", ids(R("домофон")) == ["2"])
    check("«машину» → «машины»", ids(R("машину")) == ["3"])
    check("неоднозначно («купить») — None", R("купить") is None)
    check("«все про купить» — обе", ids(R("купить", True)) == ["1", "5"])
    check("«все» без уточнения — все", len(R("", True)) == 5)
    check("ни номера, ни текста — None", R("") is None)
    check("пустая книжка — пусто", un.resolve_delete(un.NotesRequest("delete", text="2"), []) == [])

    lst = un.render_list([{"text": "Рецепт:\nмука"}, {"text": "y" * 600}], "ru")
    check("список: заголовок, номера, отступ строк, обрезка",
          lst.startswith("📝 Заметки:\n1. Рецепт:\n   мука") and "2. " + "y" * 399 + "…" in lst)
    many = [{"text": "z" * 390} for _ in range(30)]
    out = un.render_list(many, "en")
    check("длинный список — «и ещё N» вместо всего", "more — all notes are on the home page" in out
          and len(out) < 4096)
    check("пустой список", un.render_list([], "ru") == "Заметок пока нет.")


def _bot(owner_id="100", web=False):
    from app.bot_instance import BotInstance
    from app.features.user_notes import UserNotes
    b = BotInstance.__new__(BotInstance)
    b.notes = UserNotes("notes_bot" + ("_web" if web else ""))
    b.web_single_user = web
    b.owner = owner_id
    b._pending_list_messages = {}
    return b


def test_turn():
    section("4. Ход бота: _notes_turn")
    from app.features.user_notes import parse_request as pr

    b = _bot()
    names = NAMES
    turn = lambda text, uid="100", reply=None, hist=None: b._notes_turn(  # noqa: E731
        pr(text, names), "chat1", uid, "Макс", reply, hist or [], "ru")

    ctx = turn("запиши в заметки код домофона 2580")
    check("запись: в книжке, модели — «уже сохранено»",
          [n["text"] for n in b.notes.all()] == ["Код домофона 2580"]
          and "already saved" in ctx and "Код домофона 2580" in ctx)
    check("запись не шлёт список", b._pending_list_messages.get("chat1") in (None, []))

    hist = [{"role": "user", "content": "дай рецепт"},
            {"role": "assistant", "content": "Блины: 2 яйца, 500 мл молока, 250 г муки"}]
    turn("запиши это в заметки", hist=hist)
    check("«запиши это» — последний ответ персоны",
          b.notes.all()[-1]["text"] == "Блины: 2 яйца, 500 мл молока, 250 г муки")
    full = "Длинный ответ " + "слово " * 120
    hist2 = [{"role": "assistant", "content": full}, {"role": "assistant", "content": "другое"}]
    turn("запиши в заметки", reply="[Коннор]: " + full[:500], hist=hist2)
    check("ответ на сообщение — полный текст цитаты из истории", b.notes.all()[-1]["text"] == full.strip())
    turn("запиши в заметки", reply="Своя цитата из веба")
    check("цитата без префикса (веб) — как есть", b.notes.all()[-1]["text"] == "Своя цитата из веба")
    n_before = len(b.notes.all())
    ctx = turn("запиши в заметки")
    check("«запиши в заметки» без текста и цитаты — «что записать?», ничего не записано",
          "did not say what" in ctx and len(b.notes.all()) == n_before)
    ctx = turn("запиши это в заметки")
    check("«запиши это» без прошлого ответа — тоже вопрос", "did not say what" in ctx)

    b._pending_list_messages = {}
    ctx = turn("что у меня в заметках?")
    lst = b._pending_list_messages.get("chat1") or []
    check("показ: список досылкой, модели — не печатать",
          len(lst) == 1 and lst[0].startswith("📝 Заметки:") and "DO NOT print" in ctx)

    ctx = turn("какой код домофона я записывал в заметки?")
    check("вопрос по заметкам: заметки в промпте", "written down by them" in ctx and "2580" in ctx)

    ctx = turn("удали заметку про домофон")
    check("удаление по тексту", "just deleted" in ctx
          and all("домофон" not in n["text"] for n in b.notes.all()))
    n_before = len(b.notes.all())
    ctx = turn("удали заметку")
    check("непонятно какую — ничего не удалено, переспрос",
          "unclear which one" in ctx and len(b.notes.all()) == n_before)

    # Гость: свою пишет, чужих не видит и не удаляет
    turn("запиши в заметки гостевая", uid="200")
    ctx = turn("что у меня в заметках?", uid="200")
    check("гость видит только свою", "Гостевая" in ctx and "Блины" not in ctx)
    owner_count = len(b.notes.visible("100", True))
    ctx = turn("удали заметку 1", uid="200")
    check("гость удаляет свою, заметки владельца целы",
          "Гостевая" in ctx and len(b.notes.visible("100", True)) == owner_count)
    check("владелец не видит гостевых",
          all("Гостевая" not in n["text"] for n in b.notes.visible("100", True)))

    w = _bot(owner_id="", web=True)
    w.notes.add("С главной", author_id="web_user", owner=True, source="home")
    w._notes_turn(pr("покажи заметки", names), "web_user", "web_user", "", None, [], "ru")
    check("веб-чат (один пользователь): заметка с главной в списке",
          "С главной" in (w._pending_list_messages.get("web_user") or [""])[0])
    check("/notes — тот же список", "С главной" in _notes_cmd(w))
    w.notes = None
    check("notes: false — /notes честно говорит", "выключены" in _notes_cmd(w))


def _notes_cmd(bot):
    bot.chat_user_language = lambda chat_id: "ru"
    return bot.notes_list_text("web_user", "web_user")


def test_wiring():
    section("5. Проводка в конвейер и промпт")
    from app.core.persona import PersonaLayer
    from app.bot_instance import BotInstance

    src = (ROOT / "app" / "bot_instance.py").read_text(encoding="utf-8")
    check("разбор — по написанному (raw_user_text), с именами персоны",
          "user_notes.parse_request(raw_user_text, self._address_names())" in src)
    check("в режиме управления заметки молчат",
          "if getattr(self, \"notes\", None) is not None \\\n"
          "                and not (chat_id and self.control_mode_on(chat_id)):" in src)
    # claimed — ход забран явной просьбой: заметки или сервисы (app/integrations)
    check("заметки забирают ход (claimed)",
          "claimed = notes_explicit or services_chat.claims(services_req)" in src)
    for frag, name in [
        ("and not self.control_mode_on(chat_id) and not claimed:\n                reminder_context, is_reminder_request",
         "напоминания не перехватывают"),
        ("and not self.control_mode_on(chat_id) and not claimed:\n                if is_todo_done_request",
         "дела не перехватывают"),
        ("and not self.control_mode_on(chat_id) and not claimed):\n                if is_inventory_add_request",
         "инвентарь не перехватывает"),
        ("                    or claimed\n", "обучение уступает"),
        ("and not claimed):\n            # Собираем контекст персоны для QueryEnhancer", "без веб-поиска"),
        ("and not scenario_outside_mode \\\n                    and not claimed:", "«запомни в заметках» — не правило"),
        ("notes_context=notes_context,", "блок уходит в prepare_messages"),
    ]:
        check(f"заметки: {name}", frag in src)

    note = BotInstance._cc_mode_feature_note("запиши в заметки купить молоко")
    check("режим управления: честная пометка и про заметки", note and "notes" in note)
    check("…а на постороннее — нет", BotInstance._cc_mode_feature_note("как дела") is None)

    p = PersonaLayer("__no_such_persona__")
    p.system_prompt = "PERSONA PROMPT"
    msgs = p.prepare_messages("что у меня в заметках?", notes_context="NOTES BLOCK")
    check("prepare_messages: блок в системном сообщении, не в реплике",
          "NOTES BLOCK" in msgs[0]["content"] and "NOTES BLOCK" not in msgs[-1]["content"])
    check("без заметок — блока нет", "NOTES BLOCK" not in p.prepare_messages("привет")[0]["content"])


def test_api():
    section("6. API главной")
    from fastapi.testclient import TestClient
    import app.api.server as server_mod

    orig = server_mod._api_token
    server_mod._api_token = ""
    try:
        c = TestClient(server_mod.app, base_url="http://127.0.0.1")
        base = "/api/personas/connor/notes"
        check("пусто сначала", c.get(base).json() == {"items": []})
        r1 = c.post(base, json={"text": "Первая"}).json()["note"]
        r2 = c.post(base, json={"text": "Вторая"}).json()["note"]
        check("запись с главной: владелец, источник home",
              r1["owner"] is True and r1["source"] == "home" and "author_id" not in r1)
        items = c.get(base).json()["items"]
        check("список — свежие сверху", [n["text"] for n in items] == ["Вторая", "Первая"])
        up = c.patch(f"{base}/{r1['id']}", json={"text": "Первая (правка)"})
        check("правка", up.status_code == 200 and up.json()["note"]["text"] == "Первая (правка)")
        check("пустой текст — 422", c.post(base, json={"text": ""}).status_code == 422)
        check("слишком длинный — 422", c.post(base, json={"text": "x" * 4001}).status_code == 422)
        check("удаление", c.delete(f"{base}/{r2['id']}").json() == {"ok": True})
        check("повторное удаление — 404", c.delete(f"{base}/{r2['id']}").status_code == 404)
        check("кривой id — 404", c.delete(f"{base}/..%2F..%2Fx").status_code == 404
              and c.patch(f"{base}/zz", json={"text": "x"}).status_code == 404)
        check("нет персоны — 404", c.get("/api/personas/no_such_persona_x/notes").status_code == 404)
        home = c.get("/api/home").json()["personas"]["connor"]
        check("сводка главной: число заметок", home["notes_enabled"] is True and home["notes_count"] == 1)

        # notes: false — у своей копии персоны
        pdir = Path(_TMP) / "personas"
        pdir.mkdir(parents=True, exist_ok=True)
        src = (ROOT / "app" / "personas" / "connor.yaml").read_text(encoding="utf-8")
        (pdir / "nonotes.yaml").write_text(
            src.replace("features:\n", "features:\n  notes: false\n", 1), encoding="utf-8")
        check("notes: false — 400", c.get("/api/personas/nonotes/notes").status_code == 400)
        home = c.get("/api/home").json()["personas"].get("nonotes") or {}
        check("…и в сводке выключены", home.get("notes_enabled") is False)
    finally:
        server_mod._api_token = orig

    ssrc = (ROOT / "app" / "api" / "server.py").read_text(encoding="utf-8")
    check("/notes в веб-чате", 'if cmd == "notes":\n        return bot.notes_list_text(req.user_id, chat_id), False' in ssrc)
    tsrc = (ROOT / "app" / "telegram_bot.py").read_text(encoding="utf-8")
    check("/notes в Telegram", '"notes": notes_cmd if bot.notes is not None else None' in tsrc
          and '"todo", "notes", "reminders"' in tsrc)


def main():
    try:
        test_parse()
        test_parse_speed()
        test_store()
        test_visibility_and_resolve()
        test_turn()
        test_wiring()
        test_api()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"\nИтого: {ok - failures}/{ok} OK")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
