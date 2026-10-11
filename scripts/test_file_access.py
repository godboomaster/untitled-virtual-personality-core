"""«Открой файл X» / «пришли файл про Y» (app/features/file_finder,
app/features/file_access, app/api/file_links):

  A. разбор просьбы: глагол в начале, признак «файл»/тип/расширение/место,
     связки «про»/«под названием», чужое («открой ютуб», «сайт с файлами»,
     «пришли фотку» персоне) — не наше;
  B. пути: системные/служебные/скрытые папки, секреты, данные ядра — нет;
     облачные диски внутри ~/Library — да; программы не «открываются»;
  C. поиск обходом (без индекса) во временной домашней папке: ранжирование,
     копии «(1)», тип, папка, «последний из загрузок»;
  D. ход: лидер — сразу; список — номер/«да»/«нет»/мимо; повторная проверка
     файла; лимит размера; программа — показать в папке;
  E. бот: только владелец, не из скина, не в группе Telegram, без канала —
     не перехватывает; бакет файлов хода; список гасит подтверждение
     режима управления и наоборот;
  F. API: открыть — только локальному вебу; телефон/VPC Link/другой адрес —
     прислать; ссылка /api/files/{id} отдаёт файл, история — снова со ссылкой.

Модели, индекс системы и сеть не вызываются; всё — во временных папках.
Запуск: PYTHONPATH=. python3 -m scripts.test_file_access
"""

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

_TMP = Path(os.path.realpath(tempfile.mkdtemp(prefix="file_access_")))
os.environ["VPC_DATA_DIR"] = str(_TMP / "data")
os.environ["OLLAMA_URL"] = "http://127.0.0.1:9"

from app.features import file_finder as ff  # noqa: E402
from app.features import file_access as fa  # noqa: E402

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


# Временная «домашняя папка»: поиск идёт обходом, без Spotlight
HOME = _TMP / "home"
ff._HOME = HOME
ff._backend = lambda: "walk"


def make(rel: str, data: bytes = b"x", age_days: float = 0.0, mode=None) -> Path:
    p = HOME / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if age_days:
        t = time.time() - age_days * 86400
        os.utime(p, (t, t))
    if mode is not None:
        os.chmod(p, mode)
    return p


def test_parse():
    section("A. Разбор просьбы")
    P = ff.parse_file_request
    r = P("открой файл отчёт по лабораторной")
    check("«открой файл отчёт по лабораторной» — open, основы без предлога",
          r and r.verb == "open" and r.terms == ["отчет", "лабораторн"] and r.mode == "name")
    r = P("Коннор, пожалуйста, можешь открыть файл про налоговый вычет?")
    check("обращение + вежливость + «про» — по содержимому",
          r and r.verb == "open" and r.mode == "topic" and r.terms == ["налогов", "вычет"])
    r = P("пришли мне pdf методички")
    check("«пришли pdf …» — send, только pdf", r and r.verb == "send" and r.exts == {"pdf"} and r.strict_ext)
    r = P("скинь последний скриншот с рабочего стола")
    check("«последний скриншот с рабочего стола» — картинки, Desktop, свежий",
          r and r.recent and r.roots == [HOME / "Desktop"] and "png" in r.exts and r.terms == [])
    r = P("открой отчёт.docx")
    check("имя с расширением без слова «файл» — наше", r and r.exts == {"docx"} and r.terms == ["отчет"])
    r = P("открой мне этот файл Отчёт_Лабораторная_работа_1")
    check("подчёркивания делят слова", r and r.terms == ["отчет", "лабораторн", "работ", "1"])
    r = P("открой документ под названием «План на неделю»")
    check("кавычки — точное имя", r and r.exact == "План на неделю" and r.mode == "name")
    r = P("открой файл из папки smad")
    check("«из папки X» без слов — папка", r and r.folder == "smad" and r.terms == [])
    r = P("пришли презентацию")
    check("«пришли презентацию» без названия — переспросить", r and r.need_name)
    r = P("send me the file about tax deduction")
    check("EN: send … about — topic", r and r.verb == "send" and r.mode == "topic" and r.terms == ["tax", "deduction"])
    r = P("find and send me my resume file")
    check("EN: «file» последним словом", r and r.verb == "send" and r.terms == ["resume"])
    r = P("open the budget spreadsheet")
    check("EN: таблица — xlsx/numbers/…", r and "xlsx" in r.exts and r.terms == ["budget"])
    for text in ("открой ютуб", "открой видео про котов", "пришли фотку", "открой сайт с файлами",
                 "открой ютуб и файлы", "я вчера открыл файл", "открой readme.md",
                 "пришли ссылку на файл", "расскажи про файл", "открой файлы"):
        check(f"не наше: «{text}»", P(text) is None)
    check("основа: «лабораторную» → «лабораторн»", ff.stem("лабораторную") == "лабораторн")
    check("основа: «лабе» → «лаб»", ff.stem("лабе") == "лаб")
    check("основа: «reports» → «report»", ff.stem("reports") == "report")


