"""Тест генерации скина нейросетью (app/api/skin_gen_api + POST /api/skins/generate).

  - извлечение HTML из ответа модели: markdown-ограды, текст вокруг,
    обрезанный ответ (нет </html>), ответ без документа;
  - inline-ассеты: data-URI → vpc-asset:N и обратно;
  - валидация запроса (экран, локаль, размеры) → 422/413, авторизация → 401;
  - SSE: прогресс, финал с HTML, провайдер/модель; итерация исправления
    (ошибки + прошлый файл в промпте); второй заход с меньшим лимитом вывода;
    «никто не ответил», исключение провайдера (без утечки текста), занятый
    слот, отмена клиентом (стрим провайдера обрывается, слот свободен);
  - продолжение оборванного ответа (малый потолок вывода): документ из трёх
    кусков склеивается, повтор хвоста на стыке срезается, события
    {"status": "continue", "round": k}; не закрыт и после MAX_CONTINUATIONS —
    truncated;
  - арт-направление: разбор ответа (ограды, висячие запятые, битый внешний
    объект), нормализация (hex, роли, шрифтовые стеки), shell-переменные
    (одинаковы на трёх экранах), POST /api/skins/direction (повтор с просьбой
    о JSON, 502/422/413); направление и функциональный CSS в промпте;
  - генерация без направления: стадия направления на сервере (события
    direction_start / direction / direction_failed, два вызова модели,
    неудача стадии не ломает генерацию, исправление стадию не запускает,
    отмена во время стадии);
  - strip_base на настоящих шаблонах и пресете: data-vpc*, разметка, скрипты
    и блок VPC-BRIDGE целы, CSS шаблона и длинные комментарии вырезаны.

Настоящие провайдеры не вызываются: ModelRouter подменяется заглушкой.

Запуск: /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 -m scripts.test_skin_gen_api
"""

import asyncio
import json
import sys
import threading
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

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


DOC = '<!DOCTYPE html><html><head></head><body><div data-vpc-screen="chat">x</div></body></html>'
ASSET = "data:image/png;base64," + "A" * 400


class FakeRouter:
    """Заглушка ModelRouter: отдаёт chunks через on_token, пишет вызовы."""

    def __init__(self, chunks=None, answers=None, raise_exc=None, delay=0.0):
        self.chunks = chunks
        # answers — последовательность ответов по вызовам (None — «никто не ответил»)
        self.answers = list(answers) if answers is not None else None
        self.raise_exc = raise_exc
        self.delay = delay
        self.calls = []
        self.available = {"openai": {"model": "gpt-x"}}
        self.webchat_sites = ["qwen"]
        self.model_overrides = {}
        self._last_provider = None
        self.stopped_early = False

    def model_for(self, pid):
        return self.model_overrides.get(pid) or (self.available.get(pid) or {}).get("model", "")

    def get_response_stream(self, messages, on_token, **kw):
        self.calls.append({"messages": messages, **kw})
        if self.raise_exc:
            raise self.raise_exc
        if self.answers is not None:
            # ответы кончились — «никто не ответил»
            ans = self.answers.pop(0) if self.answers else None
            if ans is None:
                return None
            chunks = [ans]
        else:
            chunks = self.chunks
        parts = []
        for c in chunks:
            if self.delay:
                time.sleep(self.delay)
            parts.append(c)
            try:
                on_token(c)
            except Exception:
                # как ModelRouter._stream_with_keys_locked: обрыв — накопленное
                self.stopped_early = True
                return "".join(parts)
        self._last_provider = "openai"
        return "".join(parts)


def sse_events(text: str) -> list[dict]:
    out = []
    for frame in text.split("\n\n"):
        line = next((l for l in frame.split("\n") if l.startswith("data: ")), None)
        if line:
            out.append(json.loads(line[6:]))
    return out


DIRECTION = {
    "name": "Журнал возвратов",
    "subject": "Журнал возвратов ночного библиотекаря в закрытом читальном зале",
    "mood": "Тишина после закрытия.",
    "palette": [
        {"role": "background", "hex": "#1c1a16", "reason": "тёмное дерево стола"},
        {"role": "surface", "hex": "#26231d", "reason": "обложка журнала"},
        {"role": "ink", "hex": "#e9e1cf", "reason": "бумага карточек"},
        {"role": "muted", "hex": "#a39a86", "reason": "карандашные пометки"},
        {"role": "accent", "hex": "#c2452d", "reason": "штемпель «возвращено»"},
    ],
    "fonts": {"display": 'Didot, "Bodoni 72", serif', "text": '"Iowan Old Style", Palatino, Georgia, serif',
              "mono": "", "why": "штампы и формуляры"},
    "layout": "Лента — строки журнала с датами на полях.",
    "signature": "Красный штемпель-дата на каждом ответе персоны.",
    "texture": "Линовка repeating-linear-gradient.",
    "motion": "Новый ответ пропечатывается штемпелем.",
    "avoid": ["свечи-эмодзи", "пергамент clip-path", "золотые рамки", "готический шрифт"],
}


def directions_json(n=3):
    items = []
    for i in range(n):
        d = json.loads(json.dumps(DIRECTION))
        d["name"] = f"{DIRECTION['name']} {i + 1}"
        items.append(d)
    return json.dumps({"directions": items}, ensure_ascii=False)


def body(**kw):
    # По умолчанию — с выбранным направлением: без него сервер сам запускает
    # стадию направления (отдельный вызов модели, см. run_auto_direction)
    b = {"screen": "chat", "description": "тёмная библиотека, свечи", "base_html": DOC,
         "contract_doc": "# Контракт скина", "locale": "ru", "direction": DIRECTION}
    b.update(kw)
    return b


def dbody(**kw):
    b = {"description": "тёмная библиотека, свечи", "locale": "ru"}
    b.update(kw)
    return b


