"""Редактор банка: API, подстановки, конфликты и сохранность ручных правок.

python3 -m scripts.test_phrase_editor — без сети и настоящих данных.
"""

import copy
import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.api import phrases_api as pa
from app.features import flavor_text as ft


class PhraseEditorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="phrase_editor_")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.data = {"system_prompt": "Ты — рыцарь.", "voice_examples": ["Клянусь честью!"]}
        self.modes = {"sancho": {"name": "Санчо", "system_prompt": "Санчо говорит кратко."}}
        for p in (patch.object(ft, "_bank_path", lambda context: root / context / "flavor_bank.json"),
                  patch.object(pa, "_persona", lambda persona: (self.data, self.modes))):
            p.start()
            self.addCleanup(p.stop)
        self.context = "api_test"
        self.profiles = ft._error_profiles(self.data["system_prompt"], self.modes, self.data["voice_examples"])
        errors = {mode: {"prompt_hash": profile["prompt_hash"],
                          "languages": copy.deepcopy(ft._ERROR_FALLBACKS)}
                  for mode, profile in self.profiles.items()}
        self.initial = {"errors": errors,
                        "kinds": {"open": {"ok": ["Открыл {host}."], "err": ["Не вышло: {detail}."]}},
                        "phrases": {"scenario_saved": ["Записал «{name}»: {steps} шагов."]},
                        "_meta": {"prompt_hash": ft._prompt_hash(self.data["system_prompt"]),
                                  "generated_at": time.time()}}
        ft._save_bank(self.context, self.initial)

    def group(self, section="errors", key="models_unavailable", mode="", language="ru", bucket=None):
        return next(g for g in pa.list_phrases("test")["groups"]
                    if g["section"] == section and g["key"] == key and g["mode"] == mode
                    and (section != "errors" or g["language"] == language) and g["bucket"] == bucket)

    def save(self, group, variants):
        return pa.save_phrases("test", group["id"], group["revision"], variants)["group"]

    def test_list_has_all_categories_modes_languages_but_not_internal_metadata(self):
        result = pa.list_phrases("test")
        self.assertEqual(len(result["groups"]), 15)
        self.assertEqual({g["section"] for g in result["groups"]}, {"errors", "kinds", "phrases"})
        self.assertEqual(self.group(mode="sancho")["mode_name"], "Санчо")
        self.assertEqual(self.group(language="en")["language"], "en")
        self.assertNotIn("system_prompt", json.dumps(result))
        self.assertNotIn("last_errors", result)

    def test_edit_is_used_immediately_and_other_modes_languages_are_unchanged(self):
        group = self.group(mode="sancho")
        updated = self.save(group, ["Пока не могу ответить. Напиши позже.", "Не выходит. Вернись позже."])
        self.assertTrue(updated["edited"])
        self.assertNotEqual(group["revision"], updated["revision"])
        persona = SimpleNamespace(**self.data, persona_data=self.data, modes=self.modes,
                                  mode_for=lambda **kw: "sancho")
        bot = SimpleNamespace(context=self.context, persona=persona)
        self.assertIn(ft.error_reply(bot, "models_unavailable", lang="ru"), updated["variants"])
        self.assertEqual(self.group(language="en")["variants"], self.initial["errors"][""]["languages"]["en"]["models_unavailable"])

    def test_conflicting_edit_is_rejected_without_losing_first_edit(self):
        group = self.group()
        values = ["Сейчас не могу ответить. Попробуй завтра.", "Пока ответа нет. Вернись позже."]
        self.save(group, values)
        with self.assertRaises(pa.PhraseError) as caught:
            self.save(group, group["variants"])
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.group()["variants"], values)

    def test_one_manually_chosen_variant_does_not_trigger_endless_generation(self):
        self.save(self.group(), ["Сейчас не могу ответить. Попробуй чуть позже."])
        self.assertTrue(ft._errors_ready(ft._load_bank(self.context), self.profiles))

    def test_placeholders_cannot_be_removed_added_or_formatted(self):
        group = self.group("kinds", "open", bucket="err")
        for bad in ("Не вышло.", "Не вышло: {unknown}.", "Не вышло: {detail!r}.", "Не вышло: {detail:>10}."):
            with self.subTest(bad=bad), self.assertRaises(pa.PhraseError):
                self.save(group, [bad])
        updated = self.save(group, ["Ох, не получилось: {detail}."])
        self.assertTrue(updated["edited"])
        self.assertEqual(ft._from_bank(self.context, "open", "err", None, "ошибка связи"),
                         "Ох, не получилось: ошибка связи.")

    def test_unknown_group_blank_duplicates_and_language_mismatch_are_rejected(self):
        with self.assertRaises(pa.PhraseError) as caught:
            pa.save_phrases("test", '["..","_meta"]', "x", ["bad"])
        self.assertEqual(caught.exception.status, 404)
        group = self.group()
        for bad in ([""], ["Одинаково.", "одинаково."], ["English only."], ["[ACT:sleep]"]):
            with self.subTest(bad=bad), self.assertRaises(pa.PhraseError):
                self.save(group, bad)

    def test_error_edits_survive_prompt_change_and_fresh_generation(self):
        group = self.group(mode="sancho")
        edited = self.save(group, ["Сейчас не могу. Попробуй позже.", "Пока не выходит. Вернись позже."])
        self.modes["sancho"]["system_prompt"] = "Новый характер Санчо."
        profiles = ft._error_profiles(self.data["system_prompt"], self.modes, self.data["voice_examples"])
        with patch.object(ft, "_generate_errors", return_value=copy.deepcopy(ft._ERROR_FALLBACKS)):
            ft._generate_safe(self.context, self.data["system_prompt"], profiles=profiles, include_cc=False)
        self.assertEqual(self.group(mode="sancho")["variants"], edited["variants"])
        self.assertFalse(self.group(mode="sancho")["stale"])

    def test_computer_and_service_edits_survive_rebuild_and_topup(self):
        action = self.group("kinds", "open", bucket="ok")
        service = self.group("phrases", "scenario_saved")
        self.save(action, ["Готово, страница {host} открыта."])
        self.save(service, ["Сценарий «{name}» записан, шагов: {steps}."])
        bank = ft._load_bank(self.context)
        bank["_meta"]["prompt_hash"] = "old"
        ft._save_bank(self.context, bank)
        with patch.object(ft, "_generate_kinds", return_value={"kinds": self.initial["kinds"]}), \
                patch.object(ft, "_generate_phrases", return_value={"phrases": self.initial["phrases"]}):
            ft._generate_safe(self.context, self.data["system_prompt"], profiles=self.profiles)
        self.assertEqual(self.group("kinds", "open", bucket="ok")["variants"], ["Готово, страница {host} открыта."])
        self.assertEqual(self.group("phrases", "scenario_saved")["variants"], ["Сценарий «{name}» записан, шагов: {steps}."])
        bank = ft._load_bank(self.context)
        bank["_meta"]["generated_at"] = 0
        ft._save_bank(self.context, bank)
        with patch.object(ft, "_generate_kinds", return_value={"kinds": {"open": {"ok": ["Новый {host}."], "err": []}}}):
            self.assertTrue(ft.maybe_topup_flavor_bank(self.context, self.data["system_prompt"], None, True))
        self.assertEqual(self.group("kinds", "open", bucket="ok")["variants"], ["Готово, страница {host} открыта."])

    def test_edit_while_generator_waits_for_model_is_preserved(self):
        bank = ft._load_bank(self.context)
        bank["errors"][""]["prompt_hash"] = "old"
        ft._save_bank(self.context, bank)
        values = ["Ответить пока не получается. Попробуй позже.", "Сейчас не могу ответить. Вернись позже."]

        def generate(*args):
            self.save(self.group(), values)
            return copy.deepcopy(ft._ERROR_FALLBACKS)

        with patch.object(ft, "_generate_errors", generate):
            ft._generate_safe(self.context, self.data["system_prompt"], profiles=self.profiles, include_cc=False)
        self.assertEqual(self.group()["variants"], values)

    def test_write_failure_is_reported_and_existing_file_is_unchanged(self):
        before = ft._bank_path(self.context).read_bytes()
        with patch.object(ft, "atomic_write_json", side_effect=OSError("test disk failure")):
            with self.assertRaises(OSError):
                self.save(self.group(), ["Пока не могу ответить.", "Позови чуть позже."])
        self.assertEqual(ft._bank_path(self.context).read_bytes(), before)

    def test_manual_edits_are_personal_data_when_reusing_persona_id(self):
        from app.api.settings_api import _holds_memory
        path = ft._bank_path(self.context).parent
        self.assertFalse(_holds_memory(path))
        self.save(self.group(), ["Пока не могу ответить.", "Позови чуть позже."])
        self.assertTrue(_holds_memory(path))

    def test_http_auth_schema_get_and_save(self):
        from fastapi.testclient import TestClient
        from app.api import server
        with patch.object(server, "_api_token", "test-token"), patch.object(server, "_api_token_local", True):
            client = TestClient(server.app, base_url="http://127.0.0.1:8000")
            self.assertEqual(client.get("/api/personas/test/phrases").status_code, 401)
            headers = {"Authorization": "Bearer test-token"}
            result = client.get("/api/personas/test/phrases", headers=headers)
            self.assertEqual(result.status_code, 200)
            group = result.json()["groups"][0]
            payload = {"group_id": group["id"], "revision": group["revision"], "variants": group["variants"]}
            saved = client.put("/api/personas/test/phrases", json=payload, headers=headers)
            self.assertEqual(saved.status_code, 200)
            self.assertTrue(saved.json()["group"]["edited"])
            payload["variants"] = [12]
            self.assertEqual(client.put("/api/personas/test/phrases", json=payload, headers=headers).status_code, 422)


if __name__ == "__main__":
    unittest.main()
