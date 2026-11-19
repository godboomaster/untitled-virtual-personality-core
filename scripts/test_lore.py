"""Лор персоны: нарезка, индекс на диске, поиск без LLM, блок промпта и API.

  A. нарезка: заголовки всех видов, куски не длиннее предела, текст не теряется;
  B. индекс: замена и удаление файла, чтение другим объектом, подхват правки со
     стороны, файлы одного поколения, лимит, имена файлов;
  C. поиск на модели ядра: вопросы к персонажу находят свой раздел, болтовня и
     посторонние реплики не тянут лор, продолжение понимается по прошлой реплике;
  D. блок промпта: правило о пробелах есть и без находок;
  E. ход персоны: блок попадает в промпт, в лёгком режиме кусков меньше, поломка
     поиска не мешает ответу, лор не включён — ноль лишнего;
  F. API: загрузка (кодировки, отказы), разделы, проверка запроса, удаление;
     просмотр файла (текст с заголовками, куски, исходник: PDF — встроенно,
     остальное — скачать, html не исполняется; замена и удаление убирают
     прежний исходник, файл без исходника — только текст);
     копия персоны — с лором, смена id переносит его с папкой;
  G. обычные файлы: нарезка видна модели целиком;
  H. телефон без ядра: тот же поиск под Node, те же находки, блок и бюджет Gemma;
  I. лор, загруженный на телефоне: нарезка, оглавление, имена файлов и сжатие
     векторов — как у ядра; docx (стили заголовков в любом языке Word,
     таблицы, списки, сущности) режется в те же разделы, что разбор ядра.

Всё на временной папке (VPC_DATA_DIR): настоящий data/ не трогается. LLM не
зовётся, нужна только модель векторов ядра (кэш HF).

Запуск: python -m scripts.test_lore
"""

import json
import os
import shutil
import sys
import tempfile
import unicodedata
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

TMP = Path(tempfile.mkdtemp(prefix="vpc_lore_"))
os.environ["VPC_DATA_DIR"] = str(TMP / "data")
(TMP / "data").mkdir()

import numpy as np  # noqa: E402

from app.core import lore  # noqa: E402

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


def fake_embed(texts):
    # Векторы без модели: для проверок индекса смысл не важен, важна форма
    out = np.zeros((len(texts), 16), dtype=np.float32)
    for i, t in enumerate(texts):
        for w in t.lower().split():
            out[i, hash(w) % 16] += 1
        out[i, 0] += 1
    return out


KAYA = """# Детство
Кая Ветрова родилась в портовом городе Сальмаре, в семье смотрителя маяка. Мать умерла от чумной лихорадки, когда Кае было семь, и воспитывал девочку отец вместе со старшим братом Тимуром.

# Брат
Старший брат Тимур был для Каи главным человеком в жизни. Он ушёл служить на флот Республики. В битве у Чёрных Шхер его корабль «Стриж» затонул. Тела не нашли, и Кая хранит его последнее письмо, зашитое в подкладку куртки.

# Шрам
Кая потеряла левый глаз в девятнадцать лет на Ржавых верфях: бригадир контрабандистов Осип Рваный полоснул её абордажным крюком, когда она отказалась везти партию рабов. С тех пор она носит латунную повязку, а через щёку идёт длинный шрам.

# Учитель
Стрелять и вести судно её научила старая капитанша Марта Дрейк по прозвищу Бабка. Марта подобрала полуслепую девушку в госпитале и взяла на шхуну юнгой. Марта умерла от старости на борту, а корабль завещала ученице.

# Корабль
Шхуна «Вторая Попытка» — двухмачтовая, быстрая, со скрытыми трюмами под балластом. Кая получила её в двадцать шесть лет по завещанию Марты.

# Враг
Главный враг Каи — адмирал Рейнхард Кросс, глава таможенной эскадры. За голову Каи он назначил награду в десять тысяч крон.

# Еда
Кая терпеть не может рыбу. Любимое блюдо — лимонный пирог из пекарни на Соляной улице. На борту пьёт только крепкий чёрный чай с ромом, а кофе считает напитком для бухгалтеров.

# Страх
Больше всего Кая боится штиля в открытом море. Однажды шхуна провела двадцать три дня в мёртвом штиле. С тех пор Кая спит при свете лампы и не выносит полной тишины.

# Любовь
У Каи был роман с Лукасом Хейдом, сыном губернатора. Когда их разоблачили, Лукас выбрал отца и выдал маршрут её шхуны. Кая не простила его, хотя носит подаренный им компас.
"""


# ── A ──