def run_direction(sg, client):
    section("арт-направление: разбор")
    dirs = sg.parse_directions(directions_json())
    check("чистый JSON — 3 направления", len(dirs) == 3 and dirs[0]["name"] == "Журнал возвратов 1")
    check("hex нормализован, роли на месте",
          [c["role"] for c in dirs[0]["palette"]] == ["background", "surface", "ink", "muted", "accent"])
    fenced = "Вот направления:\n```json\n" + directions_json(2) + "\n```\nНадеюсь, подойдут!"
    check("ограды и текст вокруг", len(sg.parse_directions(fenced)) == 2)
    trailing = directions_json(1)[:-2] + ",]}"
    check("висячая запятая прощается", len(sg.parse_directions(trailing)) == 1)
    one = json.dumps(DIRECTION, ensure_ascii=False)
    check("одно направление без обёртки", len(sg.parse_directions("Ответ: " + one)) == 1)
    garbled = '{"directions": [' + one + ", " + one + ', {"name": обрыв'
    check("битый внешний объект — целые направления собраны", len(sg.parse_directions(garbled)) == 2)
    check("мусор — []", sg.parse_directions("Извините, не могу.") == [] and sg.parse_directions(None) == [])
    check("лимит числа направлений", len(sg.parse_directions(directions_json(3), 1)) == 1)

    bad = json.loads(one)
    bad["palette"] = [{"role": "bg", "hex": "#abc", "reason": ""}, {"role": "text", "hex": "rgb(1,2,3)"},
                      {"role": "ink", "hex": "#12345"}, {"role": "accent", "hex": "c2452d"}]
    check("нет валидного ink — направление отвергнуто", sg.normalize_direction(bad) is None)
    bad["palette"].append({"role": "Foreground", "hex": "#EEE"})
    d = sg.normalize_direction(bad)
    check("#abc → #aabbcc, без # — принят, синонимы ролей",
          d is not None and [(c["role"], c["hex"]) for c in d["palette"]]
          == [("background", "#aabbcc"), ("accent", "#c2452d"), ("ink", "#eeeeee")])
    evil = json.loads(one)
    evil["fonts"] = {"display": "url(https://x/f.woff)", "text": "Georgia; } body { x", "mono": ""}
    check("шрифты с url/CSS-инъекцией — отвергнуты", sg.normalize_direction(evil) is None)
    evil["fonts"]["text"] = "Georgia, serif"
    ev = sg.normalize_direction(evil)
    check("…годный стек остаётся, display = text", ev and ev["fonts"]["display"] == "Georgia, serif")
    long = json.loads(one)
    long["name"] = "Я" * 500
    long["avoid"] = "раз; два\nтри"
    lg = sg.normalize_direction(long)
    check("тексты обрезаются, avoid строкой — список", len(lg["name"]) <= 60 and lg["avoid"] == ["раз", "два", "три"])
    check("без subject — отвергнуто", sg.normalize_direction({**json.loads(one), "subject": ""}) is None)

    sv = dict(sg.shell_vars(dirs[0]))
    check("shell-переменные из палитры и шрифтов",
          sv["--vpc-shell-bg"] == "#1c1a16" and sv["--vpc-shell-accent"] == "#c2452d"
          and sv["--vpc-shell-font-disp"].startswith("Didot") and sv["--vpc-shell-font-mono"])

    section("арт-направление: эндпоинт")
    fake = FakeRouter(answers=[directions_json()])
    with mock.patch.object(sg, "_make_router", lambda: fake):
        r = client.post("/api/skins/direction", json=dbody())
    data = r.json() if r.status_code == 200 else {}
    check("200 и три направления", r.status_code == 200 and len(data.get("directions", [])) == 3)
    check("провайдер и модель", data.get("provider") == "openai" and data.get("model") == "gpt-x")
    c0 = fake.calls[0]
    check("температура и лимит направления",
          c0["temperature"] == sg.DIRECTION_TEMPERATURE and c0["max_tokens"] == sg.DIRECTION_MAX_TOKENS)
    check("в промпте бриф и язык", "тёмная библиотека" in c0["messages"][-1]["content"]
          and "Russian" in c0["messages"][-1]["content"])

    fake = FakeRouter(answers=["Вот мои идеи: библиотека, свечи, пергамент.", "```json\n" + directions_json(2) + "\n```"])
    with mock.patch.object(sg, "_make_router", lambda: fake):
        r = client.post("/api/skins/direction", json=dbody())
    check("не JSON — повтор с причиной и просьбой о JSON, затем успех",
          r.status_code == 200 and len(r.json()["directions"]) == 2 and len(fake.calls) == 2
          and "ONLY the corrected JSON" in fake.calls[1]["messages"][-1]["content"]
          and "no JSON object" in fake.calls[1]["messages"][-1]["content"]
          and fake.calls[1]["messages"][-2]["role"] == "assistant"
          and fake.calls[1]["force_provider"] == "openai")

    # Веб-чаты (deepseek): имена шрифтов в двойных кавычках внутри JSON-строк
    pal = ('[{"role": "background", "hex": "#1b1a17"}, {"role": "ink", "hex": "#e8e2d0"}, '
           '{"role": "accent", "hex": "#b33a2b"}]')
    broken_multi = ('```json\n{\n  "directions": [\n    {\n      "name": "Журнал",\n      "subject": "Каталог",\n'
                    f'      "palette": {pal},\n      "fonts": {{\n'
                    '        "display": ""Iowan Old Style", "Palatino Linotype", serif",\n'
                    '        "text": "Georgia, "Times New Roman", serif"\n      },\n'
                    '      "signature": "Штемпель "ВОЗВРАТ", красный",\n      "avoid": ["свечи",]\n    }\n  ]\n}\n```')
    broken_one = ('{"directions":[{"name":"Маяк","subject":"Журнал","palette":' + pal
                  + ',"fonts":{"display":""Rockwell", "American Typewriter", serif","text":"Optima, sans-serif"},'
                  '"avoid":["неон", "стекло"]}]}')
    d1, d2 = sg.parse_directions(broken_multi), sg.parse_directions(broken_one)
    check("неэкранированные кавычки в шрифтах (многострочный JSON) — разобрано",
          len(d1) == 1 and d1[0]["fonts"]["display"].startswith("'Iowan Old Style'")
          and d1[0]["signature"] == 'Штемпель "ВОЗВРАТ", красный' and d1[0]["avoid"] == ["свечи"])
    check("неэкранированные кавычки в шрифтах (однострочный JSON) — разобрано",
          len(d2) == 1 and "American Typewriter" in d2[0]["fonts"]["display"]
          and d2[0]["avoid"] == ["неон", "стекло"])
    smart = ('{“directions”: [{“name”: “Автомат”, “subject”: “Терминал”, “palette”: ' + pal
             + ', “fonts”: {“text”: “Menlo, monospace”}}]}')
    check("типографские кавычки-разделители — разобрано", len(sg.parse_directions(smart)) == 1)
    dictpal = ('{"directions":[{"name":"Сад","subject":"Оранжерея","palette":{"background":"#0f1a14 — ночь",'
               '"ink":{"hex":"#dfe8d8"},"accent":"#c9a227"},"fonts":"Optima, Candara, sans-serif"}]}')
    d3 = sg.parse_directions(dictpal)
    check("палитра словарём, hex с пояснением, шрифты строкой — разобрано",
          len(d3) == 1 and sg._color(d3[0], "background") == "#0f1a14"
          and d3[0]["fonts"]["text"] == "Optima, Candara, sans-serif")
    check("причина отказа называет недостающие цвета",
          "accent" in sg.explain_directions_failure(
              '{"directions":[{"name":"X","subject":"Y","palette":[{"role":"background","hex":"#000"},'
              '{"role":"ink","hex":"#fff"}],"fonts":{"text":"Georgia, serif"}}]}'))
    check("промпт направлений требует одинарные кавычки в шрифтах",
          "SINGLE quotes" in sg.DIRECTION_SYSTEM_PROMPT and "'Iowan Old Style'" in sg.DIRECTION_SYSTEM_PROMPT)

    fake = FakeRouter(answers=["нет", "всё ещё нет"])
    with mock.patch.object(sg, "_make_router", lambda: fake):
        r = client.post("/api/skins/direction", json=dbody())
    check("дважды не JSON — 502 с понятным текстом", r.status_code == 502 and "формат" in r.json()["detail"])

    fake = FakeRouter(answers=[None])
    with mock.patch.object(sg, "_make_router", lambda: fake):
        r = client.post("/api/skins/direction", json=dbody())
    check("никто не ответил — 502, без повтора", r.status_code == 502 and len(fake.calls) == 1)

    fake = FakeRouter(raise_exc=RuntimeError("401 key=sk-SECRET123"))
    with mock.patch.object(sg, "_make_router", lambda: fake):
        r = client.post("/api/skins/direction", json=dbody())
    check("исключение — обезличенная ошибка", r.status_code == 502 and "SECRET" not in r.text)

    fake = FakeRouter(answers=[directions_json(1)])
    with mock.patch.object(sg, "_make_router", lambda: fake):
        r = client.post("/api/skins/direction", json=dbody(count=1, exclude=["Старая идея"]))
    user = fake.calls[0]["messages"][-1]["content"]
    check("count и exclude в промпте", "exactly 1 direction" in user and "Старая идея" in user)
    for bad, code in (({"count": 5}, 422), ({"locale": "de"}, 422), ({"model": "bad model!"}, 422),
                      ({"exclude": ["x"] * 20}, 422)):
        r = client.post("/api/skins/direction", json=dbody(**bad))
        check(f"отказ {list(bad)[0]} → {code}", r.status_code == code)
    r = client.post("/api/skins/direction", content=b"{" + b" " * (sg.DIRECTION_MAX_BODY_BYTES + 10) + b"}",
                    headers={"Content-Type": "application/json"})
    check("тело больше лимита — 413", r.status_code == 413)


