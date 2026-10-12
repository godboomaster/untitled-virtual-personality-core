"""Поиск по смыслу в памяти персоны на телефоне без ноутбука:

  - векторы с ноутбука: offline_api.pack_vector (int8, base64) → unpackVec
    (web/src/offline/vectors.ts) — косинус с исходным не теряется;
  - гибридный поиск (web/src/offline/recall.ts) на настоящих векторах модели
    ядра: находит сказанное другими словами («кот» → «кошка Мурка», «собака»
    → «my dog»), чего поиск по словам не видит, и не тянет шум — короткие
    общие реплики, вопросы не о памяти; без векторов — ровно поиск по словам;
  - то же на переписке побольше (~360 реплик): сколько вопросов другими
    словами находят своё и что находится на вопросы не о памяти;
  - модель телефона (web/src/offline/embedCore.ts: ONNX int8 +
    @huggingface/tokenizers под Node): те же номера токенов, что у
    токенизатора ядра (с обрезкой до 128), вектор — косинус с ядром ≥ 0,99,
    поиск с её вектором вопроса — тот же. Файлы модели — из кэша HF
    (Xenova/paraphrase-multilingual-MiniLM-L12-v2, ревизия как в
    OfflineLlmPlugin.java) или VPC_EMBED_MODEL_DIR; нет — раздел
    пропускается.

Модель ядра (paraphrase-multilingual-MiniLM-L12-v2) — из кэша HF, без сети.
Модули гоняются под Node (снятие типов, Node ≥ 23.6).

Запуск: python3 -m scripts.test_offline_vectors
"""

import json
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OFFLINE = ROOT / "web" / "src" / "offline"
EMBED_REPO = "Xenova/paraphrase-multilingual-MiniLM-L12-v2"
EMBED_REV = "2c4055b12046f11709e9df2c122e59ffbdc2f900"

results = {"ok": 0, "fail": 0}


def check(name, cond):
    results["ok" if cond else "fail"] += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")


def section(title):
    print(f"\n── {title} ──")


# Переписка: темы вперемешку с болтовнёй — как в test_offline_recall
FILLER = [("user" if i % 2 == 0 else "assistant", f"{w} — запись {i}") for i, w in enumerate(
    ["Погулял по парку", "Сходил в магазин", "Посмотрел кино", "Обычный вечер", "Доделал отчёт",
     "Пил кофе", "Слушал музыку", "Убрался дома", "Встретился с другом", "Поиграл в шахматы",
     "Починил кран", "Сходил в спортзал", "Заказал пиццу", "Почитал новости", "Лёг пораньше",
     "Выспался", "Сварил суп", "Погладил рубашки", "Позвонил брату", "Купил лампочки",
     "Как дела у тебя?", "Как твои дела?", "Как дела?", "Дела нормально", "Дела идут"] * 2)]
CAT = "У меня дома живёт кошка Мурка, ей три года"
CHAT = FILLER + [
    ("user", "Привет! Как дела?"),
    ("assistant", "Неплохо. Анализирую данные."),
    ("user", CAT),
    ("assistant", "Мурка — хорошее имя для кошки."),
    ("user", "Я работаю в полиции детективом, отдел убийств"),
    ("assistant", "Значит, мы коллеги."),
    ("user", "Завтра у мамы день рождения, надо купить торт"),
    ("assistant", "Какой торт она любит?"),
    ("user", "Сегодня ездил на рыбалку на Обь, поймал щуку"),
    ("assistant", "Щука — серьёзный трофей."),
    ("user", "My dog Rex is five years old"),
    ("assistant", "Rex sounds like a good boy."),
    ("user", "Ок"),
    ("assistant", "Что-то ещё?"),
]
FACTS = ["Работает детективом в полиции", "Живёт в Новосибирске", "Любит рыбалку", "Есть кошка Мурка",
         "Не пьёт кофе после шести"]

# вопрос → (реплика, как должна найтись): first — первой, top — среди трёх,
# none — вопрос не о памяти: по смыслу ничего сверх найденного по словам
QUERIES = {
    "Как зовут моего кота?": (CAT, "top"),
    "Где я ловил рыбу?": ("Сегодня ездил на рыбалку на Обь, поймал щуку", "first"),
    "Сколько лет моей собаке?": ("My dog Rex is five years old", "first"),
    "Кем я работаю?": ("Я работаю в полиции детективом, отдел убийств", "first"),
    "Как дела?": (None, "none"),
    "Расскажи анекдот": (None, "none"),
    "какая погода завтра": (None, "none"),
    "напомни через 5 минут позвонить": (None, "none"),
    "Чем я увлекаюсь?": (None, "none"),
}