def test_chunking():
    section("A. Нарезка")
    chunks = lore.chunk_text(KAYA)
    sections = {c["s"] for c in chunks}
    check("заголовки markdown стали разделами", {"Детство", "Шрам", "Любовь"} <= sections)
    check("текст не теряется: каждое предложение есть в кусках",
          all(any(s in c["t"] for c in chunks) for s in ("Кая Ветрова родилась", "компас")))

    long_para = " ".join(f"Предложение номер {i} про долгую историю шхуны и её команды." for i in range(40))
    chunks = lore.chunk_text(f"# Хроника\n\n{long_para}")
    check("длинный абзац разбит на куски", len(chunks) > 3)
    check("кусок вместе с заголовком не длиннее предела",
          all(len(lore.embed_text(c)) <= lore.CHUNK_CHARS + 2 for c in chunks))
    check("режется по предложениям", all(c["t"].endswith(".") for c in chunks))
    joined = " ".join(c["t"] for c in chunks)
    check("слова длинного абзаца все на месте", joined == long_para)

    one = "а" * 1000
    check("слово длиннее куска не теряется", "".join(c["t"] for c in lore.chunk_text(one)) == one)
    no_marks = ("слова без знаков препинания " * 60).strip()
    check("текст без знаков режется по словам",
          all(len(c["t"]) <= lore.CHUNK_CHARS for c in lore.chunk_text(no_marks)))

    txt = ("Детство\n\nРодилась у моря и там же выросла, среди сетей и штормов, и очень любила этот город.\n\n"
           "Юность\n\nУшла из дома в шестнадцать лет.")
    check("заголовок в txt без разметки", [c["s"] for c in lore.chunk_text(txt)] == ["Детство", "Юность"])
    sheet = "Имя: Кая\nВозраст: 31\nРост: 170\n\nПрофессия: капитан."
    check("анкета «Ключ: значение» — не заголовки", all(not c["s"] for c in lore.chunk_text(sheet)))
    dialogue = "— Ты куда?\n— Домой.\nОна кивнула и ушла, не оборачиваясь."
    check("реплики диалога — не заголовки", all(not c["s"] for c in lore.chunk_text(dialogue)))
    novel = ("Почти через минуту Лиаваль спросил:\n\n— Ты уверен?\n\n"
             "Оранжевый Свет Илларион тут же улыбнулся и сказал\n\nЭто было давно, и никто уже не помнил почему.")
    check("слова автора в книге из docx — не заголовки", all(not c["s"] for c in lore.chunk_text(novel)))
    check("короткий заголовок с двоеточием — заголовок",
          lore.chunk_text("Семья:\n\nОтец был смотрителем маяка.")[0]["s"] == "Семья")
    bold = "**Семья**\n\nОтец был смотрителем маяка."
    check("жирная строка (docx) — заголовок", lore.chunk_text(bold)[0]["s"] == "Семья")
    nested = "# Биография\n## Детство\nТекст раз.\n## Юность\nТекст два.\n# Мир\nТекст три."
    check("вложенные заголовки: два нижних уровня",
          [c["s"] for c in lore.chunk_text(nested)] == ["Биография / Детство", "Биография / Юность", "Мир"])
    check("картинки data: не попадают в текст",
          "base64" not in lore.chunk_text("Текст ![](data:image/png;base64,AAAA) дальше")[0]["t"])
    check("пустой текст — ни одного куска", lore.chunk_text("  \n\n ") == [])


# ── B ──

def test_store():
    section("B. Индекс на диске")
    root = TMP / "store"
    st = lore.LoreStore(root, embed=fake_embed)
    check("без лора: пусто и искать нечего",
          not st.has_lore() and st.search("кто ты?").skipped == "no_lore")
    entry = st.add_file("kaya.md", KAYA)
    check("файл добавлен", entry["chunks"] > 5 and st.info()["chunks"] == entry["chunks"])
    check("разделы видны в списке", any(s["title"] == "Шрам" for s in st.info()["files"][0]["sections"]))
    check("источник сохранён", len(list((root / "sources").glob("*.txt"))) == 1)

    st.add_file("world.txt", "Сальмара — портовый город на юге. Там всегда пахнет солью.")
    total = st.info()["chunks"]
    other = lore.LoreStore(root, embed=fake_embed)
    check("второй объект читает то же с диска", other.info()["chunks"] == total)
    st.add_file("kaya.md", "# Новое\nКороткая новая версия.")
    check("тот же файл заменяется, а не копится",
          [f["name"] for f in st.info()["files"]] == ["world.txt", "kaya.md"]
          and st.info()["files"][1]["chunks"] == 1)
    check("другой объект подхватывает правку без связи с первым", other.info() == st.info())
    check("на диске одно поколение векторов", len(list(root.glob("vectors.*.npy"))) == 1)

    check("удаление чужого имени — False", not st.remove_file("нет такого.txt"))
    check("удаление файла", st.remove_file("kaya.md") and st.info()["chunks"] == 1)
    check("источник удалён вместе с файлом", len(list((root / "sources").glob("*.txt"))) == 1)
    check("последний файл", st.remove_file("world.txt") and not st.has_lore())
    check("без индекса поиск снова пуст", st.search("кто ты?").skipped == "no_lore")
    check("векторов не осталось", not list(root.glob("vectors.*.npy")))

    try:
        st.add_file("empty.txt", "   \n ")
        empty = False
    except lore.LoreError:
        empty = True
    check("пустой файл — понятный отказ", empty and not st.has_lore())
    with mock.patch.object(lore, "MAX_TOTAL_CHARS", 100):
        try:
            st.add_file("big.txt", "слово " * 100)
            big = False
        except lore.LoreError:
            big = True
    check("лимит на весь лор", big and not st.has_lore())

    check("имя файла — без пути", lore.clean_filename("../../etc/passwd") == "passwd"
          and lore.clean_filename("C:\\fakepath\\lore.md") == "lore.md")
    nfd = unicodedata.normalize("NFD", "Йод.txt")
    st.add_file(nfd, "Текст про йод и море.")
    st.add_file("Йод.txt", "Другой текст про йод и море.")
    check("имя из macOS (NFD) — тот же файл", len(st.info()["files"]) == 1)
    index = json.loads((root / "index.json").read_text(encoding="utf-8"))
    (root / index["vectors"]).write_bytes(b"not numpy")
    fresh = lore.LoreStore(root, embed=fake_embed)
    check("битые векторы: лора просто нет, не падение", not fresh.has_lore())


