"""Память персоны на телефоне без ноутбука: поиск по словам
(web/src/offline/recall.ts) и бюджет промпта модели телефона
(web/src/offline/prompt.ts).

Без ноутбука Gemma видит хвост переписки, а реплики постарше к теме вопроса
находит поиск по основам слов (BM25). Промпт телефона ограничен окном модели
(4096 токенов): не влезает — короче характер, личная память, факты, история.
Модули гоняются под Node (снятие типов, Node ≥ 23.6; относительные импорты
без расширения — хуком резолва).

Запуск: python3 -m scripts.test_offline_recall
"""

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OFFLINE = ROOT / "web" / "src" / "offline"

results = {"ok": 0, "fail": 0}


def check(name, cond):
    results["ok" if cond else "fail"] += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")


def section(title):
    print(f"\n── {title} ──")


# Переписка за месяц: темы вперемешку, как в живом чате (одинаковые реплики
# поиск не видит — их убирает сборка памяти, здесь их и нет)
FILLER = [("user" if i % 2 == 0 else "assistant", f"{w} — запись {i}") for i, w in enumerate(
    ["Погулял по парку", "Сходил в магазин", "Посмотрел кино", "Обычный вечер", "Доделал отчёт",
     "Пил кофе", "Слушал музыку", "Убрался дома", "Встретился с другом", "Поиграл в шахматы",
     "Починил кран", "Сходил в спортзал", "Заказал пиццу", "Почитал новости", "Лёг пораньше",
     "Выспался", "Сварил суп", "Погладил рубашки", "Позвонил брату", "Купил лампочки",
     "Как дела у тебя?", "Как твои дела?", "Как дела?", "Дела нормально", "Дела идут"] * 2)]