def main():
    run()
    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


def run():
    from app.api import skin_gen_api as sg

    section("extract_html")
    html, tr = sg.extract_html("Вот скин:\n```html\n" + DOC + "\n```\nГотово.")
    check("ограды и текст вокруг срезаны", html == DOC and tr is False)
    html, tr = sg.extract_html("```html\n<!DOCTYPE html><html><body>обрыв")
    check("нет </html> — truncated", tr is True and html.startswith("<!DOCTYPE html>"))
    html, tr = sg.extract_html("<html lang=ru><body></body></html>")
    check("документ без DOCTYPE тоже годится", html == "<html lang=ru><body></body></html>" and not tr)
    check("нет документа — None", sg.extract_html("Извините, не могу.") == (None, False))
    check("пустой ответ — None", sg.extract_html("") == (None, False))

    section("inline-ассеты")
    assets: list[str] = []
    src = f'<img src="{ASSET}"><div style="background:url({ASSET})"></div><i src="data:image/png;base64,AAA">'
    stripped = sg.strip_assets(src, assets)
    check("одинаковые ассеты — одна метка", stripped.count("vpc-asset:0") == 2 and len(assets) == 1)
    check("короткие data-URI не трогаются", "data:image/png;base64,AAA" in stripped)
    check("восстановление", sg.restore_assets(stripped, assets) == src)
    check("чужая метка остаётся как есть", sg.restore_assets("vpc-asset:7", assets) == "vpc-asset:7")

    section("prepare / промпт")
    req = sg.SkinGenRequest(**body(errors=["Не найдена точка X"], previous_html=f"<html>{ASSET}</html>"))
    msgs, assets = sg.prepare(req)
    user = msgs[-1]["content"]
    check("фикс: ошибки в промпте", "Не найдена точка X" in user and "# Problems" in user)
    check("фикс: правится прошлый файл, база не дублируется", "Previous file" in user and "# Base file" not in user)
    check("фикс: ассет прошлого файла вырезан", ASSET not in user and "vpc-asset:0" in user)
    msgs, _ = sg.prepare(sg.SkinGenRequest(**body()))
    user = msgs[-1]["content"]
    check("генерация: бриф, контракт, база", "тёмная библиотека" in user and "# Контракт скина" in user and DOC in user)
    check("язык подписей по локали", "Russian" in user)
    run_prompt_direction(sg)
    for bad, code in (({"screen": "lobby"}, 422), ({"locale": "de"}, 422), ({"base_kind": "x"}, 422),
                      ({"model": "bad model!"}, 422), ({"errors": ["e" * 3000]}, 422),
                      ({"direction": {"name": "Без палитры"}}, 422),
                      ({"base_html": "<html>" + "x" * 250_000 + "</html>"}, 413)):
        try:
            sg.prepare(sg.SkinGenRequest(**body(**bad)))
            check(f"отказ {bad and list(bad)[0]}", False)
        except sg.SkinGenError as e:
            check(f"отказ {list(bad)[0]} → {code}", e.status == code)

    section("эндпоинт")
    from fastapi.testclient import TestClient
    import app.api.server as server_mod

    orig_token = server_mod._api_token
    try:
        server_mod._api_token = "secret"
        client = TestClient(server_mod.app, base_url="http://127.0.0.1")
        r = client.post("/api/skins/generate", json=body())
        check("без токена — 401", r.status_code == 401)
        server_mod._api_token = ""

        r = client.post("/api/skins/generate", json=body(screen="lobby"))
        check("неизвестный экран — 422", r.status_code == 422)
        r = client.post("/api/skins/generate", json=body(description="x" * 5000))
        check("длинный бриф — 422 (pydantic)", r.status_code == 422)

        sg.PROGRESS_INTERVAL_SEC = 0.05
        fake = FakeRouter(chunks=["Конечно!\n```html\n", DOC.replace("x", "vpc-asset:0")[:40],
                                  DOC.replace("x", "vpc-asset:0")[40:], "\n```"], delay=0.03)
        base = DOC.replace("x", f'<img src="{ASSET}">')
        with mock.patch.object(sg, "_make_router", lambda: fake):
            r = client.post("/api/skins/generate", json=body(base_html=base))
        ev = sse_events(r.text)
        done = ev[-1] if ev else {}
        check("SSE text/event-stream", r.headers.get("content-type", "").startswith("text/event-stream"))
        check("первое событие — started", ev and ev[0].get("status") == "started")
        check("есть события прогресса", any("progress" in e for e in ev))
        check("финал done", done.get("done") is True and done.get("truncated") is False)
        check("HTML без оград, ассет восстановлен", done.get("html") == DOC.replace("x", ASSET))
        check("провайдер и модель", done.get("provider") == "openai" and done.get("model") == "gpt-x")
        check("лимит вывода — MAX_TOKENS", fake.calls[0]["max_tokens"] == sg.MAX_TOKENS)
        check("разовый канал веб-чата", fake.calls[0]["user_path"] is True)
        check("слот освобождён", not sg._busy.locked())

        fake = FakeRouter(answers=[None, DOC])
        with mock.patch.object(sg, "_make_router", lambda: fake):
            ev = sse_events(client.post("/api/skins/generate", json=body()).text)
        check("второй заход с меньшим лимитом", [c["max_tokens"] for c in fake.calls] == [sg.MAX_TOKENS, sg.FALLBACK_MAX_TOKENS])
        check("статус retry_small_limit", any(e.get("status") == "retry_small_limit" for e in ev))
        check("…и результат", ev[-1].get("html") == DOC)

        fake = FakeRouter(answers=[None, None])
        with mock.patch.object(sg, "_make_router", lambda: fake):
            ev = sse_events(client.post("/api/skins/generate", json=body()).text)
        check("никто не ответил — error", "error" in ev[-1] and not ev[-1].get("done"))

        fake = FakeRouter(answers=["Не могу сделать скин."])
        with mock.patch.object(sg, "_make_router", lambda: fake):
            ev = sse_events(client.post("/api/skins/generate", json=body()).text)
        check("ответ без документа — code no_html", ev[-1].get("code") == "no_html" and "raw" in ev[-1])

        fake = FakeRouter(answers=["<!DOCTYPE html><html><body>обрыв на полуслове"])
        with mock.patch.object(sg, "_make_router", lambda: fake):
            ev = sse_events(client.post("/api/skins/generate", json=body()).text)
        check("обрезанный ответ, продолжение не пришло — done + truncated",
              ev[-1].get("done") and ev[-1].get("truncated") is True
              and ev[-1].get("html") == "<!DOCTYPE html><html><body>обрыв на полуслове")
        check("…продолжение запрашивалось", any(e.get("status") == "continue" for e in ev))

        run_continuation(client, sg)
        run_direction(sg, client)
        run_auto_direction(sg, client)

        fake = FakeRouter(raise_exc=RuntimeError("401 key=sk-SECRET123"))
        with mock.patch.object(sg, "_make_router", lambda: fake):
            ev = sse_events(client.post("/api/skins/generate", json=body()).text)
        check("исключение — обезличенная ошибка", "error" in ev[-1] and "SECRET" not in json.dumps(ev))
        check("слот освобождён после исключения", not sg._busy.locked())

        fake = FakeRouter(answers=[DOC])
        with mock.patch.object(sg, "_make_router", lambda: fake):
            ev = sse_events(client.post("/api/skins/generate", json=body(provider="groq")).text)
        check("недоступный провайдер — ошибка", "groq" in ev[-1].get("error", ""))
        with mock.patch.object(sg, "_make_router", lambda: fake):
            ev = sse_events(client.post("/api/skins/generate", json=body(provider="openai", model="gpt-big")).text)
        check("выбранный провайдер — force_provider", fake.calls[-1]["force_provider"] == "openai")
        check("выбранная модель — override", fake.model_overrides.get("openai") == "gpt-big" and ev[-1].get("model") == "gpt-big")

        def no_providers():
            raise RuntimeError("Нет настроенных провайдеров")
        with mock.patch.object(sg, "_make_router", no_providers):
            ev = sse_events(client.post("/api/skins/generate", json=body()).text)
        check("нет провайдеров — понятная ошибка", "провайдер" in ev[-1].get("error", "").lower())

        sg.BUSY_WAIT_SEC = 0.1
        sg._busy.acquire()
        try:
            ev = sse_events(client.post("/api/skins/generate", json=body()).text)
        finally:
            sg._busy.release()
        check("занятый слот — code busy", ev[-1].get("code") == "busy")
    finally:
        server_mod._api_token = orig_token

    run_stitch(sg)
    run_strip_base(sg)

    section("отмена клиентом")

    async def cancel_flow():
        fake = FakeRouter(chunks=["<!DOCTYPE html>"] + ["<p>x</p>"] * 200, delay=0.02)
        req = sg.SkinGenRequest(**body())
        msgs, assets = sg.prepare(req)
        with mock.patch.object(sg, "_make_router", lambda: fake):
            gen = sg.generate_events(req, msgs, assets)
            first = await gen.__anext__()
            await gen.__anext__()  # прогресс
            await gen.aclose()  # клиент ушёл
            for _ in range(100):
                if not sg._busy.locked():
                    break
                await asyncio.sleep(0.02)
        return first, fake

    first, fake = asyncio.run(cancel_flow())
    check("стрим стартовал", "started" in first)
    check("стрим провайдера оборван", fake.stopped_early)
    check("слот освобождён после отмены", not sg._busy.locked())