# ── C ──

def test_search():
    section("C. Поиск (модель ядра)")
    st = lore.LoreStore(TMP / "kaya", embed=None)
    st.add_file("kaya.md", KAYA)
    name = "Кая"

    def finds(query, wanted, previous=""):
        result = st.search(query, previous=previous, name=name)
        return any(h.section == wanted for h in result.hits)

    for query, wanted in (("откуда у тебя этот шрам?", "Шрам"),
                          ("кто тебя научил стрелять?", "Учитель"),
                          ("ты скучаешь по брату?", "Брат"),
                          ("почему не пьёшь кофе?", "Еда"),
                          ("кто такой Осип Рваный?", "Шрам"),
                          ("что за компас у тебя?", "Любовь"),
                          ("расскажи про адмирала Кросса", "Враг"),
                          ("что случилось с Тимуром?", "Брат")):
        check(f"«{query}» → {wanted}", finds(query, wanted))

    for query in ("привет", "спасибо", "ок", "как дела?", "доброе утро", "лол"):
        result = st.search(query, name=name)
        check(f"болтовня «{query}» — лор не нужен", not result.hits)
    for query in ("как приготовить борщ?", "сколько будет 15 на 7?", "напиши код на питоне",
                  "какая завтра погода?", "посоветуй музыку"):
        result = st.search(query, name=name)
        check(f"посторонняя реплика «{query}» — лор не нужен", not result.hits)

    result = st.search("ты любишь пиццу?", name=name)
    check("к персонажу, но не о лоре — мало и с пометкой",
          result.weak and 0 < len(result.hits) <= lore.WEAK_LIMIT)
    check("обращение по имени снимается: «Кая, привет»", not st.search("Кая, привет", name=name).hits)
    check("«кто ты?» — вопрос к персонажу, хотя слов темы нет", bool(st.search("кто ты?", name=name).hits))
    check("короткое продолжение понимается с прошлой репликой",
          finds("а тело нашли?", "Брат", previous="ты скучаешь по брату? Каждый день."))
    check("продолжение про местоимение",
          finds("она ещё жива?", "Учитель", previous="кто тебя научил стрелять? Старуха Марта."))
    limited = st.search("откуда у тебя этот шрам?", name=name, limit=3)
    check("предел кусков соблюдается", len(limited.hits) <= 3)
    hits = st.search("откуда у тебя этот шрам?", name=name).hits
    check("лучшая находка — первой", hits[0].section == "Шрам")
    check("на маленьком лоре персона не получает его целиком", len(hits) < len(st.snapshot().chunks))

    check("имя подставляется вместо «ты»", lore.vector_query("кто тебя учил?", "Кая") == "кто Кая учил?")
    check("«у тебя» — целиком", lore.vector_query("что у тебя с глазом?", "Кая") == "что Кая с глазом?")
    check("без обращения — приставкой", lore.vector_query("почему не пьёшь?", "Кая") == "Кая: почему не пьёшь?")
    check("слово с «ты» внутри не трогается", "простыня" in lore.vector_query("где простыня?", "Кая"))
    check("английский", lore.vector_query("what is your name", "Kaya") == "what is Kaya name")


# ── D ──

def test_block():
    section("D. Блок промпта")
    empty = lore.build_block(lore.LoreResult())
    check("без находок — правило против выдумки", empty == lore.GAP_RULE)
    hit = lambda i, s, t: lore.LoreHit(i, "f", s, t, 0.5, False)  # noqa: E731
    block = lore.build_block(lore.LoreResult(hits=[hit(9, "", "Три."), hit(2, "Брат", "Два."), hit(1, "Брат", "Раз.")]))
    check("соседние куски склеены в порядке текста", "[Брат] Раз. Два." in block)
    check("кусок без раздела — без скобок", "\nТри.\n" in block)
    check("есть оба правила", lore.RULE in block and lore.GAP_RULE in block)
    weak = lore.build_block(lore.LoreResult(hits=[hit(1, "Брат", "Раз.")], weak=True))
    check("слабые находки помечены", weak.startswith(lore.WEAK_HEAD))
    rows = [{"role": "user", "content": "кто ты?"}, {"role": "assistant", "content": "Капитан."},
            {"role": "user", "content": "а что дальше?"}]
    check("прошлая реплика без текущей", lore.previous_turn(rows) == "кто ты? Капитан.")
    check("длинный ответ — только конец",
          len(lore.previous_turn([{"role": "assistant", "content": "х" * 5000}])) == lore.TAIL_CHARS)
    check("пустая история", lore.previous_turn(None) == "" and lore.previous_turn([]) == "")


