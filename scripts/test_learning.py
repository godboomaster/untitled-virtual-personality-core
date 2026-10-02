"""Тест режима обучения (LearningManager): неотвеченный тест не стопорит курс,
урок доходит текстом в транспорт без файлов (веб-чат), тест в веб-чате
остаётся проверяемым, служебный VERDICT не утекает в фидбек, просьба
остановить курс фразой/командой, распознавание просьбы «научи» и ответа
«как часто?» без ложных стартов курса, молчание по владельцу курса и по
доставленным урокам, уведомление об остановке по молчанию, все пройденные
темы в промпте урока. LLM — фейковый роутер с шаблонными
ответами. Запуск: python -m scripts.test_learning"""
import asyncio
import os
import shutil
import tempfile

_TMP = tempfile.mkdtemp(prefix="learning_test_")
os.environ["VPC_DATA_DIR"] = _TMP

from app.features.learning_intent import extract_subject, learn_request_kind  # noqa: E402
from app.features.learning_manager import (  # noqa: E402
    LearningManager, classify_continue_answer, mentions_subject, stop_request_kind)
from app.features.side_tasks import classify_learning_intent_if_enabled  # noqa: E402

CK = "c1"


class FakeRouter:
    # Шаблонные ответы по виду промпта: тест / урок / оценка ответа
    active_provider = "fake"

    def __init__(self):
        self.prompts = []

    def get_response(self, messages, **kw):
        system = messages[0]["content"]
        self.prompts.append(system)
        if "QUESTION: <question text>" in system or "QUESTION: ...\n" in system:
            return "QUESTION: new q\nANSWER: new a\nEXPLANATION: e"
        if "LESSON:\n<full lesson text" in system:
            return "TOPIC: тема\nQUESTIONS: q1; q2\nVOCAB: v\nLESSON:\nТело урока целиком."
        if "VERDICT: CORRECT | WRONG" in system:
            return "VERDICT: CORRECT\nFEEDBACK: Верно, так и есть."
        if "how often should lessons be sent" in system:
            return "1800" if "полчасика" in messages[-1]["content"] else "UNKNOWN"
        return None  # стилизация/нормализация — фолбэки менеджера


class Sender:
    def __init__(self, documents=True, replies=True):
        self.supports_documents = documents
        self.supports_replies = replies
        self.ok = True  # False — транспорт «упал», отправка не прошла
        self.out = []

    async def send_message(self, chat_id, text, topic_id=None):
        if self.ok:
            self.out.append(("msg", text))
        return self.ok

    async def send_document(self, chat_id, file_path, filename, caption=None, topic_id=None):
        if self.ok:
            self.out.append(("doc", caption))
        return self.ok


def make(documents=True, replies=True, router=True):
    shutil.rmtree(os.path.join(_TMP, "t"), ignore_errors=True)
    lm = LearningManager(context="t")
    sender = Sender(documents, replies)
    lm.set_sender(sender)
    if router:
        lm.set_routers_persona(FakeRouter(), None)
    return lm, sender


def course(lm, subject, user="u1", interval=300):
    lm.begin_setup(CK, subject, user, "U")
    return lm.commit_session(CK, interval, user_id=user)["session_id"]


def silences(lm, sid):
    return session(lm, sid)["consecutive_silences"]


def session(lm, sid):
    return lm.get_session(CK, session_id=sid)


# ── 1. Неотвеченный тест заменяется новым, курс не встаёт ──
lm, sender = make()
sid = course(lm, "испанский")
lm._set_session(CK, session_id=sid, lesson_count=5,
                quiz_pending={"question": "old q", "answer": "old a"})
asyncio.run(lm._send_lesson(session(lm, sid)))
s = session(lm, sid)
assert s["lesson_count"] == 6, s["lesson_count"]
assert s["quiz_pending"]["question"] == "new q"
assert "old a" in sender.out[-1][1], sender.out  # анонс закрывает старый вопрос ответом
# Дальше идут обычные уроки, хотя новый тест тоже висит без ответа
for _ in range(3):
    lm.record_user_activity(CK)  # пользователь пишет боту о другом
    asyncio.run(lm._send_lesson(session(lm, sid)))
assert session(lm, sid)["lesson_count"] == 9
assert session(lm, sid)["quiz_pending"]["question"] == "new q"  # тест 9 заменил тест 6
print("неотвеченный тест не стопорит курс: ok")