# Переписка побольше: 16 тем среди ~330 коротких бытовых реплик
TOPICS = [
    ("У меня дома живёт кошка Мурка, ей три года", "Как зовут моего кота?"),
    ("Я работаю в полиции детективом, отдел убийств", "Какая у меня профессия?"),
    ("Завтра у мамы день рождения, надо купить торт Наполеон", "Что я хотел купить маме?"),
    ("Сегодня ездил на рыбалку на Обь, поймал щуку", "Где я ловил рыбу?"),
    ("My dog Rex is five years old", "Сколько лет моей собаке?"),
    ("Сестра Лена переезжает в Казань в следующем месяце", "Куда переезжает моя сестра?"),
    ("У меня аллергия на арахис, даже немного нельзя", "На что у меня аллергия?"),
    ("Купил подержанную Тойоту Королла 2015 года", "Какая у меня машина?"),
    ("Записался на курсы испанского по вторникам", "Какой язык я учу?"),
    ("В детстве я жил в Норильске, там очень холодно", "Где я вырос?"),
    ("Мой любимый фильм — Бегущий по лезвию", "Какое кино мне нравится?"),
    ("Сломал руку на сноуборде прошлой зимой", "Что случилось с моей рукой?"),
    ("Играю на бас-гитаре в группе с друзьями", "На каком инструменте я играю?"),
    ("Начальник опять задержал зарплату на неделю", "Что там с деньгами на работе?"),
    ("Летом хочу съездить в Грузию, в Тбилиси", "Куда я собираюсь в отпуск?"),
    ("Бросил курить три месяца назад", "Я курю?"),
]
BIG_FACTS = ["Работает детективом в полиции", "Есть кошка Мурка", "Аллергия на арахис", "Сестра Лена", "Бросил курить"]
# вопрос темы → её факт
FACT_OF = {"Какая у меня профессия?": "Работает детективом в полиции", "Как зовут моего кота?": "Есть кошка Мурка",
           "На что у меня аллергия?": "Аллергия на арахис", "Куда переезжает моя сестра?": "Сестра Лена",
           "Я курю?": "Бросил курить"}
OFF_TOPIC = ["Как дела?", "Расскажи анекдот", "какая погода завтра", "напомни через 5 минут позвонить",
             "Сколько будет 2+2?", "Кто такой Наполеон Бонапарт?", "Привет, что нового?", "Чем я увлекаюсь?",
             "Что я ел вчера?", "Посоветуй книгу", "Который час?", "Переведи на английский слово дом"]
SMALL_TALK = [
    "Погулял по парку", "Сходил в магазин", "Посмотрел сериал", "Обычный вечер", "Доделал отчёт", "Пил чай",
    "Слушал музыку", "Убрался дома", "Встретился с другом", "Поиграл в шахматы", "Починил кран",
    "Сходил в спортзал", "Заказал пиццу", "Почитал новости", "Лёг пораньше", "Выспался", "Сварил суп",
    "Погладил рубашки", "Позвонил брату", "Купил лампочки", "Устал сегодня", "Весь день дождь",
    "Пробки ужасные", "Опять понедельник", "Как дела?", "Как ты?", "Что делаешь?", "Привет!", "Доброе утро",
    "Спокойной ночи", "Ок", "Ага", "Ладно", "Понятно", "Да, наверное", "Не знаю", "Скучно", "Хочу спать",
    "Ну такое", "Нормально всё", "Работаю", "Ем", "Еду домой", "Скоро буду", "Смотрю ютуб", "Сижу дома",
    "Жарко", "Холодно",
]
REPLIES = ["Хорошо.", "Понятно.", "Как прошёл день?", "Расскажи подробнее.", "Неплохо.", "Отдыхай.", "Я здесь.",
           "Слушаю."]


