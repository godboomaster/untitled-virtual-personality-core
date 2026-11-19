"""Банк ошибок: создание, режимы, ru/en, отсутствие повторов и работа без LLM.

python3 -m scripts.test_error_phrases
"""

import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.features import flavor_text as ft


def variants(label=""):
    return {
        "ru": {key: [f"{label} Сейчас не могу ответить. Попробуй позже.",
                     f"{label} Ответ не получается. Позови чуть позже."]
               for key in ft._ERROR_SPECS},
        "en": {key: [f"{label} I cannot answer right now. Try later.",
                     f"{label} I cannot reply yet. Please try again later."]
               for key in ft._ERROR_SPECS},
    }


class ErrorPhraseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="error_phrases_")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for p in (
            patch.dict(os.environ, {"VPC_DATA_DIR": str(root / "data")}),
            patch.object(ft, "_bank_path", lambda context: root / context / "flavor_bank.json"),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.mode = ""
        self.persona = SimpleNamespace(
            system_prompt="A theatrical knight who speaks grandly.",
            persona_data={"voice_examples": ["Hark, good friend!"]},
            modes={"sancho": {"system_prompt": "Sancho is dry and restrained.",
                              "voice_examples": ["Fine. Try later."]}},
            mode_for=lambda **kw: self.mode or None)
        self.bot = SimpleNamespace(context="test", persona=self.persona,
                                   computer_control=None, router=None)

    def profiles(self):
        return ft._error_profiles(self.persona.system_prompt, self.persona.modes,
                                  self.persona.persona_data["voice_examples"])

    def seed(self):
        bank = {"errors": {}}
        for mode, profile in self.profiles().items():
            bank["errors"][mode] = {"prompt_hash": profile["prompt_hash"],
                                     "languages": variants("Санчо" if mode else "Рыцарь")}
            # Английский раздел должен оставаться английским.
            bank["errors"][mode]["languages"]["en"] = variants("Sancho" if mode else "Knight")["en"]
        ft._save_bank(self.bot.context, bank)

    def reply(self, key="models_unavailable", lang="ru", chat="chat"):
        return ft.error_reply(self.bot, key, user_id="user", chat_id=chat, lang=lang)

    def test_mode_and_conversation_language_override_base_prompt_language(self):
        self.seed()
        self.assertTrue(self.reply().startswith("Рыцарь"))
        self.mode = "sancho"
        self.assertTrue(self.reply().startswith("Санчо"))
        self.assertTrue(self.reply(lang="en").startswith("Sancho"))
        self.mode = ""
        self.assertTrue(self.reply(lang="en").startswith("Knight"))

    def test_no_consecutive_repeats_and_last_choice_survives_bank_reload(self):
        self.seed()
        replies = [self.reply() for _ in range(30)]
        self.assertTrue(all(a != b for a, b in zip(replies, replies[1:])))
        stored = ft._load_bank("test")["last_errors"]["chat"]
        self.assertEqual(stored, replies[-1])
        self.assertNotEqual(stored, self.reply())
        # Чужой чат не расходует варианты текущего.
        with patch.object(ft.random, "choice", lambda values: values[0]):
            first = self.reply(chat="other")
            self.assertIn(first, ft._load_bank("test")["errors"][""]["languages"]["ru"]["models_unavailable"])

    def test_errors_never_call_live_model_or_schedule_generation(self):
        self.seed()
        with patch.object(ft, "_from_live", side_effect=AssertionError("network")), \
                patch.object(ft, "ensure_flavor_bank", side_effect=AssertionError("generation")), \
                patch.object(ft, "_google_cc_chat", side_effect=AssertionError("browser")):
            self.assertTrue(self.reply().startswith("Рыцарь"))
            self.mode = "sancho"
            self.persona.modes["sancho"]["system_prompt"] = "Changed character"
            self.assertIn(self.reply(), ft._ERROR_FALLBACKS["ru"]["models_unavailable"])

    def test_voice_changes_invalidate_only_matching_mode(self):
        self.seed()
        self.persona.modes["sancho"]["voice_examples"] = ["A new voice."]
        self.assertTrue(self.reply().startswith("Рыцарь"))
        self.mode = "sancho"
        self.assertIn(self.reply(), ft._ERROR_FALLBACKS["ru"]["models_unavailable"])

    def test_empty_bank_and_single_variant_still_avoid_repetition(self):
        a, b = self.reply(lang="en"), self.reply(lang="en")
        self.assertNotEqual(a, b)
        self.seed()
        bank = ft._load_bank("test")
        bank["errors"][""]["languages"]["ru"]["models_unavailable"] = ["Рыцарь пока не может ответить."]
        ft._save_bank("test", bank)
        self.assertNotEqual(self.reply(), self.reply())

    def test_generation_validates_languages_duplicates_markers_and_placeholders(self):
        raw = variants()
        raw["ru"]["models_unavailable"] = ["Не могу ответить.", "НЕ МОГУ ОТВЕТИТЬ.",
                                              "Try later.", "[ACT:sleep]", "Ошибка {detail}", 12]
        router = SimpleNamespace(answer_provider=None, get_response=lambda *a, **kw: json.dumps(raw))
        result = ft._generate_errors("test", self.profiles()[""], router)
        self.assertEqual(result["ru"]["models_unavailable"], ["Не могу ответить."])
        self.assertTrue(result["en"]["processing_error"])
        self.assertFalse(ft._errors_ready({"errors": {"": {
            "prompt_hash": self.profiles()[""]["prompt_hash"], "languages": result}}},
            {"": self.profiles()[""]}))

    def test_generation_for_all_modes_without_cc_then_fresh_bank_is_reused(self):
        with patch.object(ft, "_generate_errors", return_value=variants()) as gen, \
                patch.object(ft, "_generate_kinds", side_effect=AssertionError("CC disabled")), \
                patch.object(ft, "_generate_phrases", side_effect=AssertionError("CC disabled")):
            ft.ensure_flavor_bank(self.bot, background=False)
            self.assertEqual(gen.call_count, 2)
            self.assertTrue(ft._errors_ready(ft._load_bank("test"), self.profiles()))
            ft.ensure_flavor_bank(self.bot, background=False)
            self.assertEqual(gen.call_count, 2)
            self.persona.modes["sancho"]["voice_examples"] = ["Changed."]
            ft.ensure_flavor_bank(self.bot, background=False)
            self.assertEqual(gen.call_count, 3)

    def test_existing_cc_sections_are_preserved_and_errors_generated_first(self):
        ft._save_bank("test", {"kinds": {"generic": {"ok": ["Done."]}},
                               "phrases": {"cc_mode_on": ["On."]},
                               "_meta": {"prompt_hash": ft._prompt_hash(self.persona.system_prompt)}})
        self.bot.computer_control = object()
        with patch.object(ft, "_generate_errors", return_value=variants()), \
                patch.object(ft, "_generate_kinds", side_effect=AssertionError("CC already fresh")):
            ft.ensure_flavor_bank(self.bot, background=False)
        self.assertEqual(ft._load_bank("test")["phrases"]["cc_mode_on"], ["On."])
        self.assertTrue(ft._errors_ready(ft._load_bank("test"), self.profiles()))

    def test_creation_schedules_same_job_even_without_computer_control(self):
        from app.api import settings_api as sa
        with patch.object(ft, "ensure_flavor_bank") as ensure:
            result = sa.create_persona("id: no_cc\nname: Knight\nsystem_prompt: A knight\n"
                                       "features:\n  computer_control: false\n"
                                       "llm:\n  primary: mimo\n")
        self.assertTrue(result["ok"])
        self.assertEqual(ensure.call_count, 1)
        opts = ensure.call_args.kwargs
        self.assertEqual(opts["context"], "api_no_cc")
        self.assertFalse(opts["include_cc"])
        self.assertEqual(opts["llm"]["primary"], "mimo")

    def test_scheduling_does_not_block_behind_another_generation(self):
        done = threading.Event()
        finished = threading.Event()

        def generate(*a, **kw):
            with ft._GEN_LOCK:
                with ft._GEN_STARTED_LOCK:
                    ft._GEN_STARTED.discard("test")
                finished.set()

        def schedule():
            ft.ensure_flavor_bank(self.bot)
            done.set()

        with patch.object(ft, "_generate_safe", generate):
            ft._GEN_LOCK.acquire()
            t = threading.Thread(target=schedule)
            try:
                t.start()
                self.assertTrue(done.wait(0.5))
            finally:
                ft._GEN_LOCK.release()
                t.join(2)
            self.assertTrue(finished.wait(2))

    def test_mode_edit_during_generation_is_not_lost(self):
        entered, release = threading.Event(), threading.Event()
        seen = []

        def generate(context, profile, router):
            seen.append(profile["prompt_hash"])
            if len(seen) == 1:
                entered.set()
                release.wait(3)
            return variants()

        with patch.object(ft, "_generate_errors", generate):
            try:
                ft.ensure_flavor_bank(self.bot)
                self.assertTrue(entered.wait(2))
                self.persona.modes["sancho"]["system_prompt"] = "A newly edited Sancho"
                ft.ensure_flavor_bank(self.bot)
            finally:
                release.set()
            deadline = time.monotonic() + 3
            while "test" in ft._GEN_STARTED and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertNotIn("test", ft._GEN_STARTED)
        self.assertTrue(ft._errors_ready(ft._load_bank("test"), self.profiles()))
        self.assertEqual(len(seen), 3)  # готовый обычный режим не генерируется заново

    def test_new_mode_schedules_generation_before_bot_is_loaded(self):
        from app.api import modes_api, settings_api
        with patch.object(ft, "ensure_flavor_bank") as ensure:
            self.assertTrue(settings_api.create_persona(
                "id: no_cc\nname: Knight\nsystem_prompt: A knight\n")["ok"])
            ensure.reset_mock()
            modes_api.save("no_cc", "sancho",
                           "name: Sancho\nsystem_prompt: A restrained Sancho\n"
                           "triggers:\n  names: [sancho]\n", create=True)
        self.assertEqual(ensure.call_count, 1)
        self.assertIn("sancho", ensure.call_args.kwargs["modes"])
        self.assertFalse(ensure.call_args.kwargs["include_cc"])

    def test_command_model_failure_uses_bank_and_is_stored(self):
        from scripts.test_turn_cancel import make_bot
        bot, _, saved = make_bot(None)
        bot.context = "test"
        bot.persona.system_prompt = self.persona.system_prompt
        bot.persona.persona_data = self.persona.persona_data
        bot.persona.modes = self.persona.modes
        bot.persona.mode_for = self.persona.mode_for
        self.seed()
        reply = bot.command_reply("The task list is empty.", "todo", "web_user", "web_user",
                                  "User", "show my tasks")
        self.assertTrue(reply.startswith("Knight"), reply)
        self.assertIn(("assistant", reply), saved)

    def test_model_failure_and_pipeline_error_are_stored_in_real_turn(self):
        from scripts.test_turn_cancel import make_bot
        for kind, response in (("models_unavailable", None), ("processing_error", "unused")):
            with self.subTest(kind=kind):
                bot, _, saved = make_bot(response)
                bot.context = self.bot.context
                # Сохраняем рабочие методы заготовки пайплайна, добавляя характер.
                bot.persona.system_prompt = self.persona.system_prompt
                bot.persona.persona_data = self.persona.persona_data
                bot.persona.modes = self.persona.modes
                bot.persona.mode_for = self.persona.mode_for
                self.seed()
                if kind == "processing_error":
                    def broken(*a, **kw):
                        raise RuntimeError("test-only internal details")
                    bot.router.get_response = broken
                reply = bot.process_message("почему это произошло", user_id="web_user", chat_id="web_user")
                self.assertTrue(reply.startswith("Рыцарь"), reply)
                self.assertIn(("assistant", reply), saved)
                self.assertNotIn("test-only", reply)


if __name__ == "__main__":
    unittest.main()