# ── 2. Транспорт без файлов (веб) — урок текстом целиком; Telegram — файлом ──
lm, sender = make(documents=False)
sid = course(lm, "испанский")
asyncio.run(lm._send_lesson(session(lm, sid)))
assert [k for k, _ in sender.out] == ["msg"], sender.out
assert "Тело урока целиком." in sender.out[0][1]
assert session(lm, sid)["lesson_count"] == 1
lm, sender = make(documents=True)
sid = course(lm, "испанский")
asyncio.run(lm._send_lesson(session(lm, sid)))
assert [k for k, _ in sender.out] == ["doc"], sender.out
print("урок в веб-чате доходит текстом: ok")

# ── 3. Лимит «мимо теста» не запирает тест там, где нет reply ──
for replies, expect in ((False, "Верно, так и есть."), (True, None)):
    lm, _ = make(replies=replies)
    sid = course(lm, "испанский")
    lm._set_session(CK, session_id=sid, quiz_offtopic_count=LearningManager.QUIZ_OFFTOPIC_LIMIT,
                    quiz_pending={"question": "q", "answer": "a"})
    got = lm.submit_quiz_answer(CK, "мой ответ", session_id=sid)
    assert got == expect, (replies, got)  # и без служебного «CORRECT.»
print("тест в веб-чате проверяется после лимита: ok")

# ── 4. VERDICT не утекает пользователю ──
parse = LearningManager._parse_quiz_verdict
quiz = {"answer": "a"}
assert parse("VERDICT: WRONG\nFEEDBACK: Не совсем.", quiz) == (False, "Не совсем.")
assert parse("VERDICT: OFFTOPIC\nFEEDBACK:", quiz) == (True, "")
assert parse("VERDICT: WRONG", quiz) == (False, "Правильный ответ: a.")
assert parse("VERDICT: PARTIAL\nПочти.", quiz) == (False, "Почти.")
assert parse("Хороший ответ.", quiz) == (False, "Хороший ответ.")
print("вердикт не утекает: ok")

# ── 5. Распознавание просьбы остановить ──
for text in ("хватит уроков", "Хватит на сегодня уроков", "не присылай мне больше уроки",
             "уроки больше не присылай", "отмени курс", "останови обучение",
             "не хочу больше уроков", "stop the lessons", "cancel my course"):
    assert stop_request_kind(text) == "lessons", text
for text in ("хватит учить меня испанскому", "не надо меня учить испанскому",
             "stop teaching me Spanish", "хватит меня учить"):
    assert stop_request_kind(text) == "teach", text
for text in ("не надо повторять урок", "я остановился на уроке 3", "выключи свет, у меня урок",
             "at the end of the lesson", "стоп, а что такое урок?", "как дела?",
             "научи меня испанскому"):
    assert stop_request_kind(text) is None, text
assert mentions_subject("хватит учить японскому", "японский язык")
assert not mentions_subject("хватит учить язык", "японский язык")
assert mentions_subject("stop go lessons", "Go")
print("распознавание просьбы остановить: ok")

# ── 6. Остановка курса ──
lm, _ = make(router=False)
es = course(lm, "испанский язык")
assert lm.handle_stop_request(CK, "u1", "хватит меня учить") is None  # идиома без темы
assert lm.handle_stop_request(CK, "u1", "расскажи анекдот") is None
r = lm.handle_stop_request(CK, "u1", "хватит учить меня испанскому")
assert r == {"kind": "stopped", "subjects": ["испанский язык"]}, r
assert not lm.get_sessions(CK)
assert lm.handle_stop_request(CK, "u1", "хватит уроков") is None  # курсов нет — не наше
assert lm.handle_stop_request(CK, "u1", "", explicit=True)["kind"] == "none"

# Два курса: без темы — «какой?», ответ темой останавливает только его
es, jp = course(lm, "испанский язык"), course(lm, "японский язык")
r = lm.handle_stop_request(CK, "u1", "хватит уроков")
assert r["kind"] == "which" and set(r["subjects"]) == {"испанский язык", "японский язык"}
assert "«испанский язык»" in lm.render_stop_reply(r)
r = lm.handle_stop_request(CK, "u1", "японский")
assert r == {"kind": "stopped", "subjects": ["японский язык"]}, r
assert [s["session_id"] for s in lm.get_sessions(CK)] == [es]
# Вопрос «какой?», ответ не про курсы — обычное сообщение, вопрос снят
jp = course(lm, "японский язык")
assert lm.handle_stop_request(CK, "u1", "хватит уроков")["kind"] == "which"
assert lm.handle_stop_request(CK, "u1", "как дела?") is None
assert lm.handle_stop_request(CK, "u1", "японский") is None
assert len(lm.get_sessions(CK)) == 2
# «все» — останавливает все
r = lm.handle_stop_request(CK, "u1", "хватит всех уроков")
assert r["kind"] == "stopped" and not lm.get_sessions(CK)