# ── E ──

def test_turn():
    section("E. Ход персоны")
    from app.bot_instance import BotInstance
    from app.core.persona import PersonaLayer
    bot = SimpleNamespace(persona=SimpleNamespace(persona_data={"name": "Кая (копия)"}),
                          persona_name="kaya", trigger_words={"кая"})
    bot.lore_search = lambda *a, **kw: BotInstance.lore_search(bot, *a, **kw)
    block = lambda *a: BotInstance._lore_block(bot, *a)  # noqa: E731
    history = [{"role": "user", "content": "откуда у тебя этот шрам?"}]

    with mock.patch.object(lore, "_default_embed", side_effect=AssertionError("модель не нужна")):
        check("лора нет — ни блока, ни модели", block("откуда у тебя этот шрам?", history, False) is None)

    store = lore.store_for("kaya")
    store.add_file("kaya.md", KAYA)
    full = block("откуда у тебя этот шрам?", history, False)
    check("блок с лором", full and "латунную повязку" in full and lore.GAP_RULE in full)
    light = BotInstance.lore_search(bot, "откуда у тебя этот шрам?", light=True)
    check("в лёгком режиме кусков меньше", len(light.hits) <= lore.LIGHT_LIMIT)
    check("на болтовню — только правило", block("привет", history, False) == lore.GAP_RULE)
    with mock.patch.object(lore.LoreStore, "search", side_effect=RuntimeError("сломалось")):
        check("поломка поиска не мешает ответу", block("откуда у тебя этот шрам?", history, False) is None)

    layer = PersonaLayer.__new__(PersonaLayer)
    layer.system_prompt = "Ты капитан."
    layer.persona_data = {}
    layer.modes = {}
    prompt = layer.prepare_messages("откуда шрам?", addon_blocks=[full])
    check("блок доходит до системного промпта", "латунную повязку" in prompt[0]["content"])


# ── F ──