BIG = ("<!DOCTYPE html><html><head><style>body{margin:0}</style></head><body>\n"
       + "".join(f'<div class="row" data-i="{i}">строка {i}</div>\n' for i in range(300))
       + "</body></html>")


def run_stitch(sg):
    section("stitch: склейка кусков")
    a, b = BIG[:4000], BIG[4000:]
    check("простой стык", sg.stitch(a, b) == BIG)
    check("повтор хвоста (300 символов) срезан", sg.stitch(a, a[-300:] + b) == BIG)
    check("повтор после перевода строки в начале куска срезан",
          sg.stitch(a, "\n" + a[-120:] + b) == BIG)
    check("ограды по краям куска отброшены", sg.stitch(a, "```html\n" + b + "\n```") == BIG)
    short = "</div>\n"
    check("короткое совпадение (< OVERLAP_MIN) — не повтор",
          sg.stitch("<div><div>x</div>\n", short + "</body>") == "<div><div>x</div>\n</div>\n</body>")
    check("кусок с <!DOCTYPE — модель начала заново: заменяет накопленное",
          sg.stitch(a, "\n" + BIG) == BIG)
    check("пустой кусок ничего не меняет", sg.stitch(a, "```\n```") == a)
    check("is_complete", sg.is_complete(BIG) and not sg.is_complete(a) and not sg.is_complete("нет"))