def big_chat():
    rnd = random.Random(1)
    chat = []
    for _ in range(170):
        chat += [("user", rnd.choice(SMALL_TALK)), ("assistant", rnd.choice(REPLIES))]
    for statement, _ in TOPICS:
        k = rnd.randrange(len(chat) // 2) * 2
        chat[k:k] = [("user", statement), ("assistant", rnd.choice(["Понял.", "Запомню.", "Ясно."]))]
    return chat


TOKEN_TEXTS = [
    "У меня дома живёт кошка Мурка, ей три года", "Как зовут моего кота?", "My dog Rex is five years old",
    "Ёлки-палки, 2+2=4 😀 ‘кавычки’ — тире… ＦＵＬＬ ｗｉｄｔｈ", "  пробелы\tи\nпереносы  ", "",
    "Очень длинное сообщение " * 60, "日本語のテキスト", "naïve café résumé",
]

HOOK = """
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
process.stdin.setEncoding('utf8'); // буквы на стыке кусков не бьются
const input = JSON.parse(await new Promise((r) => { let s = ''; process.stdin.on('data', (d) => s += d); process.stdin.on('end', () => r(s)); }));
"""

# Поиск как в ответе (engine.gatherMemory): реплики и факты отдельно, фон
# сходства — по всей памяти сразу
SEARCH_JS = """
const docsOf = (chat) => chat.map(([role, text, vec], i) => ({ role, text, ts: 1.79e9 + i * 3600, vec: vectors.unpackVec(vec) }));
const factsOf = (facts) => facts.map(([text, vec]) => ({ role: 'fact', text, vec: vectors.unpackVec(vec) }));
const texts = (list) => list.map((d) => d.text);
function search(c, query, qvec) {
  const docs = docsOf(c.chat);
  const facts = factsOf(c.facts);
  const base = qvec ? recall.vecBaseline(qvec, [...docs, ...facts]) : null;
  return {
    hybrid: texts(recall.searchDocs(query, docs, 3, qvec, base)),
    facts: texts(recall.searchDocs(query, facts, 2, qvec, base)),
    words: texts(recall.searchDocs(query, docs, 3)),
    factsWords: texts(recall.searchDocs(query, facts, 2)),
  };
}
"""

SEARCH_CODE = HOOK + """
const recall = await import(%s);
const vectors = await import(%s);
""" + SEARCH_JS + """
const q = (v) => Float32Array.from(v);
const small = input.cases[0];
const out = {
  unpack: input.packed.map((b) => { const v = vectors.unpackVec(b); return v ? Array.from(v) : null; }),
  bad: [vectors.unpackVec(''), vectors.unpackVec('###'), vectors.unpackVec('AAAA'), vectors.unpackVec(undefined)],
  cos: vectors.cosine(Float32Array.from([1, 0]), Float32Array.from([0, 1])),
  cosZero: vectors.cosine(Float32Array.from([0, 0]), Float32Array.from([1, 1])),
  cases: input.cases.map((c) => c.queries.map(([text, v]) => search(c, text, q(v)))),
  nullVec: small.queries.map(([text]) => texts(recall.searchDocs(text, docsOf(small.chat), 3, null))),
  noDocVec: small.queries.map(([text, v]) => texts(recall.searchDocs(text, docsOf(small.chat).map((d) => ({ ...d, vec: null })), 3, q(v)))),
  ownBase: small.queries.map(([text, v]) => texts(recall.searchDocs(text, docsOf(small.chat), 3, q(v)))),
  minChars: recall.VEC_MIN_CHARS,
};
console.log(JSON.stringify(out));
"""

EMBED_CODE = HOOK + """
import fs from 'node:fs';
const { createEmbedder, MAX_TOKENS } = await import(%s);
const recall = await import(%s);
const vectors = await import(%s);
const { Tokenizer } = await import('@huggingface/tokenizers');
""" + SEARCH_JS + """
const tokenizer = JSON.parse(fs.readFileSync(input.dir + '/tokenizer.json', 'utf8'));
const tok = new Tokenizer(tokenizer, {});
const ids = input.texts.map((t) => tok.encode(t, { add_special_tokens: false }).ids);
const t0 = Date.now();
const emb = await createEmbedder({ tokenizer, model: fs.readFileSync(input.dir + '/onnx/model_quantized.onnx') });
const loadMs = Date.now() - t0;
const vecs = [];
const t1 = Date.now();
for (const t of input.texts) vecs.push(Array.from(await emb.embed(t)));
const perMs = (Date.now() - t1) / input.texts.length;
// Вопрос — моделью телефона, реплики — векторами ядра: поиск как в ответе
const found = [];
for (const [text] of input.case.queries) found.push(search(input.case, text, await emb.embed(text)));
await emb.release();
let released = false;
try { await emb.embed('x'); } catch { released = true; }
console.log(JSON.stringify({ ids, vecs, loadMs, perMs, found, released, maxTokens: MAX_TOKENS }));
"""


def run_node(code, stdin, cwd=None):
    res = subprocess.run(["node", "--input-type=module", "-e", code], input=stdin, cwd=cwd,
                         capture_output=True, text=True, timeout=300)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip()[-800:])
    return json.loads(res.stdout.strip().splitlines()[-1])