# Команда: единственный курс без аргумента — стоп; тема не та — список
es = course(lm, "испанский язык")
r = lm.handle_stop_request(CK, "u1", "химия", explicit=True)
assert r == {"kind": "which", "subjects": ["испанский язык"]}, r
r = lm.handle_stop_request(CK, "u1", "", explicit=True)
assert r == {"kind": "stopped", "subjects": ["испанский язык"]}, r

# Настройка нового курса («как часто?») отменяется просьбой остановить
lm.begin_setup(CK, "химия", "u1", "U")
r = lm.handle_stop_request(CK, "u1", "не, не надо уроков")
assert r == {"kind": "stopped", "subjects": ["химия"]}, r
assert lm.get_setup_state(CK, "u1") is None
print("остановка курса: ok")

# ── 7. Просьба «научи»: формы просьбы, а не корни слов ──
for text, kind, subject in (
    ("научи меня испанскому", "learn", "испанскому"),
    ("Коннор, научи меня испанскому каждый день", "learn", "испанскому"),
    ("научи меня, пожалуйста, основам криптографии", "learn", "основам криптографии"),
    ("а ты можешь научить меня готовить?", "learn", "готовить"),
    ("можешь ли ты меня научить играть на гитаре?", "learn", "играть на гитаре"),
    ("учи меня японскому", "learn", "японскому"),
    ("хочу изучить rust", "learn", "rust"),
    ("давай с тобой выучим немецкий", "learn", "немецкий"),
    ("я хотел бы научиться рисовать", "learn", "рисовать"),
    ("позанимайся со мной математикой", "learn", "математикой"),
    ("научи меня python с нуля раз в день", "learn", "python"),
    ("teach me Spanish every day", "learn", "Spanish"),
    ("I want to learn Go", "learn", "Go"),
    ("I'd like to learn French please", "learn", "French"),
    ("научи меня", "learn", ""),
    ("научи, как сварить яйцо", "howto", "сварить яйцо"),
    ("teach me how to fix this bug", "howto", "fix this bug"),
):
    assert learn_request_kind(text) == kind, (text, learn_request_kind(text))
    assert extract_subject(text) == subject, (text, extract_subject(text))
for text in ("научись уже шутить", "выучил наконец таблицу умножения", "меня в школе научили",
             "я не хочу учить уроки", "don't teach me", "учи уроки", "научный подход",
             "расскажи про CBC-MAC", "как дела?"):
    assert learn_request_kind(text) is None, text
assert extract_subject("испанский") == "испанский"  # ответ на «чему учить?»


class FakeBot:
    features = {}  # side_tasks не заданы — LLM-уточнение выключено (как у Коннора)
    _local_router = None


for text, verdict in (("научи меня испанскому", "LEARN"), ("научи, как сварить яйцо", "INFO"),
                      ("научись уже шутить", "INFO"), ("выучил наконец таблицу", "INFO")):
    assert classify_learning_intent_if_enabled(FakeBot(), text) == verdict, text
print("распознавание просьбы «научи»: ok")

# ── 8. Ответ на «как часто?»: без reply — только явная частота ──
lm, _ = make()
for text, expect in (("раз в день", (86400, True)), ("5 часов", (18000, True)),
                     ("давай каждые 2 часа", (7200, True)), ("через полчасика", (1800, True)),
                     ("я спал 5 часов", (None, False)),
                     ("через неделю у меня экзамен", (None, False)),
                     ("а что за час пик в Токио?", (None, False)),
                     ("в понедельник через 2 дня", (None, False))):
    assert lm.parse_setup_answer(text) == expect, (text, lm.parse_setup_answer(text))
# reply на вопрос — точно ответ: не понят — переспросить
assert lm.parse_setup_answer("ну как-нибудь", is_reply=True) == (None, True)
print("ответ на «как часто?»: ok")

# ── 9. Молчание — про ученика: в группе чужие реплики чужой курс не держат ──
lm, _ = make(router=False)
mine, legacy = course(lm, "испанский", user="u1"), course(lm, "химия", user="default")
for sid in (mine, legacy):
    lm._set_session(CK, session_id=sid, consecutive_silences=2)
lm.record_user_activity(CK, "u2")  # пишет другой участник группы
assert silences(lm, mine) == 2 and silences(lm, legacy) == 0  # курс без владельца — как раньше
lm.record_user_activity(CK, "u1")
assert silences(lm, mine) == 0
lm._set_session(CK, session_id=mine, consecutive_silences=2)
lm.record_user_activity(CK)  # вызов без user_id — любое сообщение, как раньше
assert silences(lm, mine) == 0
print("молчание по владельцу курса: ok")