def run_continuation(client, sg):
    section("продолжение оборванного ответа")
    p1, p2, p3 = BIG[:3000], BIG[3000:7000], BIG[7000:]
    fake = FakeRouter(answers=["```html\n" + p1, p2, p3 + "\n```"])
    with mock.patch.object(sg, "_make_router", lambda: fake):
        ev = sse_events(client.post("/api/skins/generate", json=body()).text)
    done = ev[-1] if ev else {}
    check("три куска → один документ, не truncated",
          done.get("done") is True and done.get("truncated") is False and done.get("html") == BIG)
    rounds = [e.get("round") for e in ev if e.get("status") == "continue"]
    check("события continue с номером раунда", rounds == [1, 2])
    check("три вызова модели", len(fake.calls) == 3)
    cont = fake.calls[1]["messages"]
    check("продолжение: переписка + написанное + просьба продолжить",
          cont[:2] == fake.calls[0]["messages"] and cont[2] == {"role": "assistant", "content": "```html\n" + p1}
          and cont[3]["role"] == "user" and "Continue" in cont[3]["content"])
    check("второе продолжение видит склеенное", fake.calls[2]["messages"][2]["content"] == "```html\n" + p1 + p2)
    check("продолжение — у того же провайдера, с тем же лимитом",
          all(c["force_provider"] == "openai" and c["max_tokens"] == sg.MAX_TOKENS for c in fake.calls[1:]))
    check("прогресс считает все куски", max((e.get("progress", 0) for e in ev), default=0) <= done.get("chars", 0)
          and done.get("chars") >= len(BIG))

    # Модель повторяет хвост написанного в начале продолжения
    fake = FakeRouter(answers=[p1, p1[-200:] + p2, "\n" + p2[-80:] + p3])
    with mock.patch.object(sg, "_make_router", lambda: fake):
        ev = sse_events(client.post("/api/skins/generate", json=body()).text)
    check("повторы на стыках срезаны — документ цел",
          ev[-1].get("done") and ev[-1].get("truncated") is False and ev[-1].get("html") == BIG)

    # Меньший лимит, выбранный на первом заходе, — и для продолжений
    fake = FakeRouter(answers=[None, p1, p2 + p3])
    with mock.patch.object(sg, "_make_router", lambda: fake):
        ev = sse_events(client.post("/api/skins/generate", json=body()).text)
    check("после retry_small_limit продолжение с FALLBACK_MAX_TOKENS",
          [c["max_tokens"] for c in fake.calls] == [sg.MAX_TOKENS] + [sg.FALLBACK_MAX_TOKENS] * 2
          and ev[-1].get("html") == BIG)

    # Так и не закрыт после всех продолжений
    fake = FakeRouter(answers=[p1] + [f'<p data-more="{i}">ещё</p>\n' for i in range(10)])
    with mock.patch.object(sg, "_make_router", lambda: fake):
        ev = sse_events(client.post("/api/skins/generate", json=body()).text)
    rounds = [e.get("round") for e in ev if e.get("status") == "continue"]
    check(f"не больше MAX_CONTINUATIONS ({sg.MAX_CONTINUATIONS}) продолжений, затем truncated",
          rounds == list(range(1, sg.MAX_CONTINUATIONS + 1))
          and len(fake.calls) == 1 + sg.MAX_CONTINUATIONS
          and ev[-1].get("done") and ev[-1].get("truncated") is True
          and 'data-more="3"' in ev[-1].get("html", ""))
    check("слот освобождён", not sg._busy.locked())