def test_paths():
    section("B. Пути: что можно трогать")
    doc = make("Documents/учёба/отчёт.pdf")
    check("обычный документ — можно", ff.path_allowed(str(doc)))
    check("…и в папке «temp» глубже корня — можно", ff.path_allowed(str(make("Documents/temp/план.txt"))))
    check("~/.ssh/id_ed25519 — нет", not ff.path_allowed(str(make(".ssh/id_ed25519"))))
    check("~/Library/Preferences — нет", not ff.path_allowed(str(make("Library/Preferences/a.plist"))))
    check("iCloud Drive внутри ~/Library — можно",
          ff.path_allowed(str(make("Library/Mobile Documents/com~apple~CloudDocs/книга.epub"))))
    check("Google Drive (~/Library/CloudStorage) — можно",
          ff.path_allowed(str(make("Library/CloudStorage/GoogleDrive-a/My Drive/табель.xlsx"))))
    check("внутри пакета .app — нет", not ff.path_allowed(str(make("Applications/Foo.app/Contents/x.pdf"))))
    check("node_modules — нет", not ff.path_allowed(str(make("code/proj/node_modules/x/readme.txt"))))
    check("скрытый файл — нет", not ff.path_allowed(str(make("Documents/.secret.txt"))))
    check(".env — нет", not ff.path_allowed(str(make("code/proj/.env"))))
    check("ключ .pem — нет", not ff.path_allowed(str(make("Documents/server.pem"))))
    check("база паролей .kdbx — нет", not ff.path_allowed(str(make("Documents/pass.kdbx"))))
    data = Path(os.environ["VPC_DATA_DIR"])
    (data / "api_x").mkdir(parents=True, exist_ok=True)
    (data / "api_x" / "notes.txt").write_text("x")
    check("папка данных ядра — нет", not ff.path_allowed(str(data / "api_x" / "notes.txt")))
    link = HOME / "Documents" / "ссылка.txt"
    if not link.exists():
        link.symlink_to(HOME / ".ssh" / "id_ed25519")
    check("симлинк на секрет — нет (по realpath)", not ff.path_allowed(str(link)))
    check("системный корень /System — нет", not ff.path_allowed("/System/Library/x.pdf"))
    check("pdf открывается", ff.openable("a.pdf"))
    for name in ("run.command", "s.sh", "x.py", "setup.exe", "a.app", "l.webloc", "p.mobileconfig", "w.js"):
        check(f"«{name}» не открывается (только прислать)", not ff.openable(name))
    exe = make("bin_tools/tool", b"#!/bin/sh\n", mode=0o755)
    check("без расширения с правом исполнения — не открывается", not ff.openable(str(exe)))