def test_api():
    section("F. API")
    from fastapi.testclient import TestClient
    import app.api.server as server_mod
    from app.api import settings_api
    client = TestClient(server_mod.app, base_url="http://127.0.0.1")
    bot = SimpleNamespace(lore_search=lambda q, p="": lore.store_for("kaya2").search(q, previous=p, name="Кая"))
    with mock.patch.object(server_mod, "list_personas", return_value=["kaya2"]), \
            mock.patch.object(server_mod.runtime.registry, "get", return_value=bot):
        url = "/api/personas/kaya2/lore"
        check("чужая персона — 404", client.get("/api/personas/nobody/lore").status_code == 404)
        check("пусто", client.get(url).json()["chunks"] == 0)
        r = client.post(url, files={"file": ("kaya.md", KAYA.encode("utf-8"))})
        check("загрузка md", r.status_code == 200 and r.json()["chunks"] > 5)
        r = client.post(url, files={"file": ("cp.txt", "Старый файл в кодировке Windows про шхуну.".encode("cp1251"))})
        names = [f["name"] for f in r.json()["files"]]
        check("cp1251 читается", r.status_code == 200 and "cp.txt" in names)
        src = lore.lore_dir("kaya2") / "sources"
        check("текст cp1251 без кракозябр",
              any("шхуну" in p.read_text(encoding="utf-8") for p in src.glob("*.txt")))
        r = client.post(url, files={"file": ("bad.txt", "Ошибка молодости: лор, начатый этим словом.".encode())})
        check("файл со словом «Ошибка» в начале — не отказ", r.status_code == 200)
        r = client.post(url, files={"file": ("pic.png", b"\x89PNG")})
        check("картинка — отказ с причиной", r.status_code == 400 and "txt" in r.json()["detail"])
        r = client.post(url, files={"file": ("empty.txt", b"  \n")})
        check("пустой файл — отказ", r.status_code == 400)
        with mock.patch.object(lore, "MAX_FILE_BYTES", 10):
            r = client.post(url, files={"file": ("big.txt", b"x" * 100)})
        check("большой файл — отказ", r.status_code == 400)

        r = client.post(f"{url}/search", json={"query": "откуда у тебя этот шрам?"})
        check("проверка запроса", r.status_code == 200 and any(h["section"] == "Шрам" for h in r.json()["hits"]))
        r = client.post(f"{url}/search", json={"query": "привет"})
        check("проверка болтовни: причина видна", r.json()["skipped"] == "smalltalk" and not r.json()["hits"])
        check("пустой запрос — 422", client.post(f"{url}/search", json={"query": ""}).status_code == 422)

        # Просмотр файла
        r = client.get(f"{url}/kaya.md/view")
        v = r.json()
        heads = [b["t"] for b in v.get("blocks", []) if b.get("h")]
        check("просмотр: текст с заголовками, как их увидела нарезка",
              r.status_code == 200 and "Шрам" in heads and v["ext"] == "md" and v["original"] is True)
        check("просмотр: куски — те же, что в индексе",
              len(v["pieces"]) == v["chunks"] and any(p["s"] == "Шрам" for p in v["pieces"]))
        r = client.get(f"{url}/kaya.md/original")
        check("исходник md — скачать, байт в байт, nosniff",
              r.status_code == 200 and r.content == KAYA.encode("utf-8")
              and r.headers["content-disposition"].startswith("attachment")
              and r.headers.get("x-content-type-options") == "nosniff")
        from app.api import lore_api
        pdf_bytes = b"%PDF-1.4 fake"
        with mock.patch.object(lore_api, "extract_text", return_value="Якорь\n\nКая нашла якорь на дне бухты Ржавой."):
            r = client.post(url, files={"file": ("doc.pdf", pdf_bytes)})
        check("pdf загружен", r.status_code == 200 and any(f["name"] == "doc.pdf" and f.get("original") == len(pdf_bytes)
                                                          for f in r.json()["files"]))
        r = client.get(f"{url}/doc.pdf/original")
        check("исходник pdf — встроенно, тип pdf, sandbox",
              r.status_code == 200 and r.content == pdf_bytes and r.headers["content-type"] == "application/pdf"
              and r.headers["content-disposition"].startswith("inline")
              and "sandbox" in r.headers.get("content-security-policy", ""))
        with mock.patch.object(lore_api, "extract_text", return_value="Бухта\n\nТекст страницы про бухту и маяк."):
            r = client.post(url, files={"file": ("page.html", b"<script>alert(1)</script><p>x</p>")})
        r = client.get(f"{url}/page.html/original")
        check("исходник html — только скачать, не как страница",
              r.status_code == 200 and r.headers["content-type"].startswith("application/octet-stream")
              and r.headers["content-disposition"].startswith("attachment"))
        st = lore.store_for("kaya2")
        with mock.patch.object(lore_api, "extract_text", return_value="Якорь\n\nНовый текст про якорь и цепь."):
            client.post(url, files={"file": ("doc.pdf", b"%PDF-1.4 v2")})
        origs = list((lore.lore_dir("kaya2") / "sources").glob(lore._file_key("doc.pdf") + ".orig*"))
        check("замена: один исходник, новый", len(origs) == 1 and origs[0].read_bytes() == b"%PDF-1.4 v2")
        check("удаление pdf убирает и исходник",
              client.delete(f"{url}/doc.pdf").status_code == 200
              and not list((lore.lore_dir("kaya2") / "sources").glob(lore._file_key("doc.pdf") + ".orig*")))
        st.original_path("kaya.md").unlink()
        r = client.get(f"{url}/kaya.md/view")
        check("файл без исходника (загружен раньше): текст есть, исходника нет",
              r.status_code == 200 and r.json()["original"] is False
              and client.get(f"{url}/kaya.md/original").status_code == 404)
        check("просмотр чужого файла — 404", client.get(f"{url}/nope.md/view").status_code == 404)
        check("оглавление txt: короткая строка-заголовок",
              lore.outline("Детство\n\n" + "Длинный абзац о детстве, который явно длиннее восьмидесяти знаков, чтобы строка выше стала заголовком.")[0] == {"h": 4, "t": "Детство"})
        check("удаление", client.delete(f"{url}/cp.txt").status_code == 200)
        check("удаление чужого — 404", client.delete(f"{url}/cp.txt").status_code == 404)
        rules = [m for m in server_mod.app.user_middleware if m.cls.__name__ == "BodySizeLimit"]
        check("лимит тела запроса", any("lore" in str(m.kwargs) or "lore" in str(m.args) for m in rules))

    src_yaml = TMP / "personas" / "kaya3.yaml"
    src_yaml.parent.mkdir(exist_ok=True)
    src_yaml.write_text("id: kaya3\nname: Кая\nsystem_prompt: Ты капитан.\n", encoding="utf-8")
    with mock.patch.object(settings_api, "_persona_yaml_path",
                           side_effect=lambda p: src_yaml.parent / f"{p}.yaml"):
        lore.store_for("kaya3").add_file("kaya.md", KAYA)
        result = settings_api.duplicate_persona("kaya3")
    check("копия персоны создана", bool(result and result.get("ok")))
    copied = result["persona"]
    check("копия — с лором", lore.store_for(copied).info()["chunks"] == lore.store_for("kaya3").info()["chunks"])
    lore.store_for(copied).remove_file("kaya.md")
    check("лор копии свой", lore.store_for("kaya3").has_lore())
    dirs = settings_api._persona_data_dirs("kaya3")
    check("папка лора — внутри папки памяти (едет при смене id)",
          any(lore.lore_dir("kaya3").resolve().is_relative_to(d.resolve()) for d in dirs))
    (TMP / "data" / "api_kaya3").rename(TMP / "data" / "api_kaya4")
    check("после смены папки старый id пуст, новый видит лор",
          not lore.store_for("kaya3").has_lore() and lore.store_for("kaya4").has_lore())
    check("папка только с лором считается памятью (спросят при новой персоне)",
          settings_api._holds_memory(TMP / "data" / "api_kaya4"))