def run_prompt_direction(sg):
    section("промпт: арт-направление и функциональный CSS")
    d = sg.normalize_direction(DIRECTION)
    check("normalize_direction идемпотентна (клиент шлёт назад то, что вернул сервер)",
          sg.normalize_direction(d) == d)
    shells = {}
    for scr in sg.SCREENS:
        # у каждого экрана своя заметка — shell-переменные от неё не зависят
        user = sg.prepare(sg.SkinGenRequest(**body(screen=scr, direction={**d, "note": f"заметка {scr}"})))[0][-1]["content"]
        shells[scr] = [l for l in user.split("\n") if l.strip().startswith("--vpc-shell-")]
    check("shell-переменные буква в букву одинаковы на трёх экранах",
          len(shells["chat"]) == 9 and shells["chat"] == shells["dossier"] == shells["room"])

    gen = sg.prepare(sg.SkinGenRequest(**body()))[0][-1]["content"]
    check("генерация: направление в промпте",
          "# Art direction (binding)" in gen and DIRECTION["subject"] in gen and "#c2452d" in gen
          and "--vpc-shell-accent: #c2452d;" in gen)
    fix = sg.prepare(sg.SkinGenRequest(**body(errors=["Не найдена точка X"], previous_html=DOC)))[0][-1]["content"]
    check("исправление: присланное направление в промпте",
          "# Art direction (binding)" in fix and DIRECTION["name"] in fix and "# Problems" in fix)
    fix0 = sg.prepare(sg.SkinGenRequest(**body(errors=["Не найдена точка X"], previous_html=DOC,
                                               direction=None)))[0][-1]["content"]
    check("исправление без направления — без блока направления и просьбы его вывести",
          "Art direction" not in fix0 and "art direction" not in fix0)
    nodir = sg.prepare(sg.SkinGenRequest(**body(direction=None)))[0][-1]["content"]
    check("генерация без направления — просьба вывести его из брифа",
          "# Art direction\n(none given" in nodir)
    note = sg.prepare(sg.SkinGenRequest(**body(direction={**DIRECTION, "note": "побольше красного"})))[0][-1]["content"]
    check("заметка пользователя к направлению", "побольше красного" in note)

    tpl_head = "# Functional CSS requirements"
    check("функциональный CSS — у базы-шаблона", tpl_head in gen and "[data-vpc-screen]" in gen)
    check("…и в исправлении шаблонного экрана (без «base file comes without its styles»)",
          tpl_head in fix and "comes without its styles" not in fix)
    skin = sg.prepare(sg.SkinGenRequest(**body(base_kind="skin")))[0][-1]["content"]
    skin_fix = sg.prepare(sg.SkinGenRequest(**body(base_kind="skin", errors=["x"], previous_html=DOC)))[0][-1]["content"]
    check("у базы-скина функционального CSS нет", tpl_head not in skin and tpl_head not in skin_fix)
    room = sg.prepare(sg.SkinGenRequest(**body(screen="room")))[0][-1]["content"]
    check("правила экрана — только его", "room-scene" in room and "room-scene" not in gen
          and 'html[data-vpc-active="room"] [data-vpc-screen="room"]' in room)

    section("арт-направление: шрифтовые стеки")
    long_stack = ", ".join(f'"Font Number {i}"' for i in range(20)) + ", serif"
    st = sg._norm_stack(long_stack)
    check("длинный стек режется по запятой, кавычки парные",
          0 < len(st) <= 200 and st.count('"') % 2 == 0 and not st.endswith(",") and "…" not in st)
    check("непарная кавычка — отказ", sg._norm_stack('"Iowan Old Style, serif') == "")
    check("обычный стек как есть", sg._norm_stack(' "Iowan Old Style",  Georgia, serif ') == '"Iowan Old Style", Georgia, serif')


class FlakyRouter(FakeRouter):
    """Первый вызов (стадия направления) падает исключением, дальше — ответы."""

    def get_response_stream(self, messages, on_token, **kw):
        if not self.calls:
            self.calls.append({"messages": messages, **kw})
            raise RuntimeError("500 upstream key=sk-SECRET123")
        return super().get_response_stream(messages, on_token, **kw)


