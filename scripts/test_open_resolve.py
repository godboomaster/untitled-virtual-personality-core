"""Тест резолва «открой X» по инциденту 05.10 («открой личную страницу
кутузовой нгту» открыло выдуманный адрес, а вопрос показал третий).

Проверяет:
* вопрос «Открыть …?» и «открыл …» показывают настоящий путь адреса;
  длинный путь сокращён в середине с «…» — показанное никогда не выглядит
  рабочим адресом, которого нет; %-кодированный путь читаемый;
* find_site_url сверяет выдачу без служебных слов («личную страницу»,
  «сайт», «official page») — на реальной выдаче Google по Кутузовой
  находится её страница;
* LLM-ярус: распознанное «открой X», для которого сайт не нашёлся, —
  честный отказ, а не (None, None) (фраза уходила в обычный разговор, и
  модель вписывала маркер с выдуманным адресом);
* вся цепочка инцидента: «открой личную страницу кутузовой нгту» →
  страница ciu.nstu.ru/kaf/persons/98849 и вопрос с ней.

Сеть, история браузера и проверка интернета подменены.
Запуск: PYTHONPATH=. python3 scripts/test_open_resolve.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.features import browser_history, cc_texts  # noqa: E402
from app.features import web_search as ws  # noqa: E402
from app.features.computer_control import ComputerControlManager  # noqa: E402

FAILS = 0


def check(name, cond, detail=""):
    global FAILS
    if cond:
        print(f"  [OK] {name}")
    else:
        FAILS += 1
        print(f"  [FAIL] {name} {detail}")


# Реальная выдача Google по запросу о Кутузовой (05.10)
NSTU_RESULTS = [
    {"title": "НГТУ - КУТУЗОВА И. А. - Общая информация", "href": "https://ciu.nstu.ru/kaf/persons/98849/"},
    {"title": "НГТУ - ТПИ - Преподаватели и сотрудники", "href": "https://ciu.nstu.ru/kaf/psibd/about/persons"},
    {"title": "НГТУ - КУТУЗОВА И. А. - Список научных публикаций",
     "href": "https://ciu.nstu.ru/kaf/persons/98849/nauchnaya_deyatelnost/publicationlist_new"},
    {"title": "НГТУ - КУТУЗОВА И. А. - Расписание занятий",
     "href": "https://ciu.nstu.ru/kaf/persons/98849/edu_actions/timetables/lessons"},
    {"title": "НГТУ. Сотрудники", "href": "https://www.nstu.ru/phone/person?faculty=35&page=4"},
]
search_calls = []
ws.internet_available = lambda: True
ws._google_translate = lambda s: None  # перевод с IP пользователя получает /sorry 429
browser_history.find_in_history = lambda name: None  # историю Chrome человека не трогаем


def fake_search(results):
    def _search(name, n, engine="google"):
        search_calls.append(name)
        return results, engine
    return _search


CCM = ComputerControlManager
url = lambda v: {"kind": "url", "value": v}  # noqa: E731

print("Показ адреса в вопросе")
INCIDENT = "https://ciu.nstu.ru/kaf/pii/a/ic/view/kutuzova_irina_aleksandrovna"
check("вопрос показывает путь целиком (адрес инцидента)",
      CCM.confirm_question(url(INCIDENT)) == "Открыть ciu.nstu.ru/kaf/pii/a/ic/view/kutuzova_irina_aleksandrovna?",
      CCM.confirm_question(url(INCIDENT)))
check("по-английски — тоже", "ciu.nstu.ru/kaf/pii/a/ic/view/kutuzova_irina_aleksandrovna"
      in CCM.confirm_question(url(INCIDENT), lang="en"))
check("«открыл …» — тоже", CCM.describe_done(url(INCIDENT)).endswith(
      "ciu.nstu.ru/kaf/pii/a/ic/view/kutuzova_irina_aleksandrovna"), CCM.describe_done(url(INCIDENT)))
check("короткий путь — как раньше", CCM.confirm_question(url("https://www.google.com/maps")) == "Открыть google.com/maps?")
check("корень — один хост", CCM.confirm_question(url("https://nstu.ru/")) == "Открыть nstu.ru?")
check("%-кодированный путь читаемый",
      CCM._host(url("https://ru.wikipedia.org/wiki/%D0%9D%D0%93%D0%A2%D0%A3")) == "ru.wikipedia.org/wiki/НГТУ")
LONG = ("https://portal.example.edu/faculty/informatics/departments/applied/staff/"
        "teachers/2024/autumn/profiles/view/kutuzova_irina_aleksandrovna")
shown = CCM._host(url(LONG))
check("длинный путь сокращён в середине с «…»",
      shown == "portal.example.edu/faculty/…/kutuzova_irina_aleksandrovna", shown)
check("query в адресе не показан как путь, но вопрос предупреждает о параметрах",
      "?" not in CCM._host(url("https://x.ru/c?d=1")) and "d=1" in CCM.confirm_question(url("https://x.ru/c?d=1")))
samples = [INCIDENT, LONG, "https://a.ru/x/y", "https://b.com/one/two/three/four/five",
           "https://c.org/" + "/".join(f"s{i}" for i in range(30)) + "/end"]
bad = []
for u in samples:
    s = CCM._host(url(u))
    from urllib.parse import urlparse
    full = urlparse(u).hostname.removeprefix("www.") + urlparse(u).path.rstrip("/")
    if s != full and "…" not in s:
        bad.append((u, s))
check("показанное — либо адрес целиком, либо явно сокращено «…»", not bad, bad)

print("Сверка выдачи с запросом")
ws.search_links = fake_search(NSTU_RESULTS)
for q in ("личную страницу кутузовой нгту", "личная страница Кутузовой НГТУ",
          "страницу кутузовой нгту", "официальную страницу Кутузовой НГТУ", "кутузова нгту"):
    got = ws.find_site_url(q)
    check(f"«{q}» → страница Кутузовой", got == "https://ciu.nstu.ru/kaf/persons/98849/", got)
check("служебные слова отброшены", ws._significant_words("личную страницу кутузовой нгту") == ["кутузовой", "нгту"])
check("одни служебные — берутся все", ws._significant_words("сайт") == ["сайт"])
check("англ. «official page» — тоже служебное",
      ws._significant_words("official page of NSTU") == ["nstu"])
ws.search_links = fake_search([{"title": "Кутузов — фельдмаршал", "href": "https://example.org/kutuzov"}])
check("выдача не про запрос — по-прежнему отказ (не первый попавшийся)",
      ws.find_site_url("личную страницу кутузовой нгту") is None)


class IntentRouter:
    def __init__(self, resp):
        self.resp = resp

    def get_response(self, messages, **kw):
        return self.resp


tmp = Path(tempfile.mkdtemp(prefix="cc_open_"))
mgr = ComputerControlManager(context="test", config={"confirm": True}, base_dir=tmp)

print("LLM-ярус: «открой X»")
ws.search_links = fake_search([])
act, err = mgr.resolve_intent_llm(
    "открой личную страницу кутузовой нгту",
    IntentRouter('{"action":"open","target":"личная страница Кутузовой НГТУ"}'))
check("сайт не нашёлся → честный отказ, а не (None, None)", act is None and bool(err), (act, err))
check("отказ называет цель и не придумывает ссылку",
      err and "личная страница Кутузовой НГТУ" in err and "http" not in err, err)
check("отказ по-английски есть",
      "precisely" in cc_texts.t("open_not_found", "en", target="X"))
check("«none» — по-прежнему не команда",
      mgr.resolve_intent_llm("как дела?", IntentRouter('{"action":"none"}')) == (None, None))

print("Цепочка инцидента")
ws.search_links = fake_search(NSTU_RESULTS)
search_calls.clear()
act, err = mgr.resolve_intent_llm(
    "открой личную страницу кутузовой нгту",
    IntentRouter('{"action":"open","target":"личная страница Кутузовой НГТУ"}'))
check("найдена её страница", act and act.get("value") == "https://ciu.nstu.ru/kaf/persons/98849/", (act, err))
check("адрес от поиска — с подтверждением", act and act.get("via_search") is True)
q = CCM.confirm_question(act) if act else ""
check("вопрос показывает эту страницу", "ciu.nstu.ru/kaf/persons/98849" in q, q)
check("поиск шёл по цели модели", search_calls and search_calls[-1] == "личная страница Кутузовой НГТУ", search_calls)

print("Адрес из маркера модели")
clean, _n = mgr.process_markers(
    "Открываю страницу. [OPEN_URL:https://ciu.nstu.ru/kaf/pii/a/ic/view/kutuzova_irina_aleksandrovna]",
    "mk", user_id="u", user_text="открой личную страницу кутузовой нгту", lang="ru")
check("вопрос показывает настоящий путь маркера",
      "ciu.nstu.ru/kaf/pii/a/ic/view/kutuzova_irina_aleksandrovna" in clean, clean)
check("и предупреждает, что адрес не проверен", "предложила модель" in clean, clean)
clean2, _n = mgr.process_markers("Открываю. [OPEN_URL:youtube.com]", "mk", user_id="u",
                                user_text="открой ютуб", lang="ru")
check("корень сайта из маркера — без предупреждения", "предложила модель" not in clean2, clean2)

print(f"\n{'ALL OK' if FAILS == 0 else f'{FAILS} FAIL'}")
sys.exit(1 if FAILS else 0)