# ── G ──

def test_files():
    section("G. Обычные файлы")
    from app.core.file_vector_db import CHUNK_CHARS, CHUNK_OVERLAP, FileVectorDB
    db = FileVectorDB.__new__(FileVectorDB)
    text = " ".join(lore.clean_text(KAYA).split()) * 4
    chunks = db._split_content(text)
    check("текст покрыт полностью", "".join(c[:CHUNK_CHARS - CHUNK_OVERLAP] for c in chunks).startswith(text[:2000]))
    tokenizer = lore._embedder()._model.tokenizer
    limit = lore._embedder()._model.max_seq_length
    seen = np.zeros(len(text), dtype=bool)
    step = CHUNK_CHARS - CHUNK_OVERLAP
    for i, chunk in enumerate(chunks):
        enc = tokenizer(chunk, truncation=True, max_length=limit, return_offsets_mapping=True)
        seen[i * step:i * step + max(e for _, e in enc["offset_mapping"])] = True
    check(f"модель читает весь текст ({seen.mean():.0%})", seen.mean() > 0.995)
    sizes = [len(tokenizer.encode(lore.embed_text(c))) for c in lore.chunk_text(KAYA * 3)]
    check(f"кусок лора влезает в модель: до {max(sizes)} токенов из {limit}", max(sizes) <= limit)


# ── H ──

NODE_CODE = """
import { registerHooks } from 'node:module';
registerHooks({
  resolve(spec, ctx, next) {
    try { return next(spec, ctx); }
    catch (e) {
      if (spec.startsWith('.') && !spec.endsWith('.ts')) return next(spec + '.ts', ctx);
      throw e;
    }
  },
});
const lore = await import(%s);
const recall = await import(%s);
const prompt = await import(%s);
const input = JSON.parse(%s);
const index = lore.buildLoreIndex(input.data, true);
const out = {
  stems: input.words.map((w) => recall.stemWord(w)),
  terms: input.texts.map((t) => recall.terms(t)),
  forms: input.cases.map((c) => lore.loreQueryTexts(c.query, c.previous, input.name)),
  search: input.cases.map((c) => {
    const vectors = c.vectors.map((v) => Float32Array.from(v));
    const r = lore.searchLore(index, { query: c.query, previous: c.previous, name: input.name, smalltalk: c.smalltalk, vectors });
    return { hits: r.hits.map((h) => h.index), weak: r.weak, skipped: r.skipped };
  }),
  words: input.cases.map((c) => lore.searchLore(index, { query: c.query, previous: c.previous, name: input.name, smalltalk: c.smalltalk }).hits.length),
  block: lore.loreBlock(input.rules, { hits: [{ index: 1, section: 'Брат', text: 'Раз.' }, { index: 2, section: 'Брат', text: 'Два.' }], weak: false, skipped: null }),
  gap: lore.loreBlock(input.rules, { hits: [], weak: false, skipped: 'smalltalk' }),
  none: lore.loreBlock(input.rules, { hits: [], weak: false, skipped: 'no_lore' }),
  previous: lore.previousTurn([{ role: 'user', text: 'кто ты?' }, { role: 'assistant', text: 'Капитан.' }]),
  local: prompt.localPrompt(input.kit, { recall: [], facts: [], lore: { hits: input.localHits, weak: false, skipped: null } }, [], [], 'откуда шрам?'),
};
console.log(JSON.stringify(out));
"""