CHAT = FILLER + [
    ("user", "Привет! Как дела?"),
    ("assistant", "Неплохо. Анализирую данные."),
    ("user", "У меня дома живёт кошка Мурка, ей три года"),
    ("assistant", "Мурка — хорошее имя для кошки."),
    ("user", "Я работаю в полиции детективом, отдел убийств"),
    ("assistant", "Значит, мы коллеги."),
    ("user", "Завтра у мамы день рождения, надо купить торт"),
    ("assistant", "Какой торт она любит?"),
    ("user", "Наполеон, без вариантов"),
    ("assistant", "Классика."),
    ("user", "Ок"),
    ("assistant", "Что-то ещё?"),
    ("user", "Сегодня ездил на рыбалку на Обь, поймал щуку"),
    ("assistant", "Щука — серьёзный трофей."),
    ("user", "Читаю Азимова, «Я, робот»"),
    ("assistant", "Три закона роботехники. Знакомо."),
    ("user", "Как у тебя день прошёл?"),
    ("assistant", "Стабильно."),
    ("user", "Мурку отвезли к ветеринару, прививки сделали"),
    ("assistant", "Хорошо, что всё в порядке."),
]

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
const recall = await import(%s);
const prompt = await import(%s);
const input = JSON.parse(%s);
const docs = input.chat.map(([role, text], i) => ({ role, text, ts: 1.79e9 + i * 3600 }));
const kit = input.kit;
const out = {
  stems: input.words.map((w) => recall.stemWord(w)),
  terms: input.queries.map((q) => recall.terms(q)),
  search: input.queries.map((q) => recall.searchDocs(q, docs, 3).map((d) => d.text)),
  facts: recall.searchDocs('где я работаю?', input.facts.map((f) => ({ role: 'fact', text: f })), 3).map((d) => d.text),
  recallBlock: recall.formatRecall(kit, recall.searchDocs('как зовут мою кошку', docs, 3)),
  budget: prompt.LOCAL_PROMPT_CHARS,
  prompts: input.cases.map((c) => {
    const k = { ...kit, ...c.kit, parts: { ...kit.parts, ...(c.kit.parts || {}) } };
    const memory = { recall: docs.slice(0, c.recall || 0), facts: [] };
    return prompt.localPrompt(k, memory, c.notes || [], c.history, c.text);
  }),
};
console.log(JSON.stringify(out));
"""


def run_node(payload):
    code = NODE_CODE % (json.dumps((OFFLINE / "recall.ts").as_uri()),
                        json.dumps((OFFLINE / "prompt.ts").as_uri()),
                        json.dumps(json.dumps(payload, ensure_ascii=False)))
    res = subprocess.run(["node", "--input-type=module", "-e", code],
                         capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip()[:600])
    return json.loads(res.stdout.strip().splitlines()[-1])


def main():
    print("Память на телефоне: поиск по словам и промпт модели телефона")
    if not shutil.which("node"):
        print("  (пропущено — нет Node)")
        return 0

    words = ["уведомления", "уведомлений", "уведомлять", "кота", "коты", "котом", "кот", "Ёлка", "елки", "cats",
             "reading"]
    queries = ["Как зовут мою кошку?", "что я говорил про работу", "помнишь, какой торт любит мама?",
               "привет, как дела?", "ок", "какая погода на Марсе", "куда я ездил на рыбалку и кого поймал",
               "что сделали у ветеринара"]
    character = "Ты Коннор, андроид-детектив. " * 300  # ~9000 символов
    kit = {
        "version": 3, "persona": "connor", "name": "Коннор",
        "parts": {"character": "Ты Коннор.", "rules": "RULES", "style": "STYLE", "language": "LANG"},
        "facts": [f"Факт {i}" for i in range(80)],
        "dossier": "Known about the user (chat analysis): interests: рыбалка",
        "self_memory": "[PERSONAL MEMORY Коннор]\nStory: " + "день за днём. " * 20 + "\n[END OF PERSONAL MEMORY]",
        "rules": {"web": "", "env": "", "self_memory": "SELF RULE", "recall_head": "HEAD:", "recall": "RECALL RULE"},
    }
    now = time.time()
    line = "реплика " * 40  # ~320 символов
    hist30 = [{"role": "user" if i % 2 == 0 else "assistant", "text": f"{i}: {line}"} for i in range(30)]
    cases = [
        # 0. Маленькая персона: всё целиком, история — 8 реплик
        {"kit": {}, "history": hist30, "text": "Как дела?"},
        # 1. Огромная персона: характер ужат, история влезает в бюджет
        {"kit": {"parts": {"character": character}, "self_memory": "[PERSONAL MEMORY]\n" + "эпизод. " * 600}, "history": hist30,
         "text": "Как дела?", "recall": 3, "notes": ["NOTE " * 100]},
        # 2. История кончается репликой пользователя — она вместе с новой
        {"kit": {}, "history": [{"role": "assistant", "text": "Здравствуй"}, {"role": "user", "text": "ты тут?"},
                                {"role": "assistant", "text": "Да."}, {"role": "user", "text": "эй"}],
         "text": "ответь", "notes": []},
        # 4. Заметки огромные (выдача поиска) — промпт ужимается до третьего уровня
        {"kit": {"parts": {"character": character}, "self_memory": "[PERSONAL MEMORY]\n" + "эпизод. " * 600},
         "history": hist30, "text": "Как дела?", "recall": 3, "notes": ["NOTE " * 700]},
        # 3. Одна огромная реплика — обрезана
        {"kit": {}, "history": [{"role": "user", "text": "а" * 5000}, {"role": "assistant", "text": "ок"}],
         "text": "дальше"},
        # 5. Хвост — старая реплика без ответа (час назад): не к этому ходу
        {"kit": {}, "history": [{"role": "user", "text": "привет", "ts": now - 7200},
                                {"role": "assistant", "text": "Здравствуй.", "ts": now - 7190},
                                {"role": "user", "text": "открой телеграм", "ts": now - 3600}],
         "text": "мне грустно"},
        # 6. Хвост — свежие реплики без ответа (очередь без связи): вместе с новой;
        # из смешанного хвоста старая уходит, свежая остаётся
        {"kit": {}, "history": [{"role": "assistant", "text": "Да.", "ts": now - 9000},
                                {"role": "user", "text": "открой телеграм", "ts": now - 7200},
                                {"role": "user", "text": "ты тут?", "ts": now - 120}],
         "text": "ответь"},
    ]
    facts = ["Имя: Хэнк", "Работа: детектив в полиции", "Любит рыбалку", "Кошка Мурка", "Город: Детройт"]
    try:
        got = run_node({"chat": CHAT, "words": words, "queries": queries, "kit": kit, "cases": cases,
                        "facts": facts})
    except RuntimeError as e:
        check(f"recall.ts и prompt.ts запускаются под Node: {e}", False)
        return 1

    section("A. Основы слов")
    st = dict(zip(words, got["stems"]))
    check("уведомления / уведомлений / уведомлять — одна основа",
          st["уведомления"] == st["уведомлений"] == st["уведомлять"] == "уведом")
    check("кот / кота / коты / котом — одна основа", st["кот"] == st["кота"] == st["коты"] == st["котом"] == "кот")
    check("ё = е", st["Ёлка"] == st["елки"])
    check("английские -s, -ing", st["cats"] == "cat" and st["reading"] == "read")
    tm = dict(zip(queries, got["terms"]))
    check("служебные слова — не тема", tm["Как зовут мою кошку?"] == ["зов", "кошк"])
    check("«работаю» и «работу» — одна основа", tm["что я говорил про работу"][-1] == "работ")
    check("«привет, как дела?» — тема «дела»", tm["привет, как дела?"] == ["дел"])

    section("B. Поиск реплик к теме вопроса")
    sr = dict(zip(queries, got["search"]))
    check("«как зовут мою кошку» — реплики про Мурку",
          sr["Как зовут мою кошку?"] and all("Мурка" in t for t in sr["Как зовут мою кошку?"][:2]))
    check("«про работу» — полиция", any("полиции" in t for t in sr["что я говорил про работу"]))
    check("«какой торт любит мама» — день рождения или «Наполеон»",
          any("торт" in t.lower() for t in sr["помнишь, какой торт любит мама?"]))
    check("«рыбалка, кого поймал» — щука на Оби",
          sr["куда я ездил на рыбалку и кого поймал"][:1] == ["Сегодня ездил на рыбалку на Обь, поймал щуку"])
    check("«ветеринар» — свежая реплика про прививки",
          sr["что сделали у ветеринара"][:1] == ["Мурку отвезли к ветеринару, прививки сделали"])
    check("«ок» — ничего (нет темы)", sr["ок"] == [])
    check("«привет, как дела?» — ничего: «дела» в каждой второй реплике", sr["привет, как дела?"] == [])
    check("«погода на Марсе» — ничего (такого не было)", sr["какая погода на Марсе"] == [])
    check("не больше трёх", all(len(v) <= 3 for v in sr.values()))
    check("факты к теме: «где я работаю» — работа", got["facts"][:1] == ["Работа: детектив в полиции"])
    block = got["recallBlock"]
    check("блок найденного — заголовок и правило кита, метки времени",
          block.startswith("HEAD:\n  User [") and block.rstrip().endswith("RECALL RULE") and "Мурка" in block)

    section("C. Промпт модели телефона в бюджете")
    budget = got["budget"]

    def total(pr):
        return len(pr["system"]) + sum(len(m["content"]) for m in pr["history"]) + len(pr["text"])

    p0, p1, p2, p4, p3, p5, p6 = got["prompts"]
    check("без найденного — реплика как есть", p0["text"] == "Как дела?")
    check("маленькая персона: характер, правила, факты, досье, личная память, язык",
          all(x in p0["system"] for x in ("Ты Коннор.", "RULES", "STYLE", "Факт 79", "interests: рыбалка",
                                          "[PERSONAL MEMORY Коннор]", "SELF RULE", "LANG")))
    check("…факты — последние 25", "Факт 55" in p0["system"] and "Факт 54" not in p0["system"])
    check(f"…в бюджете {budget} символов", total(p0) <= budget)
    check("…история — сколько влезло (≤8, по порядку, первая — пользователя)",
          0 < len(p0["history"]) <= 8 and p0["history"][0]["role"] == "user"
          and p0["history"][-1]["content"].startswith("29:"))
    check(f"огромная персона — в бюджете ({total(p1)} ≤ {budget})", total(p1) <= budget)
    self1 = p1["system"].split("[PERSONAL MEMORY]")[1].split("[END OF PERSONAL MEMORY]")[0]
    check("…характер ≤ 4000, личная память обрезана (≤ 1200, не пустая), найденное ≤ 3",
          len(p1["system"].split("\n\nRULES")[0]) <= 4000 and 500 < len(self1) <= 1200
          and "эпизод" in self1 and "SELF RULE" in p1["system"])
    note1 = p1["text"].split("\n\nКак дела?")[0]
    check("…найденное — в реплике перед вопросом (до 3 строк), не в системном тексте",
          note1.startswith("(Note from the app") and 1 <= note1.count("the user wrote: ") + note1.count("you replied: ") <= 3
          and p1["text"].endswith("Как дела?") and "Note from the app" not in p1["system"])
    check("…места под историю — не меньше 1500 символов",
          sum(len(m["content"]) for m in p1["history"]) + len(p1["text"]) >= 1500 - 400)
    check("…история всё равно есть (≥ 2 реплики)", len(p1["history"]) >= 1 and len(p1["text"]) >= len("Как дела?"))
    check("заметки огромные — характер до 2000, без личной памяти, найденное ≤ 2",
          len(p4["system"].split("\n\nRULES")[0]) <= 2000 and "SELF RULE" not in p4["system"]
          and p4["text"].count("the user wrote: ") + p4["text"].count("you replied: ") <= 2)
    check("…и всё равно есть история (последняя реплика)", len(p4["history"]) >= 1 or "29:" in p4["text"])
    check("хвост — реплика пользователя: уходит вместе с новой",
          p2["text"] == "эй\n\nответь" and [m["role"] for m in p2["history"]] == ["user", "assistant"]
          and p2["history"][0]["content"] == "ты тут?")
    check("огромная реплика истории — обрезана до 600 с «…»",
          p3["history"][0]["content"] == "а" * 600 + "…")
    check("старая реплика без ответа в хвосте — не склеивается с новой",
          p5["text"] == "мне грустно" and "телеграм" not in json.dumps(p5, ensure_ascii=False)
          and [m["role"] for m in p5["history"]] == ["user", "assistant"])
    check("свежая реплика без ответа — вместе с новой, старая из того же хвоста — нет",
          p6["text"] == "ты тут?\n\nответь" and "телеграм" not in json.dumps(p6, ensure_ascii=False))

    print(f"\nИтого: {results['ok'] + results['fail']} проверок, {results['fail']} провалов")
    return 1 if results["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
