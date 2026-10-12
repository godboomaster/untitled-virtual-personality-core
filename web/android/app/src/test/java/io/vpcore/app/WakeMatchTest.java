package io.vpcore.app;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.BeforeClass;
import org.junit.Test;

import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.List;

/**
 * WakeMatch — те же ответы, что app/voice/wake_match.py: общие примеры из
 * scripts/fixtures/wake_match_cases.json (их же гоняет scripts/test_wake_word.py).
 */
public class WakeMatchTest {

    private static JSONObject cases;
    private static List<WakeMatch.Phrase> phrases;

    @BeforeClass
    public static void load() throws Exception {
        // Путь передаёт build.gradle; без него — от папки модуля app
        String path = System.getProperty("wakeCases");
        File f = path != null ? new File(path) : new File("../../../scripts/fixtures/wake_match_cases.json");
        assertTrue("нет общих примеров: " + f.getAbsolutePath(), f.isFile());
        cases = new JSONObject(new String(Files.readAllBytes(f.toPath()), StandardCharsets.UTF_8));
        phrases = WakeMatch.wakePhrases(cases.getJSONArray("personas"));
    }

    @Test
    public void phoneticKeys() throws Exception {
        JSONArray keys = cases.getJSONArray("keys");
        assertTrue(keys.length() > 0);
        for (int i = 0; i < keys.length(); i++) {
            JSONArray pair = keys.getJSONArray(i);
            assertEquals("ключ «" + pair.getString(0) + "»", pair.getString(1), WakeMatch.phoneticKey(pair.getString(0)));
        }
    }

    @Test
    public void sharedCases() throws Exception {
        JSONArray list = cases.getJSONArray("cases");
        assertTrue(list.length() > 0);
        for (int i = 0; i < list.length(); i++) {
            JSONObject c = list.getJSONObject(i);
            String text = c.getString("text");
            WakeMatch.Hit hit = WakeMatch.match(text, phrases);
            if (c.isNull("persona")) {
                assertNull("«" + text + "» не должно будить, а разбудило " + (hit == null ? "" : hit.persona), hit);
                continue;
            }
            assertNotNull("«" + text + "» должно будить " + c.getString("persona"), hit);
            assertEquals("персона для «" + text + "»", c.getString("persona"), hit.persona);
            assertEquals("команда для «" + text + "»", c.getString("command"), hit.command);
        }
    }

    @Test
    public void phrasesFromPersonas() throws Exception {
        // Ассистент без trigger_words голосом не будится; «арродес» второй
        // персоны совпадает по звуку с первой — достаётся первой
        for (WakeMatch.Phrase ph : phrases) {
            assertTrue(!ph.persona.equals("assistant"));
            assertTrue(!ph.persona.equals("arrodes_master"));
        }
        assertEquals(List.of("Коннор", "Арродес", "Axel"), WakeMatch.displayNames(cases.getJSONArray("personas"), phrases));
    }

    @Test
    public void hotwordsAreCyrillic() throws Exception {
        String hw = WakeMatch.hotwords(phrases, 2.0f);
        assertEquals("коннор :2.0/жабка :2.0/арродес :2.0/миррор :2.0/зеркало :2.0/аксел :2.0", hw);
    }

    @Test
    public void speechText() throws Exception {
        assertEquals("Погода: ясно, +12. Подробнее", WakeMatch.forSpeech("**Погода:** ясно, +12 ☀️. Подробнее https://example.com/x"));
        assertEquals("Ссылка и список", WakeMatch.forSpeech("[Ссылка](https://a.b) и\n- список 🙂"));
    }
}