def test_search():
    section("C. Поиск обходом")
    make("Downloads/Отчёт_Лабораторная_работа_1.pdf", b"a" * 10, age_days=3)
    make("Downloads/Отчёт_Лабораторная_работа_1 (1).pdf", b"a" * 12, age_days=1)
    make("Documents/km/Лабораторная работа 2 КМ.docx", age_days=5)
    make("Documents/km/Лабораторная работа 2 КМ.pdf", age_days=6)
    make("Documents/smad/README.md", age_days=2)
    make("Documents/smad/notes.txt", age_days=1)
    make("Downloads/свежий.png", age_days=0.01)
    make("Downloads/старый.png", age_days=40)
    make("Desktop/screen1.png", age_days=2)
    make("Desktop/screen2.png", age_days=0.5)

    r = ff.parse_file_request("открой файл отчёт по лабораторной")
    hits = ff.search(r)
    leader, choices = ff.pick(hits, r)
    check("«отчёт по лабораторной» — один лидер", leader is not None and leader.name.startswith("Отчёт_"))
    check("копии «(1)» схлопнуты — остался свежий", leader and leader.name.endswith("(1).pdf")
          and sum(1 for h in hits if h.name.startswith("Отчёт_")) == 1)
    r = ff.parse_file_request("открой файл лабораторная работа 2 КМ")
    leader, choices = ff.pick(ff.search(r), r)
    check("docx и pdf одной работы — список, а не угадывание",
          leader is None and {c.name for c in choices[:2]} == {"Лабораторная работа 2 КМ.docx", "Лабораторная работа 2 КМ.pdf"})
    r = ff.parse_file_request("открой pdf лабораторная работа 2 КМ")
    leader, choices = ff.pick(ff.search(r), r)
    check("тип назван — только pdf, лидер", leader is not None and leader.name.endswith(".pdf")
          and all(c.name.endswith(".pdf") for c in choices))
    r = ff.parse_file_request("открой последний файл из загрузок")
    leader, _ = ff.pick(ff.search(r), r)
    check("последний из загрузок — самый свежий", leader is not None and leader.name == "свежий.png")
    r = ff.parse_file_request("пришли фото с рабочего стола")
    leader, choices = ff.pick(ff.search(r, for_send=True), r)
    check("фото с рабочего стола без слов — список из двух", leader is None and len(choices) == 2)
    r = ff.parse_file_request("скинь последний скриншот с рабочего стола")
    leader, _ = ff.pick(ff.search(r, for_send=True), r)
    check("последний скриншот — свежий", leader is not None and leader.name == "screen2.png")
    r = ff.parse_file_request("открой файл readme из папки smad")
    leader, _ = ff.pick(ff.search(r), r)
    check("слово + папка — файл из неё", leader is not None and leader.folder.endswith("smad"))
    make("scripts/deploy.sh", age_days=1)
    r = ff.parse_file_request("открой файл deploy.sh")
    check("скрипт для «открой» не ищется (open-поиск)", not ff.search(r, for_send=False))
    check("…а для «пришли» — находится", any(h.name == "deploy.sh" and not h.openable
                                             for h in ff.search(r, for_send=True)))
    r = ff.parse_file_request("открой файл фывапролдж")
    check("нет такого — пусто", ff.pick(ff.search(r), r) == (None, []))
    check("папка показа — с «~»", ff.display_folder(str(HOME / "Documents" / "a.pdf")) == "~/Documents")
    r = ff.parse_file_request("открой файл отчёт 1")
    q = dict(ff._mdfind_queries(r))
    check("Spotlight: имя — все основы подстрокой без регистра и ё, с синонимами",
          '((kMDItemFSName == "*отчет*"cd) || (kMDItemFSName == "*report*"cd)) && '
          '((kMDItemFSName == "*1*"cd))' in q["name"] and "public.folder" in q["name"])
    check("Spotlight: содержимое — без чисел, префиксом слова",
          q["content"].startswith('(kMDItemTextContent == "отчет*"cdw)') and '"1*"' not in q["content"])
    sql = dict(ff._ps_sql(r))
    check("Windows Search: имя через LIKE, папки исключены",
          "System.FileName LIKE '%отчет%'" in sql["name"] and "System.ItemType <> 'Directory'" in sql["name"])
    check("Windows Search: содержимое через CONTAINS с префиксом",
          "CONTAINS(System.Search.Contents, '\"отчет*\"')" in sql["content"])
    check("кавычки и % из основ не попадают в запрос", ff._safe_term("a'b\"c%_*") == "abc")