def test_phone():
    section("H. Телефон без ядра (lore.ts под Node)")
    import shutil as sh
    import subprocess
    if not sh.which("node"):
        print("  (пропущено — нет Node)")
        return
    from app.api import offline_api
    st = lore.store_for("kaya_phone")
    st.add_file("kaya.md", KAYA)
    data = offline_api.build_lore("kaya_phone")
    check("лор для телефона: куски с векторами", len(data["chunks"]) == st.info()["chunks"]
          and all(len(c["v"]) == 512 for c in data["chunks"]) and data["vectors"]["dim"] == 384)
    check("метка меняется с лором", data["stamp"] == st.stamp() and st.stamp() != "")
    check("без лора телефону пусто", offline_api.build_lore("nobody")["chunks"] == []
          and offline_api.build_lore("nobody")["vectors"] is None)

    name = "Кая"
    queries = [("откуда у тебя этот шрам?", ""), ("кто такой Осип Рваный?", ""), ("почему не пьёшь кофе?", ""),
               ("что за компас у тебя?", ""), ("расскажи про адмирала Кросса", ""), ("как приготовить борщ?", ""),
               ("привет", ""), ("ты любишь пиццу?", ""), ("а тело нашли?", "ты скучаешь по брату? Каждый день."),
               ("она ещё жива?", "кто тебя научил стрелять? Старуха Марта.")]
    cases = []
    for query, previous in queries:
        forms = [query, lore.vector_query(query, name)]
        if previous:
            forms.append(lore.vector_query(f"{previous} {query}", name))
        vecs = lore._normalize(lore._default_embed(forms))
        cases.append({"query": query, "previous": previous, "smalltalk": lore._is_smalltalk(query, [name]),
                      "vectors": vecs.tolist()})
    words = ["уведомления", "кота", "Ёлка", "cats", "reading", "шхуну", "Осипа"]
    texts = [KAYA[:600], "The captain's old ship sails at dawn", "кто-то 15 да"]
    rules = {"lore_head": lore.HEAD, "lore_weak": lore.WEAK_HEAD, "lore": lore.RULE, "lore_gap": lore.GAP_RULE}
    kit = {"parts": {"character": "Ты капитан.", "rules": "", "style": "", "language": ""}, "facts": [],
           "rules": rules, "version": 3, "name": name, "persona": "kaya"}
    payload = {"data": data, "name": name, "cases": cases, "words": words, "texts": texts, "rules": rules, "kit": kit,
               "localHits": [{"index": i, "section": "Шрам", "text": f"Кусок {i}."} for i in (1, 5, 9, 13)]}
    offline = Path("web/src/offline").resolve()
    code = NODE_CODE % tuple(json.dumps(x) for x in (
        (offline / "lore.ts").as_uri(), (offline / "recall.ts").as_uri(), (offline / "prompt.ts").as_uri(),
        json.dumps(payload, ensure_ascii=False)))
    res = subprocess.run(["node", "--input-type=module", "-e", code], capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        check(f"Node: {res.stderr.strip()[:300]}", False)
        return
    out = json.loads(res.stdout.strip().splitlines()[-1])
    check("основа слова — как на телефоне", out["stems"] == [lore.stem_word(w) for w in words])
    check("слова темы — как на телефоне", out["terms"] == [lore.terms(t) for t in texts])
    check("запросы к векторам — как на телефоне", out["forms"] == [
        [q, lore.vector_query(q, name)] + ([lore.vector_query(f"{p} {q}", name)] if p else []) for q, p in queries])
    for (query, previous), got in zip(queries, out["search"]):
        want = st.search(query, previous=previous, name=name)
        same = (set(got["hits"]) == {h.index for h in want.hits} and got["weak"] == want.weak
                and got["skipped"] == want.skipped)
        check(f"«{query}»: телефон находит то же, что ядро", same)
    check("без модели векторов — по словам, болтовня не тянет лор",
          out["words"][queries.index(("откуда у тебя этот шрам?", ""))] > 0
          and out["words"][queries.index(("привет", ""))] == 0
          and out["words"][queries.index(("как приготовить борщ?", ""))] == 0)
    check("блок — как у ядра", out["block"] == lore.build_block(lore.LoreResult(hits=[
        lore.LoreHit(1, "f", "Брат", "Раз.", 0.5, False), lore.LoreHit(2, "f", "Брат", "Два.", 0.5, False)])))
    check("без находок — правило; без лора — ничего", out["gap"] == lore.GAP_RULE and out["none"] == "")
    check("прошлая реплика — как у ядра", out["previous"] == lore.previous_turn(
        [{"role": "user", "content": "кто ты?"}, {"role": "assistant", "content": "Капитан."},
         {"role": "user", "content": "дальше"}]))
    local = out["local"]
    check("Gemma: куски в реплике, не больше трёх", "Кусок 1." in local["text"] and "Кусок 13." not in local["text"])
    check("Gemma: правило против выдумки в системном тексте", lore.GAP_RULE in local["system"])


# ── I ──

NODE_CHUNK = """
const chunk = await import(%s);
const docx = await import(%s);
const vectors = await import(%s);
const input = JSON.parse(%s);
const buf = Buffer.from(input.docx, 'base64');
const text = await docx.docxText(buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength));
console.log(JSON.stringify({
  chunks: input.texts.map((t) => chunk.chunkText(t)),
  outline: input.texts.map((t) => chunk.outline(t)),
  clean: input.texts.map((t) => chunk.cleanText(t)),
  names: input.names.map((n) => chunk.cleanFilename(n)),
  docx: text,
  docxChunks: chunk.chunkText(text),
  packed: input.vecs.map((v) => vectors.packVec(v)),
}));
"""


def _docx_bytes() -> bytes:
    # Минимальный .docx как из русского Word: id стилей — «1», «2», имена —
    # английские; заголовок, таблица, список, перевод строки, сущности
    import io
    import zipfile
    W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'

    def para(text, style=None, num=False, extra=""):
        ppr = ""
        if style or num:
            ppr = "<w:pPr>" + (f'<w:pStyle w:val="{style}"/>' if style else "") + \
                  ('<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>' if num else "") + "</w:pPr>"
        return f'<w:p w:rsidR="00A1">{ppr}<w:r><w:t xml:space="preserve">{text}</w:t></w:r>{extra}</w:p>'

    long = "Кая выросла на шхуне «Ржавая чайка» и с детства умела вязать морские узлы. " * 4
    body = "".join([
        para("Кая — досье", "Title"),
        para("Детство", "1"),
        para(long),
        '<w:p/>',
        para("Шрам", "2"),
        para("Шрам над бровью Кая получила в драке за компас &amp; карту &lt;Кросса&gt;."),
        para("Компас", "2"),
        para("Компас всегда врёт на два градуса", None, False, '<w:r><w:br/><w:t>и Кая это знает.</w:t></w:r>'),
        para("стреляет без промаха", None, True),
        '<w:tbl><w:tr><w:tc>' + para("Имя") + '</w:tc><w:tc>' + para("Кая") + '</w:tc></w:tr>'
        '<w:tr><w:tc>' + para("Корабль") + '</w:tc><w:tc>' + para("«Ржавая чайка»") + '</w:tc></w:tr></w:tbl>',
        para("Конец досье про Каю и её корабль, длинный абзац, чтобы строка выше не была заголовком никогда."),
    ])
    styles = (f'<w:styles {W}>'
              '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/></w:style>'
              '<w:style w:type="paragraph" w:styleId="1"><w:name w:val="heading 1"/></w:style>'
              '<w:style w:type="paragraph" w:styleId="2"><w:name w:val="heading 2"/></w:style>'
              '</w:styles>')
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                   '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
                   '</Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
                   '</Relationships>')
        z.writestr("word/_rels/document.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                   '</Relationships>')
        z.writestr("word/document.xml", f'<?xml version="1.0" encoding="UTF-8"?><w:document {W}><w:body>{body}</w:body></w:document>')
        z.writestr("word/styles.xml", f'<?xml version="1.0" encoding="UTF-8"?>{styles}')
    return out.getvalue()


def test_phone_upload():
    section("I. Лор, загруженный на телефоне (loreChunk.ts, docx.ts под Node)")
    import base64
    import shutil as sh
    import subprocess
    if not sh.which("node"):
        print("  (пропущено — нет Node)")
        return
    from app.api import lore_api, offline_api
    long_word = "Ланкастерширскаядолгаяфамилия" * 20
    texts = [
        KAYA,
        KAYA * 3,
        "Детство\n\n" + "Кая родилась на шхуне. " * 30 + "\n\nВозраст: 31\n\n— Ты кто? — спросил он.\nПочти через минуту он спросил:\n\nОтвет",
        "# Том\n## Часть\n### Глава\n#### Сцена\nТекст сцены.\n### Другая глава\nЕщё текст.\n**Жирный заголовок:**\nАбзац под ним.",
        "Строка\r\nс CRLF\rи CR\u00a0и nbsp\tи таб\u200bневидимый\u00ad ![картинка](x.png) конец",
        "Длинное предложение без точек, но с запятыми, " * 20 + "и тире — " * 10,
        long_word,
        "Эмодзи 😀 в начале 🚢 и в конце 🐙\n\n" + "😀" * 400,
        "",
    ]
    names = ["C:\\fakepath\\Досье.docx", "a/b/лор.md", "  ", "Йод.txt", "ё" * 300 + ".md"]
    import numpy as np
    rng = np.random.default_rng(7)
    vecs = [rng.normal(size=384).astype(np.float32) for _ in range(3)]
    raw = _docx_bytes()
    payload = {"texts": texts, "names": names, "docx": base64.b64encode(raw).decode(),
               "vecs": [v.tolist() for v in vecs]}
    offline = Path("web/src/offline").resolve()
    code = NODE_CHUNK % tuple(json.dumps(x) for x in (
        (offline / "loreChunk.ts").as_uri(), (offline / "docx.ts").as_uri(), (offline / "vectors.ts").as_uri(),
        json.dumps(payload, ensure_ascii=False)))
    res = subprocess.run(["node", "--input-type=module", "-e", code], capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        check(f"Node: {res.stderr.strip()[:300]}", False)
        return
    out = json.loads(res.stdout.strip().splitlines()[-1])
    for i, t in enumerate(texts):
        label = repr(t[:24])
        check(f"чистка {label} — как у ядра", out["clean"][i] == lore.clean_text(t))
        check(f"нарезка {label} — как у ядра", out["chunks"][i] == lore.chunk_text(t))
        check(f"оглавление {label} — как у ядра", out["outline"][i] == lore.outline(t))
    check("имена файлов — как у ядра", out["names"] == [lore.clean_filename(n) for n in names])
    check("векторы сжимаются как у ядра", out["packed"] == [offline_api.pack_vector(v) for v in vecs])

    text = out["docx"]
    check("docx: заголовки по стилям русского Word («1», «2» → heading)",
          "# Кая — досье" in text and "\n# Детство" in text and "\n## Шрам" in text)
    check("docx: сущности, перевод строки, список, таблица",
          "&" in text and "<Кросса>" in text and "градуса\nи Кая" in text
          and "- стреляет без промаха" in text and "Имя | Кая" in text and "Корабль | «Ржавая чайка»" in text)
    core = lore.chunk_text(lore_api.read_text("kaya.docx", raw))
    phone = out["docxChunks"]
    check(f"docx: разделы — как у разбора ядра ({[c['s'] for c in phone]})",
          [c["s"] for c in phone] == [c["s"] for c in core])


def main():
    try:
        test_chunking()
        test_store()
        test_search()
        test_block()
        test_turn()
        test_api()
        test_files()
        test_phone()
        test_phone_upload()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\nПроверок: {ok}, провалов: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