def run_auto_direction(sg, client):
    section("генерация без направления: стадия направления на сервере")
    fake = FakeRouter(answers=[directions_json(1), DOC])
    with mock.patch.object(sg, "_make_router", lambda: fake):
        ev = sse_events(client.post("/api/skins/generate", json=body(direction=None)).text)
    statuses = [e.get("status") for e in ev if "status" in e]
    dir_ev = next((e for e in ev if e.get("status") == "direction"), {})
    check("события direction_start → direction до финала",
          statuses[:3] == ["started", "direction_start", "direction"] and ev[-1].get("done") is True
          and ev[-1].get("html") == DOC)
    check("в событии — нормализованное направление",
          dir_ev.get("direction") == sg.normalize_direction(json.loads(directions_json(1))["directions"][0]))
    check("два вызова модели: направление, затем экран", len(fake.calls) == 2
          and fake.calls[0]["temperature"] == sg.DIRECTION_TEMPERATURE
          and fake.calls[0]["max_tokens"] == sg.DIRECTION_MAX_TOKENS
          and fake.calls[1]["max_tokens"] == sg.MAX_TOKENS)
    check("стадия просит ровно одно направление",
          "exactly 1 direction" in fake.calls[0]["messages"][-1]["content"])
    gen_user = fake.calls[1]["messages"][-1]["content"]
    check("экран генерируется по подобранному направлению",
          "# Art direction (binding)" in gen_user and "Журнал возвратов 1" in gen_user)
    check("слот освобождён", not sg._busy.locked())

    fake = FakeRouter(answers=[directions_json(1), DOC])
    with mock.patch.object(sg, "_make_router", lambda: fake):
        ev = sse_events(client.post("/api/skins/generate",
                                    json=body(direction=None, provider="openai", model="gpt-big")).text)
    check("стадия направления — у выбранного провайдера и модели",
          [c["force_provider"] for c in fake.calls] == ["openai", "openai"]
          and ev[-1].get("model") == "gpt-big")

    fake = FakeRouter(answers=["не JSON", "опять не JSON", DOC])
    with mock.patch.object(sg, "_make_router", lambda: fake):
        ev = sse_events(client.post("/api/skins/generate", json=body(direction=None)).text)
    statuses = [e.get("status") for e in ev if "status" in e]
    check("направление не разобрано → direction_failed, генерация всё равно идёт",
          "direction_failed" in statuses and "direction" not in statuses
          and ev[-1].get("done") is True and ev[-1].get("html") == DOC and len(fake.calls) == 3)
    check("…экран по одному брифу", "(none given" in fake.calls[2]["messages"][-1]["content"])

    fake = FlakyRouter(answers=[DOC])
    with mock.patch.object(sg, "_make_router", lambda: fake):
        ev = sse_events(client.post("/api/skins/generate", json=body(direction=None)).text)
    check("исключение в стадии направления → direction_failed, генерация успешна, текст не утёк",
          any(e.get("status") == "direction_failed" for e in ev) and ev[-1].get("done") is True
          and "SECRET" not in json.dumps(ev, ensure_ascii=False))

    fake = FakeRouter(answers=[None, DOC])
    with mock.patch.object(sg, "_make_router", lambda: fake):
        ev = sse_events(client.post("/api/skins/generate", json=body(direction=None)).text)
    check("никто не ответил на стадии направления → direction_failed, без повтора, генерация успешна",
          any(e.get("status") == "direction_failed" for e in ev) and ev[-1].get("html") == DOC
          and len(fake.calls) == 2)

    fake = FakeRouter(answers=[DOC])
    with mock.patch.object(sg, "_make_router", lambda: fake):
        ev = sse_events(client.post("/api/skins/generate", json=body(provider="groq", direction=None)).text)
    check("недоступный провайдер без направления — та же ошибка генерации",
          "groq" in ev[-1].get("error", "") and not fake.calls)

    fake = FakeRouter(answers=[DOC])
    with mock.patch.object(sg, "_make_router", lambda: fake):
        ev = sse_events(client.post("/api/skins/generate", json=body(
            direction=None, errors=["Не найдена точка X"], previous_html=DOC)).text)
    check("исправление без направления — стадии нет, один вызов",
          len(fake.calls) == 1 and not any(str(e.get("status", "")).startswith("direction") for e in ev)
          and ev[-1].get("done") is True)
    fake = FakeRouter(answers=[DOC])
    with mock.patch.object(sg, "_make_router", lambda: fake):
        ev = sse_events(client.post("/api/skins/generate", json=body(
            errors=["Не найдена точка X"], previous_html=DOC)).text)
    check("исправление с направлением — один вызов, направление в промпте",
          len(fake.calls) == 1 and DIRECTION["name"] in fake.calls[0]["messages"][-1]["content"]
          and not any(str(e.get("status", "")).startswith("direction") for e in ev))

    fake = FakeRouter(answers=[DOC])
    with mock.patch.object(sg, "_make_router", lambda: fake):
        ev = sse_events(client.post("/api/skins/generate", json=body()).text)
    check("с направлением — стадии нет, один вызов",
          len(fake.calls) == 1 and not any(str(e.get("status", "")).startswith("direction") for e in ev))

    r = client.post("/api/skins/generate", json=body(direction={"name": "x", "subject": "y"}))
    check("негодное направление — 422", r.status_code == 422)

    async def cancel_in_direction():
        fake = FakeRouter(chunks=['{"directions": ['] + ["  "] * 300, delay=0.02)
        req = sg.SkinGenRequest(**body(direction=None))
        msgs, assets = sg.prepare(req)
        with mock.patch.object(sg, "_make_router", lambda: fake):
            gen = sg.generate_events(req, msgs, assets)
            await gen.__anext__()  # started
            await gen.__anext__()
            await gen.aclose()  # клиент ушёл во время стадии направления
            for _ in range(100):
                if not sg._busy.locked():
                    break
                await asyncio.sleep(0.02)
        return fake

    fake = asyncio.run(cancel_in_direction())
    check("отмена во время стадии направления: стрим оборван, генерация не начата, слот свободен",
          fake.stopped_early and len(fake.calls) == 1 and not sg._busy.locked())