def test_hints():
    section("C2. Уточнение папками: «методичку в db в LabS7»")
    P = ff.parse_file_request
    r = P("открой методичку в db в LabS7")
    check("без слова «файл», с папками — мягкая просьба, подсказки",
          r and r.soft and r.terms == ["методичк"] and set(r.hints) == {"db", "LabS7"})
    r = P("пришли файл отчёт в телеграм")
    check("«в телеграм» — куда прислать, не папка", r and r.hints == [] and r.terms == ["отчет"])
    r = P("открой отчёт в формате pdf")
    check("«в формате pdf» — тип, не папка и не слово", r and r.hints == [] and r.terms == ["отчет"]
          and r.exts == {"pdf"})
    check("«открой ютуб» без папок — не наше", P("открой ютуб") is None)
    check("похожесть: «LabS7» ≈ «LabsS7» больше, чем «LabsS7_1»",
          ff.folder_sim("LabS7", "LabsS7") > ff.folder_sim("LabS7", "LabsS7_1") >= ff.FOLDER_MIN_SIM)
    check("похожесть: «дб» = «db», «учёбе» ≈ «Учёба», «db» ≠ «dbt»",
          ff.folder_sim("дб", "db") == 1.0 and ff.folder_sim("учёбе", "Учёба") > 0.9
          and ff.folder_sim("db", "dbt") == 0.0)
    check("синоним: «методичка» — «УП_Работа…» (целое слово)",
          ff._term_score("методичк", "уп_работа_с_бд_2025.pdf", {"уп", "работа", "с", "бд", "2025"}) == 0.85)
    check("синоним: короткий — с номером («lek1»), но не внутри слова («группа»)",
          ff._term_score("лекци", "lek1_bd.pdf", {"lek1", "bd"}) == 0.85
          and ff._term_score("методичк", "группа.pdf", {"группа"}) == 0.0)

    make("Documents/LabsS7/db/materials/УП_Работа_с_БД_2025.pdf", age_days=10)
    make("Documents/LabsS7/db/materials/lek1_bd.pdf", age_days=9)
    make("Documents/LabsS7/db/README.md", "см. методичку".encode("utf-8"), age_days=1)
    make("Documents/LabsS7_1/db/materials/УП_Работа_с_БД_2025.pdf", age_days=20)
    make("Documents/LabsS7/smad/materials/УП_Статистика.pdf", age_days=5)
    m = fa.FileAccess(base_dir=None)
    reply, files = m.turn("пришли методичку в db в LabS7", "h1", "u1", "send", lang="ru")
    check("«методичку в db в LabS7» — УП из LabsS7/db, не из LabsS7_1 и не из smad",
          len(files) == 1 and files[0]["path"].endswith("LabsS7/db/materials/УП_Работа_с_БД_2025.pdf"))
    reply, files = m.turn("пришли методичку в LabS7 в db", "h1", "u1", "send", lang="ru")
    check("порядок папок не важен", len(files) == 1 and "LabsS7/db/" in files[0]["path"])
    reply, files = m.turn("пришли лекцию в дб в LabS7", "h1", "u1", "send", lang="ru")
    check("«в дб» — папка db, «лекцию» — lek1", len(files) == 1 and files[0]["name"] == "lek1_bd.pdf")
    reply, files = m.turn("пришли методичку в db в LabsS7_1", "h1", "u1", "send", lang="ru")
    check("точное «LabsS7_1» — она", len(files) == 1 and "LabsS7_1/db/" in files[0]["path"])
    check("папки нет — мягкая просьба не наша («открой поиск в ютубе»)",
          m.turn("открой поиск в ютубе", "h1", "u1", "open", lang="ru") is None)
    check("папка есть, но файла с таким именем нет — тоже не наша",
          m.turn("открой корзину в db", "h1", "u1", "open", lang="ru") is None)
    reply, files = m.turn("открой файл отчёт в LabX9", "h1", "u1", "send", lang="ru")
    check("со словом «файл» и несуществующей папкой — «папки не нашлось»",
          reply.startswith("Не нашлось папки «LabX9»") and files == [])
    reply, files = m.turn("пришли файл в db", "h1", "u1", "send", lang="ru")
    check("«файл в db» без слов — список файлов папки", reply.startswith("Нашлось несколько")
          or (files and "/db/" in files[0]["path"]))
    m.clear("h1")