def model_dir():
    env = os.environ.get("VPC_EMBED_MODEL_DIR")
    if env and (Path(env) / "onnx" / "model_quantized.onnx").exists():
        return Path(env)
    try:
        from huggingface_hub import try_to_load_from_cache
        onnx = try_to_load_from_cache(EMBED_REPO, "onnx/model_quantized.onnx", revision=EMBED_REV)
        tok = try_to_load_from_cache(EMBED_REPO, "tokenizer.json", revision=EMBED_REV)
        if isinstance(onnx, str) and isinstance(tok, str):
            return Path(onnx).parent.parent
    except Exception:
        pass
    return None


def main():
    print("Поиск по смыслу в памяти на телефоне")
    if not shutil.which("node"):
        print("  (пропущено — нет Node)")
        return 0
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    import numpy as np
    from sentence_transformers import SentenceTransformer
    from app.api.offline_api import pack_vector
    from app.core.st_embedder import ST_MODEL_NAME

    model = SentenceTransformer(ST_MODEL_NAME)

    def case(chat, facts, queries):
        return {
            "chat": [[role, text, pack_vector(v)] for (role, text), v in zip(chat, model.encode([t for _, t in chat]))],
            "facts": [[f, pack_vector(v)] for f, v in zip(facts, model.encode(facts))],
            "queries": [[q, v.tolist()] for q, v in zip(queries, model.encode(queries))],
        }

    def cos(a, b):
        a, b = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
        return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))

    queries = list(QUERIES)
    small = case(CHAT, FACTS, queries)
    chat2 = big_chat()
    topic_q = [q for _, q in TOPICS]
    big = case(chat2, BIG_FACTS, topic_q + OFF_TOPIC)
    sample = [np.random.default_rng(i).standard_normal(384).astype(np.float32) for i in range(3)]
    out = run_node(SEARCH_CODE % (json.dumps((OFFLINE / "recall.ts").as_uri()),
                                  json.dumps((OFFLINE / "vectors.ts").as_uri())),
                   json.dumps({"cases": [small, big], "packed": [pack_vector(v) for v in sample]},
                              ensure_ascii=False))

    section("A. Векторы с ноутбука: int8 base64 → телефон")
    check("распаковка — тот же вектор (косинус ≥ 0.999)",
          all(u is not None and cos(u, v) >= 0.999 for u, v in zip(out["unpack"], sample)))
    check("пусто, не base64, не той длины — null", out["bad"] == [None, None, None, None])
    check("косинус: перпендикулярные — 0, нулевой вектор — 0", out["cos"] == 0 and out["cosZero"] == 0)

    section("B. Гибридный поиск на векторах модели ядра")
    found = dict(zip(queries, out["cases"][0]))
    for q, (want, how) in QUERIES.items():
        r = found[q]
        if how == "first":
            check(f"«{q}» → «{want[:28]}…» первой", r["hybrid"][:1] == [want])
        elif how == "top":
            check(f"«{q}» → «{want[:28]}…» среди трёх", want in r["hybrid"])
        else:
            check(f"«{q}» — не о памяти: по смыслу ничего сверх слов",
                  set(r["hybrid"]) <= set(r["words"]) and set(r["facts"]) <= set(r["factsWords"]))
    check("«кот» — по словам не находится, по смыслу находится", CAT not in found["Как зовут моего кота?"]["words"])
    check("…и факт «Есть кошка Мурка» — только по смыслу",
          found["Как зовут моего кота?"]["facts"][:1] == ["Есть кошка Мурка"]
          and found["Как зовут моего кота?"]["factsWords"] == [])
    check("«собака» по-русски → «my dog» по-английски — только по смыслу",
          "My dog Rex is five years old" not in found["Сколько лет моей собаке?"]["words"])
    check("«Кем я работаю?» — без болтовни «как дела»",
          not any("дела" in t.lower() for t in found["Кем я работаю?"]["hybrid"]))
    check("без вектора вопроса — ровно поиск по словам", out["nullVec"] == [found[q]["words"] for q in queries])
    check("у реплик нет векторов — тоже поиск по словам", out["noDocVec"] == [found[q]["words"] for q in queries])
    check("фон по самим репликам (без общего) — то же самое",
          out["ownBase"] == [found[q]["hybrid"] for q in queries])

    section("C. Переписка побольше: другими словами и не о памяти")
    res = out["cases"][1]
    on, off = res[:len(TOPICS)], res[len(TOPICS):]
    hits = [st for (st, _), r in zip(TOPICS, on) if st in r["hybrid"]]
    word_hits = [st for (st, _), r in zip(TOPICS, on) if st in r["words"]]
    check(f"вопрос другими словами находит свою реплику: {len(hits)} из {len(TOPICS)} (по словам — "
          f"{len(word_hits)}), нужно ≥ 10", len(hits) >= 10 and set(word_hits) <= set(hits))
    extra = [t for r in on for t in r["hybrid"] if t not in r["words"]]
    check(f"находки только по смыслу — не короче {out['minChars']} символов (болтовня не тянется)",
          all(len(t) >= out["minChars"] for t in extra))
    check("…и это сами темы, а не ответы «Понял.»/«Запомню.»",
          all(t in {st for st, _ in TOPICS} for t in extra))
    noise = [(q, t) for q, r in zip(OFF_TOPIC, off) for t in [*r["hybrid"], *r["facts"]]
             if t not in r["words"] and t not in r["factsWords"]]
    check(f"вопросы не о памяти — по смыслу ничего сверх слов ({len(OFF_TOPIC)} вопросов)", noise == [])
    by_q = dict(zip(topic_q, on))
    fact_found = sum(1 for q, f in FACT_OF.items() if by_q[q]["facts"][:1] == [f])
    check(f"свой факт первым: {fact_found} из {len(FACT_OF)} тем с фактами, нужно ≥ 4", fact_found >= 4)
    check("…«кот» и «профессия» — только по смыслу (по словам факта нет)",
          all(not by_q[q]["factsWords"] for q in ("Как зовут моего кота?", "Какая у меня профессия?")))

    section("D. Модель телефона под Node: токены и векторы как у ядра")
    mdir = model_dir()
    if mdir is None or not (ROOT / "web" / "node_modules" / "onnxruntime-web").exists():
        print(f"  (пропущено — нет {'файлов модели (кэш HF или VPC_EMBED_MODEL_DIR)' if mdir is None else 'web/node_modules'})")
    else:
        emb = run_node(EMBED_CODE % tuple(json.dumps((OFFLINE / f).as_uri())
                                          for f in ("embedCore.ts", "recall.ts", "vectors.ts")),
                       json.dumps({"dir": str(mdir), "texts": TOKEN_TEXTS, "case": big}, ensure_ascii=False),
                       cwd=str(ROOT / "web"))
        cosines = [cos(a, b) for a, b in zip(emb["vecs"], model.encode(TOKEN_TEXTS))]
        check(f"вектор телефона ≈ вектору ядра (мин. косинус {min(cosines):.4f} ≥ 0.99)", min(cosines) >= 0.99)
        py_ids = [model.tokenizer(t, add_special_tokens=False)["input_ids"] for t in TOKEN_TEXTS]
        check("номера токенов — как у токенизатора ядра (ru, en, эмодзи, полная ширина, пусто)", emb["ids"] == py_ids)
        check("обрезка — как у ядра (128 токенов)", emb["maxTokens"] == 128 == model.max_seq_length)
        same = sum(1 for a, b in zip(emb["found"], res) if a == b)
        check(f"вопрос моделью телефона, реплики векторами ядра — найдено то же: {same} из {len(res)}",
              same >= len(res) - 1)
        check("после release — не считает", emb["released"])
        print(f"  (загрузка {emb['loadMs']} мс, вопрос ≈ {emb['perMs']:.0f} мс — Мак, один поток)")

    print(f"\nИтого: {results['ok'] + results['fail']} проверок, {results['fail']} провалов")
    return 1 if results["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