def _start_tags(html: str) -> list[tuple]:
    from html.parser import HTMLParser

    class P(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.tags, self.scripts, self._in = [], [], None

        def handle_starttag(self, tag, attrs):
            self.tags.append((tag, tuple(attrs)))
            if tag == "script":
                self._in = []

        def handle_data(self, data):
            if self._in is not None:
                self._in.append(data)

        def handle_endtag(self, tag):
            if tag == "script" and self._in is not None:
                self.scripts.append("".join(self._in))
                self._in = None

    p = P()
    p.feed(html)
    p.close()
    return p.tags, p.scripts


def _bridge_block(html: str) -> str:
    i = html.find("<!-- ==== VPC-BRIDGE:START")
    j = html.find("<!-- ==== VPC-BRIDGE:END ==== -->")
    return html[i:j + len("<!-- ==== VPC-BRIDGE:END ==== -->")] if i >= 0 and j > i else ""


def run_strip_base(sg):
    import re
    from collections import Counter

    section("strip_base: база для модели")
    skins = Path(__file__).parent.parent / "web" / "src" / "skins"
    files = [(skins / f"skin-template.{s}.html", "template") for s in sg.SCREENS]
    files.append((skins / "presets" / "sylvan-grove.html", "skin"))
    comment_re = re.compile(r"<!--.*?-->", re.DOTALL)
    for path, kind in files:
        if not path.exists():
            check(f"{path.name}: файл есть", False)
            continue
        src = path.read_text(encoding="utf-8")
        out = sg.strip_base(src, kind)
        tags0, scripts0 = _start_tags(src)
        tags1, scripts1 = _start_tags(out)
        vpc = lambda tags: Counter((n, v) for _, attrs in tags for n, v in attrs if n.startswith("data-vpc"))
        name = f"{path.name} ({kind})"
        check(f"{name}: все data-vpc* атрибуты на месте ({sum(vpc(tags0).values())})",
              vpc(tags0) == vpc(tags1) and sum(vpc(tags0).values()) > 10)
        check(f"{name}: разметка (теги и атрибуты) не изменилась", tags0 == tags1)
        check(f"{name}: все <script> ({len(scripts0)}) байт в байт", scripts0 == scripts1 and len(scripts0) >= 1)
        check(f"{name}: блок VPC-BRIDGE с маркерами цел", _bridge_block(src) and _bridge_block(out) == _bridge_block(src))
        long0 = [c for c in comment_re.findall(src) if len(c) > sg.LONG_COMMENT and not sg._BRIDGE_MARKER_RE.match(c)]
        # комментарии вне скриптов (внутри JS «<!--» не бывает в шаблонах, но на всякий случай вычитаем скрипты)
        bare = out
        for sc in scripts1:
            bare = bare.replace(sc, "")
        long1 = [c for c in comment_re.findall(bare) if len(c) > sg.LONG_COMMENT and not sg._BRIDGE_MARKER_RE.match(c)]
        check(f"{name}: длинные комментарии вырезаны ({len(long0)})", long0 and not long1)
        check(f"{name}: нет строк из одних пробелов на месте комментариев",
              not re.search(r"\n[ \t]+\n", bare))
        check(f"{name}: документ начинается с <!DOCTYPE html>", out.lstrip().lower().startswith("<!doctype html>"))
        styles0 = re.findall(r"<style\b[^>]*>(.*?)</style\s*>", src, re.DOTALL | re.IGNORECASE)
        styles1 = re.findall(r"<style\b[^>]*>(.*?)</style\s*>", out, re.DOTALL | re.IGNORECASE)
        if kind == "template":
            check(f"{name}: CSS шаблона вырезан", styles0 and all(len(c) > 1000 for c in styles0)
                  and styles1 == [sg.STYLES_REMOVED] * len(styles0))
            check(f"{name}: заметно короче", len(out) < len(src) * 0.6)
        else:
            norm = lambda css: re.sub(r"\s+", "", re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL))
            check(f"{name}: CSS скина сохранён (кроме комментариев)",
                  len(styles1) == len(styles0) and [norm(c) for c in styles0] == [norm(c) for c in styles1]
                  and sum(map(len, styles1)) > 0.8 * sum(map(len, styles0)))
            check(f"{name}: длинные CSS-комментарии вырезаны",
                  not any(len(c) > sg.LONG_COMMENT for st in styles1 for c in re.findall(r"/\*.*?\*/", st, re.DOTALL)))

    long_c = "<!-- " + "инструкция " * 40 + "<script src=https://cdn/x.js></script> -->"
    doc = ("<!DOCTYPE html>\n" + long_c + "\n<html><head><style>b{color:red}</style></head><body>\n"
           "  " + long_c + "\n"
           "  <p>текст" + long_c + "\nслово</p>\n"
           "  <!-- REQUIRED: короткая пометка -->\n"
           "  <script>const s = '<!-- " + "x" * 300 + " -->'; /* " + "y" * 300 + " */</script>\n"
           "  <div data-vpc=\"messages\" data-vpc-label=\"feed\"></div>\n"
           "</body></html>")
    out = sg.strip_base(doc, "template")
    check("комментарий с «<script src>» внутри вырезан, настоящий скрипт цел",
          "cdn/x.js" not in out and "const s = '<!-- " + "x" * 300 in out and "y" * 300 in out)
    check("комментарий на своей строке уходит с отступом и переводом строки",
          "<body>\n  <p>" in out and "<!DOCTYPE html>\n<html>" in out)
    check("комментарий после текста оставляет перевод строки (слова не склеиваются)", "<p>текст\nслово</p>" in out)
    check("короткая пометка остаётся", "<!-- REQUIRED: короткая пометка -->" in out)
    check("разметка с data-vpc* цела", '<div data-vpc="messages" data-vpc-label="feed"></div>' in out)
    check("CSS шаблона вырезан, у скина — остаётся",
          "color:red" not in out and "b{color:red}" in sg.strip_base(doc, "skin"))
    css_doc = "<style>\n  /* " + "z" * 300 + " */\n  a{b:c}\n  /* коротко */\n</style>"
    check("у скина длинный CSS-комментарий вырезан вместе со строкой, короткий остаётся",
          sg.strip_base(css_doc, "skin") == "<style>\n  a{b:c}\n  /* коротко */\n</style>")

if __name__ == "__main__":
    sys.exit(main())