class Spy:
    def __init__(self):
        self.opened = []
        self.revealed = []


def test_turn():
    section("D. Ход: лидер, список, ответ")
    spy = Spy()
    orig_open, orig_reveal = ff.open_file, fa.reveal
    ff.open_file = lambda p: spy.opened.append(p)
    fa.reveal = lambda p: spy.revealed.append(p)
    base = _TMP / "audit"
    m = fa.FileAccess(base_dir=base)
    try:
        reply, files = m.turn("открой файл отчёт по лабораторной", "c1", "u1", "open", lang="ru")
        check("open + лидер — открыт сразу, файлов в чат нет",
              len(spy.opened) == 1 and spy.opened[0].endswith("(1).pdf") and files == []
              and reply.startswith("Открываю «Отчёт_"))
        reply, files = m.turn("пришли файл отчёт по лабораторной", "c1", "u1", "send", lang="ru")
        check("send + лидер — файл в бакет, «Вот …»",
              len(files) == 1 and files[0]["name"].endswith("(1).pdf") and reply.startswith("Вот «"))
        check("отправка файл не трогает (на месте)", Path(files[0]["path"]).exists())
        reply, files = m.turn("open the file zxqwerty", "c1", "u1", "open", lang="en")
        check("EN: ничего — честный отказ по-английски", reply.startswith("I couldn't find") and files == [])

        reply, files = m.turn("пришли файл лабораторная работа 2 КМ", "c2", "u1", "send", lang="ru")
        check("несколько — список с номерами", reply.startswith("Нашлось несколько файлов — какой прислать?")
              and "\n1. " in reply and "\n2. " in reply and files == [])
        check("в историю — заглушка без имён файлов",
              m.history_text(reply) == "Нашлось несколько файлов (2) — выбор показан в чате.")
        check("…одноразово: обычный ответ — как есть", m.history_text(reply) == reply)
        opts = m.answer_options("c2", "u1")
        check("кнопки веба — номера и «нет», scope files", opts and opts["options"][0]["send"] == "1"
              and opts["options"][-1] == {"role": "no"} and opts.get("scope") == "files")
        check("чужому пользователю кнопок нет", m.answer_options("c2", "u2") is None)
        reply, _ = m.turn("7", "c2", "u1", "send", lang="ru")
        check("номер вне списка — переспрос, список жив", reply.startswith("Варианта 7 нет") and m.has_pending("c2"))
        reply, files = m.turn("2", "c2", "u1", "send", lang="ru")
        check("«2» — второй файл", len(files) == 1 and files[0]["name"] == "Лабораторная работа 2 КМ.pdf"
              and not m.has_pending("c2"))
        m.turn("пришли файл лабораторная работа 2 КМ", "c2", "u1", "send", lang="ru")
        reply, files = m.turn("да", "c2", "u1", "send", lang="ru")
        check("«да» — первый", len(files) == 1 and files[0]["name"] == "Лабораторная работа 2 КМ.docx")
        m.turn("открой файл лабораторная работа 2 КМ", "c2", "u1", "open", lang="ru")
        reply, files = m.turn("нет", "c2", "u1", "open", lang="ru")
        check("«нет» — отмена", reply == "Хорошо, не открываю." and not m.has_pending("c2"))
        m.turn("открой файл лабораторная работа 2 КМ", "c2", "u1", "open", lang="ru")
        check("посторонняя реплика — не наша, список снят",
              m.turn("как дела?", "c2", "u1", "open", lang="ru") is None and not m.has_pending("c2"))
        m.turn("открой файл лабораторная работа 2 КМ", "c2", "u1", "open", lang="ru")
        check("подтверждение режима управления новее — «2» не наше",
              m.turn("2", "c2", "u1", "open", lang="ru", foreign_pending=True) is None)
        m.turn("открой файл лабораторная работа 2 КМ", "c3", "u1", "open", lang="ru")
        m._pending["c3"]["expires"] = time.time() - 1
        check("список протух — «1» не наше", m.turn("1", "c3", "u1", "open", lang="ru") is None)

        gone = make("Documents/исчезнет/уходящий отчёт.txt")
        gone.unlink()
        hit = ff.FileHit(path=str(gone), name=gone.name, folder="~", size=1, mtime=0, score=99, openable=True)
        reply, files = m._act(hit, "send", "c4", "ru")
        check("файл исчез между списком и ответом — честно, без отправки",
              "больше нет на месте" in reply and files == [])
        big = make("Documents/большой архив.pdf", b"0" * 2048)
        orig_max = fa.MAX_SEND_BYTES
        fa.MAX_SEND_BYTES = 1024
        try:
            reply, files = m.turn("пришли файл большой архив", "c5", "u1", "send", lang="ru")
        finally:
            fa.MAX_SEND_BYTES = orig_max
        check("больше лимита — не присылаем", "прислать не получится" in reply and files == [])
        check("большой файл на месте", big.exists())
        reply, files = m.turn("открой файл deploy.sh", "c6", "u1", "open", lang="ru")
        check("скрипт по «открой» — только показать в папке",
              spy.revealed and spy.revealed[-1].endswith("deploy.sh") and "запускать по сообщению не буду" in reply)
        make("Documents/заметки/разное.txt", "тут упоминается квазиобъект".encode("utf-8"))
        orig_walk = ff._search_walk
        ff._search_walk = lambda req: {str(HOME / "Documents/заметки/разное.txt"): {"content"}}
        try:
            reply, files = m.turn("пришли файл про квазиобъект", "c8", "u1", "send", lang="ru")
        finally:
            ff._search_walk = orig_walk
        check("одно неуверенное совпадение — «прислать его?», без «несколько»",
              reply.startswith("Нашёлся похожий файл — прислать его?") and files == [])
        check("…кнопки да/нет (и вне режима управления)", m.answer_options("c8", "u1") == {
            "kind": "yesno", "scope": "files", "options": [{"role": "yes"}, {"role": "no"}]})
        reply, files = m.turn("да", "c8", "u1", "send", lang="ru")
        check("…«да» — присылает", len(files) == 1 and files[0]["name"] == "разное.txt")
        reply, files = m.turn("пришли презентацию", "c7", "u1", "send", lang="ru")
        check("без названия — «Какой файл?»", reply.startswith("Какой файл?") and files == [])
        audit = (base / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        check("аудит пишется (открыто/отправлено/не найдено)",
              any('"action": "open"' in a for a in audit) and any('"action": "send"' in a for a in audit)
              and any('"not_found"' in a for a in audit))
    finally:
        ff.open_file, fa.reveal = orig_open, orig_reveal


def _stub_bot(web=False, owner="u1"):
    from app.bot_instance import BotInstance
    b = BotInstance.__new__(BotInstance)
    b.features = {}
    b.web_single_user = web
    b.owner = owner
    b.persona_name = "connor"
    b.context = "test"
    b.computer_control = None
    b._file_access_mgr = fa.FileAccess(base_dir=_TMP / "audit_bot")
    return b


def test_bot():
    section("E. Бот: кому можно и бакет хода")
    from app import bot_instance as bi
    b = _stub_bot()
    r = b._file_access_turn("пришли файл отчёт по лабораторной", "u1", "u1", "send", "ru")
    files = b.pop_pending_files("u1")
    check("владелец в личке Telegram — файл в бакет", r and r.startswith("Вот «") and len(files) == 1)
    check("бакет одноразовый", b.pop_pending_files("u1") == [])
    check("не владелец — не перехватываем",
          b._file_access_turn("пришли файл отчёт по лабораторной", "u2", "u2", "send", "ru") is None)
    check("группа Telegram — не перехватываем даже владельца",
          b._file_access_turn("пришли файл отчёт по лабораторной", "u1", "-100500", "send", "ru") is None)
    check("канал файлы не обслуживает (None) — не перехватываем",
          b._file_access_turn("пришли файл отчёт по лабораторной", "u1", "u1", None, "ru") is None)
    bi._SKIN_TURN.on = True
    try:
        check("ход из скина — не перехватываем",
              b._file_access_turn("пришли файл отчёт по лабораторной", "u1", "u1", "send", "ru") is None)
    finally:
        bi._SKIN_TURN.on = False
    b.features = {"file_access": False}
    check("features.file_access: false — выключено",
          b._file_access_turn("пришли файл отчёт по лабораторной", "u1", "u1", "send", "ru") is None)
    w = _stub_bot(web=True, owner="")
    r = w._file_access_turn("пришли файл лабораторная работа 2 КМ", "web_user", "web_user", "send", "ru")
    check("веб (однопользовательский) — владелец всегда, список", r and r.startswith("Нашлось несколько"))
    opts = w.cc_answer_options("web_user", "web_user")
    check("кнопки списка — и вне режима управления", opts and opts["kind"] == "options")

    class FakeCC:
        def __init__(self):
            self.pending = {}
            self.cleared = []

        def get_pending(self, chat_id, user_id=None):
            return self.pending.get(chat_id)

        def clear_pending(self, chat_id):
            self.cleared.append(chat_id)
            self.pending.pop(chat_id, None)

    cc = FakeCC()
    w.computer_control = cc
    cc.pending["web_user"] = {"kind": "url"}
    w._file_access_turn("пришли файл лабораторная работа 2 КМ", "web_user", "web_user", "send", "ru")
    check("наш список гасит старое подтверждение режима управления", "web_user" in cc.cleared)
    cc.pending["web_user"] = {"kind": "url"}   # режим управления спросил позже
    check("…а более новое подтверждение получает «2» само",
          w._file_access_turn("2", "web_user", "web_user", "send", "ru") is None)
    b2 = _stub_bot()
    del b2._file_access_mgr
    check("заготовка без менеджера — создаётся лениво", b2._file_access() is not None)


def test_api():
    section("F. API: канал, ссылка, история")
    try:
        from fastapi.testclient import TestClient
    except ImportError as e:
        print(f"  (пропущено — fastapi/httpx недоступны: {e})")
        return
    import app.api.server as server_mod
    from app.api import file_links
    from app.api.schemas import ChatRequest

    def req(**kw):
        return ChatRequest(persona="connor", message="x", **kw)

    def rq(host="127.0.0.1:8000", **headers):
        return SimpleNamespace(headers={"host": host, **headers})

    D = server_mod._file_delivery
    check("веб на этом компьютере — открыть", D(rq(), req()) == "open")
    check("localhost — открыть", D(rq("localhost:8000"), req()) == "open")
    check("адрес в сети (телефон по Wi-Fi) — прислать", D(rq("192.168.1.5:8000"), req()) == "send")
    check("VPC Link — прислать", D(rq(**{"x-vpc-link": "dev1"}), req()) == "send")
    check("приложение (client=app) — прислать", D(rq(), req(client="app")) == "send")
    check("через прокси — прислать", D(rq(**{"x-forwarded-for": "1.2.3.4"}), req()) == "send")
    check("скин — никак", D(rq(), req(from_skin=True)) is None)

    target = make("Documents/для телефона.txt", "привет с ядра".encode("utf-8"))
    seen = {}
    T = time.time()

    class FakeBot:
        features = {}
        router = SimpleNamespace(_last_provider=None, active_provider=None)

        def __init__(self):
            self.files = {}
            self.stm = []
            self.memory = SimpleNamespace(stm=SimpleNamespace(get_messages=lambda **kw: list(self.stm)))

        def process_message(self, text, file_delivery=None, chat_id=None, **kw):
            seen["delivery"] = file_delivery
            reply = f"Вот «{target.name}»"
            if file_delivery == "send":
                self.files[str(chat_id)] = [{"path": str(target), "name": target.name,
                                             "size": target.stat().st_size}]
            self.stm = [{"role": "user", "content": text, "timestamp": T - 1},
                        {"role": "assistant", "content": reply, "timestamp": T}]
            return reply

        def _turn_stm_tail(self, key):
            return self.stm

        def pop_pending_files(self, chat_id):
            return self.files.pop(str(chat_id), [])

        def pop_reply_history_text(self, chat_id):
            return seen.pop("stm_text", None)

        def pop_pending_split_messages(self, chat_id):
            return []

        def pop_pending_list_messages(self, chat_id):
            return []

        def pop_pending_question_kind(self, chat_id):
            return None

        def pop_pending_photos(self, chat_id):
            return []

        def control_mode_on(self, key):
            return False

        def cc_answer_options(self, *a):
            return None

        def cc_task_card(self, *a):
            return None

    bot = FakeBot()

    async def fake_get_bot(persona):
        return bot

    async def fake_touch(persona):
        return None

    orig = server_mod._get_bot, server_mod._api_token, server_mod._touch_persona
    server_mod._get_bot = fake_get_bot
    server_mod._touch_persona = fake_touch
    server_mod._api_token = ""
    file_links.clear()
    try:
        import json as _json
        c = TestClient(server_mod.app, base_url="http://127.0.0.1")
        body = {"persona": "connor", "message": "пришли файл", "chat_id": "web_user"}
        r = c.post("/api/chat/stream", json=body)
        final = [_json.loads(x[6:]) for x in r.text.split("\n\n") if x.startswith("data: ")][-1]
        check("локальный веб — ход с «открыть», файлов в ответе нет",
              seen.get("delivery") == "open" and final.get("files") == [])
        r = c.post("/api/chat/stream", json={**body, "client": "app"})
        final = [_json.loads(x[6:]) for x in r.text.split("\n\n") if x.startswith("data: ")][-1]
        files = final.get("files") or []
        check("приложение — «прислать», в ответе ссылка",
              seen.get("delivery") == "send" and len(files) == 1 and files[0]["name"] == target.name
              and files[0]["url"].startswith("/api/files/"))
        check("ответ лёг в историю тем же текстом — reply_stm пуст", final.get("reply_stm") is None)
        seen["stm_text"] = "заглушка"
        r = c.post("/api/chat/stream", json=body)
        last = [_json.loads(x[6:]) for x in r.text.split("\n\n") if x.startswith("data: ")][-1]
        check("ответ в истории заглушкой — reply_stm в ответе", last.get("reply_stm") == "заглушка")
        r = c.get(files[0]["url"])
        check("ссылка отдаёт файл целиком", r.status_code == 200 and r.content == target.read_bytes())
        check("…вложением с именем", "attachment" in r.headers.get("content-disposition", ""))
        h = c.get("/api/chat/history", params={"persona": "connor", "chat_id": "web_user"}).json()
        check("история: ответ снова со ссылкой", h[-1].get("files") and h[-1]["files"][0]["id"] == files[0]["id"]
              and not h[0].get("files"))
        check("чужой id — 404", c.get("/api/files/" + "A" * 32).status_code == 404)
        check("кривой id — 404", c.get("/api/files/..%2F..%2Fetc").status_code == 404)
        server_mod._api_token = "s3cr3t"
        check("с API_TOKEN без токена — 401", c.get(files[0]["url"]).status_code == 401)
        check("с токеном — 200", c.get(files[0]["url"], headers={"Authorization": "Bearer s3cr3t"}).status_code == 200)
        target.unlink()
        check("файл исчез — 410", c.get(files[0]["url"], headers={"Authorization": "Bearer s3cr3t"}).status_code == 410)
        # Телефон по адресу в сети: ядро по умолчанию слушает только этот
        # компьютер (TrustedHost отвечает 400) — тогда канал проверен выше
        seen.clear()
        lan = TestClient(server_mod.app, base_url="http://192.168.1.5:8000")
        r = lan.post("/api/chat/stream", json=body, headers={"Authorization": "Bearer s3cr3t"})
        if r.status_code == 400:
            print("  (адрес сети не пропущен TrustedHost — канал по адресу проверен выше)")
        else:
            check("телефон по адресу в сети — «прислать»", seen.get("delivery") == "send")
    finally:
        server_mod._get_bot, server_mod._api_token, server_mod._touch_persona = orig
        file_links.clear()


def main():
    test_parse()
    test_paths()
    test_search()
    test_hints()
    test_turn()
    test_bot()
    test_api()
    print(f"\nИтого: {ok} проверок, провалов: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