# ── 10. Молчание растёт при доставке урока, а не при попытке ──
lm, sender = make()
sid = course(lm, "испанский")
sender.ok = False  # сбой транспорта / персона заморожена — урок не дошёл
asyncio.run(lm._send_lesson(session(lm, sid)))
assert silences(lm, sid) == 0 and session(lm, sid)["lesson_count"] == 0
sender.ok = True
asyncio.run(lm._send_lesson(session(lm, sid)))
assert silences(lm, sid) == 1 and session(lm, sid)["lesson_count"] == 1
lm._set_session(CK, session_id=sid, lesson_count=2)  # следующий — тест
asyncio.run(lm._send_lesson(session(lm, sid)))
assert silences(lm, sid) == 2 and session(lm, sid)["quiz_pending"]
print("молчание считается по доставленным урокам: ok")


# ── 11. Остановка по молчанию — с уведомлением, «да» успевает её отменить ──
async def one_loop_tick(lm):
    lm._running = True
    task = asyncio.create_task(lm._loop())
    await asyncio.sleep(0.3)  # первая итерация цикла, дальше он спит 30 с
    lm._running = False
    task.cancel()


lm, sender = make(router=False)
sid = course(lm, "испанский")
lm._set_session(CK, session_id=sid, asked_continue=True, next_lesson_at=0)
asyncio.run(one_loop_tick(lm))
assert not lm.get_sessions(CK)
assert len(sender.out) == 1 and "испанский" in sender.out[0][1], sender.out
lm, sender = make(router=False)
sid = course(lm, "испанский")
lm._set_session(CK, session_id=sid, asked_continue=True, next_lesson_at=0)
lm.resolve_continue(CK, "YES", session_id=sid)  # ответил до тика
asyncio.run(lm._stop_by_silence(session(lm, sid)))
assert lm.get_sessions(CK) and not sender.out
print("остановка по молчанию с уведомлением: ok")

# ── 12. В промпт урока идут все пройденные темы, а не последние 8 ──
lm, _ = make()
sid = course(lm, "испанский")
topics = [f"тема {i}" for i in range(15)]
lm._set_session(CK, session_id=sid, covered_topics=topics)
lm._generate_lesson_text(session(lm, sid))
prompt = next(p for p in lm._router.prompts if "Previously covered" in p)
assert all(t in prompt for t in topics), prompt[:300]
print("все пройденные темы в промпте урока: ok")

# ── 13. Тест — по тексту последних уроков ──
lm, _ = make()
sid = course(lm, "испанский")
for _ in range(4):  # урок, урок, тест, урок
    asyncio.run(lm._send_lesson(session(lm, sid)))
s13 = session(lm, sid)
assert len(s13["recent_lessons"]) == 3 and s13["recent_lessons"][0]["text"] == "Тело урока целиком."
lm._router.prompts.clear()
lm._generate_quiz(s13)
quiz_prompt = next(p for p in lm._router.prompts if "QUESTION: <question text>" in p)
assert "Material of the recent lessons" in quiz_prompt and "Тело урока целиком." in quiz_prompt
print("тест по тексту уроков: ok")

# ── 14. Фоновые тексты курса — на языке ученика ──
lm, sender = make(router=False)
sid = course(lm, "Spanish grammar")
en = session(lm, sid)
assert lm._session_language(en) == "en"
assert "Shall we continue the course? (yes/no)" in lm._render_continue_question(en)
assert "teach me Spanish grammar" in lm._render_auto_stop_notice(en)
assert lm._plain_lesson_caption("Verbs", 2, ["q"], "en") == "Lesson 2: Verbs\n\nReview questions:\n1. q"
assert lm._render_quiz_announcement("Spanish grammar", "q?", "en").startswith("Quiz on")
asyncio.run(lm._send_lesson(en))  # роутера нет — урок не готов, сообщение об этом
assert sender.out[-1][1].startswith("I couldn't prepare the lesson"), sender.out
ru = session(lm, course(lm, "испанский"))
assert "Продолжаем обучение? (да/нет)" in lm._render_continue_question(ru)
for text, verdict in (("continue", "YES"), ("keep going", "YES"), ("yes, go on", "YES"),
                      ("don't continue", "NO"), ("no", "NO")):
    assert classify_continue_answer(text) == verdict, (text, classify_continue_answer(text))
print("фоновые тексты на языке ученика: ok")

shutil.rmtree(_TMP, ignore_errors=True)
print("ALL OK")
