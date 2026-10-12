package io.vpcore.app;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Имя персоны в распознанной речи (голосовая активация, «Коннор, …») —
 * тот же алгоритм, что app/voice/wake_match.py на ядре; общие примеры —
 * scripts/data/wake_match_cases.json, их гоняют оба теста (WakeMatchTest).
 *
 * Распознаватель пишет имена на слух: «коннер» вместо «коннор», «родис»
 * вместо «арродес». Поэтому сравнение — по грубому фонетическому ключу
 * (безударные гласные сведены, двойные — одна, оглушение на конце, латиница —
 * кириллицей) с допуском по взвешенному расстоянию правки: путаница гласных
 * стоит половину, согласных — целую. Имя ищется только в начале реплики.
 *
 * Без Android: класс проверяется обычным JUnit на JVM.
 */
final class WakeMatch {

    // Сколько слов может стоять перед именем: «эй, Коннор», «слушай, Коннор»
    static final int MAX_LEAD = 2;
    // Имя распознаватель может склеить или разбить: окно до стольких слов
    static final int MAX_WINDOW = 3;

    // Порядок важен: сначала длинные сочетания («sch» раньше «sh» и «s»)
    private static final String[][] LATIN = {
            {"sch", "ш"}, {"sh", "ш"}, {"ch", "ч"}, {"zh", "ж"}, {"kh", "х"}, {"ts", "ц"},
            {"yu", "ю"}, {"ya", "я"}, {"yo", "е"}, {"ck", "к"}, {"ph", "ф"}, {"th", "т"},
            {"ee", "и"}, {"oo", "у"}, {"x", "кс"}, {"q", "к"}, {"w", "в"}, {"y", "и"},
            {"j", "дж"}, {"h", "х"}, {"a", "а"}, {"b", "б"}, {"c", "к"}, {"d", "д"},
            {"e", "е"}, {"f", "ф"}, {"g", "г"}, {"i", "и"}, {"k", "к"}, {"l", "л"},
            {"m", "м"}, {"n", "н"}, {"o", "о"}, {"p", "п"}, {"r", "р"}, {"s", "с"},
            {"t", "т"}, {"u", "у"}, {"v", "в"}, {"z", "з"},
    };
    private static final String VOWEL_FROM = "ояеэыйю";
    private static final String VOWEL_TO = "ааииииу";
    private static final String DEVOICE_FROM = "бвгджз";
    private static final String DEVOICE_TO = "пфкташс";
    // Как re.IGNORECASE в Python: латиница, кириллица (и Ё), цифры
    private static final Pattern WORD = Pattern.compile("[a-zа-яё0-9]+",
            Pattern.CASE_INSENSITIVE | Pattern.UNICODE_CASE);
    // Что срезается с краёв команды после имени
    private static final String STRIP = " \t\n,.!?;:—-";

    private WakeMatch() {}

    /** Имя для пробуждения: чья персона, как написано, его ключ. */
    static final class Phrase {
        final String persona;
        final String phrase;
        final String key;

        Phrase(String persona, String phrase, String key) {
            this.persona = persona;
            this.phrase = phrase;
            this.key = key;
        }
    }

    /** Кто разбужен и что сказано после имени (может быть пустым). */
    static final class Hit {
        final String persona;
        final String phrase;
        final String command;
        final double distance;

        Hit(String persona, String phrase, String command, double distance) {
            this.persona = persona;
            this.phrase = phrase;
            this.command = command;
            this.distance = distance;
        }
    }

    static String translit(String s) {
        StringBuilder out = new StringBuilder(s.length() + 4);
        int i = 0;
        outer:
        while (i < s.length()) {
            for (String[] pair : LATIN) {
                if (s.startsWith(pair[0], i)) {
                    out.append(pair[1]);
                    i += pair[0].length();
                    continue outer;
                }
            }
            out.append(s.charAt(i));
            i++;
        }
        return out.toString();
    }

    /** Грубый «звук» слова или фразы (пробелы выкидываются). */
    static String phoneticKey(String text) {
        String s = translit(text.toLowerCase(Locale.ROOT).replace('ё', 'е'));
        StringBuilder b = new StringBuilder(s.length());
        for (int i = 0; i < s.length(); i++) {
            char ch = s.charAt(i);
            if (ch < 'а' || ch > 'я') continue;
            if (ch == 'ь' || ch == 'ъ') continue;
            int v = VOWEL_FROM.indexOf(ch);
            b.append(v >= 0 ? VOWEL_TO.charAt(v) : ch);
        }
        int n = b.length();
        if (n > 0) {
            int d = DEVOICE_FROM.indexOf(b.charAt(n - 1));
            if (d >= 0) b.setCharAt(n - 1, DEVOICE_TO.charAt(d));
        }
        // Двойные буквы — одна: «арродес» и «ародес» звучат одинаково
        StringBuilder out = new StringBuilder(b.length());
        for (int i = 0; i < b.length(); i++) {
            char ch = b.charAt(i);
            if (out.length() == 0 || out.charAt(out.length() - 1) != ch) out.append(ch);
        }
        return out.toString();
    }

    private static boolean vowel(char ch) {
        return ch == 'а' || ch == 'и' || ch == 'у';
    }

    private static double cost(char ch) {
        return vowel(ch) ? 0.5 : 1.0;
    }

    /** Расстояние правки: гласные (вставка, удаление, замена гласной на
     * гласную) — 0.5, всё остальное — 1. */
    static double keyDistance(String a, String b) {
        double[] prev = new double[b.length() + 1];
        for (int j = 0; j < b.length(); j++) prev[j + 1] = prev[j] + cost(b.charAt(j));
        for (int i = 0; i < a.length(); i++) {
            char ca = a.charAt(i);
            double[] cur = new double[b.length() + 1];
            cur[0] = prev[0] + cost(ca);
            for (int j = 1; j <= b.length(); j++) {
                char cb = b.charAt(j - 1);
                double sub;
                if (ca == cb) sub = 0.0;
                else if (vowel(ca) && vowel(cb)) sub = 0.5;
                else sub = 1.0;
                cur[j] = Math.min(prev[j - 1] + sub, Math.min(prev[j] + cost(ca), cur[j - 1] + cost(cb)));
            }
            prev = cur;
        }
        return prev[b.length()];
    }

    /** Допуск по длине ключа имени: короткое — почти точно. */
    static double tolerance(String key) {
        int n = key.length();
        if (n <= 3) return 0.0;
        if (n <= 5) return 0.5;
        if (n <= 7) return 1.0;
        return 1.5;
    }

    /**
     * Имена для пробуждения из ответа GET /api/personas: features.trigger_words.
     * Персоны без своих trigger_words голосом не будятся (имя вроде
     * «Ассистент» — обычное слово). Одинаковое по звуку имя у двух персон
     * достаётся первой.
     */
    static List<Phrase> wakePhrases(JSONArray personas) {
        List<Phrase> out = new ArrayList<>();
        if (personas == null) return out;
        Set<String> seen = new HashSet<>();
        for (int i = 0; i < personas.length(); i++) {
            JSONObject p = personas.optJSONObject(i);
            if (p == null) continue;
            Object rawId = p.opt("id");
            String pid = rawId == null || rawId == JSONObject.NULL ? "" : String.valueOf(rawId);
            if (pid.isEmpty()) continue;
            JSONObject features = p.optJSONObject("features");
            JSONArray words = features == null ? null : features.optJSONArray("trigger_words");
            if (words == null) continue;
            for (int j = 0; j < words.length(); j++) {
                Object w = words.opt(j);
                if (!(w instanceof String) || ((String) w).trim().isEmpty()) continue;
                String key = phoneticKey((String) w);
                if (key.length() < 3 || seen.contains(key)) continue;
                seen.add(key);
                out.add(new Phrase(pid, ((String) w).trim(), key));
            }
        }
        return out;
    }

    /** Имя в начале реплики → кто разбужен и что сказано после имени. */
    static Hit match(String text, List<Phrase> phrases) {
        if (phrases == null || phrases.isEmpty() || text == null || text.isEmpty()) return null;
        List<int[]> tokens = new ArrayList<>();
        Matcher m = WORD.matcher(text);
        while (m.find()) tokens.add(new int[] {m.start(), m.end()});
        double bestD = 0;
        int bestStart = 0;
        int bestEnd = 0;
        Phrase best = null;
        for (int start = 0; start < Math.min(MAX_LEAD + 1, tokens.size()); start++) {
            for (int size = 1; size <= MAX_WINDOW; size++) {
                int end = start + size;
                if (end > tokens.size()) break;
                StringBuilder joined = new StringBuilder();
                for (int t = start; t < end; t++) joined.append(text, tokens.get(t)[0], tokens.get(t)[1]);
                String key = phoneticKey(joined.toString());
                if (key.isEmpty()) continue;
                for (Phrase ph : phrases) {
                    // Окно заметно длиннее имени — это уже не имя
                    if (key.length() > ph.key.length() + 3) continue;
                    double d = keyDistance(key, ph.key);
                    if (d > tolerance(ph.key)) continue;
                    // Лучшее — ближе по звуку, затем раньше в реплике, затем длиннее
                    if (best == null || better(d, start, ph.key.length(), bestD, bestStart, best.key.length())) {
                        best = ph;
                        bestD = d;
                        bestStart = start;
                        bestEnd = end;
                    }
                }
            }
        }
        if (best == null) return null;
        String rest = text.substring(tokens.get(bestEnd - 1)[1]);
        return new Hit(best.persona, best.phrase, strip(rest), bestD);
    }

    // Кортеж (d, start, -len) строго меньше — как сравнение кортежей в Python
    private static boolean better(double d, int start, int len, double bd, int bstart, int blen) {
        if (d != bd) return d < bd;
        if (start != bstart) return start < bstart;
        return -len < -blen;
    }

    private static String strip(String s) {
        int a = 0;
        int b = s.length();
        while (a < b && STRIP.indexOf(s.charAt(a)) >= 0) a++;
        while (b > a && STRIP.indexOf(s.charAt(b - 1)) >= 0) b--;
        return s.substring(a, b);
    }

    /**
     * Горячие слова для распознавателя (createStream): «коннор :2.0/жабка :2.0».
     * Русская модель кодирует только кириллицу (BPE) — латиница переводится
     * той же транслитерацией, что у ключа; прочее выкидывается.
     */
    static String hotwords(List<Phrase> phrases, float score) {
        Set<String> words = new LinkedHashSet<>();
        for (Phrase ph : phrases) {
            String s = translit(ph.phrase.toLowerCase(Locale.ROOT).replace('ё', 'е'));
            StringBuilder b = new StringBuilder();
            for (int i = 0; i < s.length(); i++) {
                char ch = s.charAt(i);
                if (ch >= 'а' && ch <= 'я') b.append(ch);
                else if (b.length() > 0 && b.charAt(b.length() - 1) != ' ') b.append(' ');
            }
            String w = b.toString().trim();
            if (!w.isEmpty()) words.add(w);
        }
        StringBuilder out = new StringBuilder();
        String sc = String.format(Locale.ROOT, "%.1f", score);
        for (String w : words) {
            if (out.length() > 0) out.append('/');
            out.append(w).append(" :").append(sc);
        }
        return out.toString();
    }

    /** Имена для значка в шторке и настроек: по одному на будимую персону —
     * первое кириллическое из её trigger_words (как его скажут), с заглавной. */
    static List<String> displayNames(JSONArray personas, List<Phrase> phrases) {
        List<String> ids = new ArrayList<>();
        for (Phrase ph : phrases) if (!ids.contains(ph.persona)) ids.add(ph.persona);
        List<String> out = new ArrayList<>();
        for (String id : ids) {
            String name = null;
            JSONObject p = findPersona(personas, id);
            JSONObject features = p == null ? null : p.optJSONObject("features");
            JSONArray words = features == null ? null : features.optJSONArray("trigger_words");
            for (int j = 0; words != null && j < words.length(); j++) {
                Object w = words.opt(j);
                if (w instanceof String && ((String) w).matches("(?s).*[а-яА-ЯёЁ].*")) {
                    name = ((String) w).trim();
                    break;
                }
            }
            if (name == null || name.isEmpty()) {
                for (Phrase ph : phrases) {
                    if (ph.persona.equals(id)) {
                        name = ph.phrase;
                        break;
                    }
                }
            }
            if (name == null || name.isEmpty()) continue;
            out.add(name.substring(0, 1).toUpperCase(Locale.ROOT) + name.substring(1));
        }
        return Collections.unmodifiableList(out);
    }

    private static JSONObject findPersona(JSONArray personas, String id) {
        for (int i = 0; personas != null && i < personas.length(); i++) {
            JSONObject p = personas.optJSONObject(i);
            if (p != null && id.equals(String.valueOf(p.opt("id")))) return p;
        }
        return null;
    }

    // Ссылки, разметка Markdown и эмодзи — озвучке они не нужны
    private static final Pattern URL = Pattern.compile("https?://\\S+|www\\.\\S+");
    private static final Pattern MD_LINK = Pattern.compile("\\[([^\\]]*)\\]\\([^)]*\\)");
    private static final Pattern MD_MARKS = Pattern.compile("[*_`~#>|]+");
    private static final Pattern LIST_BULLET = Pattern.compile("(?m)^\\s*(?:[-•]|\\d+[.)])\\s+");
    private static final Pattern SPACES = Pattern.compile("\\s+");
    private static final Pattern SPACE_BEFORE_PUNCT = Pattern.compile("\\s+([.,!?;:])");

    /** Текст ответа персоны → что произнести. */
    static String forSpeech(String text) {
        if (text == null) return "";
        String s = MD_LINK.matcher(text).replaceAll("$1");
        s = URL.matcher(s).replaceAll(" ");
        s = LIST_BULLET.matcher(s).replaceAll("");
        s = MD_MARKS.matcher(s).replaceAll(" ");
        StringBuilder b = new StringBuilder(s.length());
        for (int i = 0; i < s.length(); ) {
            int cp = s.codePointAt(i);
            i += Character.charCount(cp);
            if (isEmoji(cp)) continue;
            b.appendCodePoint(cp);
        }
        String out = SPACES.matcher(b.toString()).replaceAll(" ");
        return SPACE_BEFORE_PUNCT.matcher(out).replaceAll("$1").trim();
    }

    private static boolean isEmoji(int cp) {
        if (cp >= 0x1F000 && cp <= 0x1FAFF) return true; // эмодзи, пиктограммы, флаги
        if (cp >= 0x2600 && cp <= 0x27BF) return true; // символы и дингбаты ☀ ✨
        if (cp >= 0x2B00 && cp <= 0x2BFF) return true; // стрелки и звёзды ⭐
        if (cp == 0x200D || (cp >= 0xFE00 && cp <= 0xFE0F)) return true; // склейка и варианты
        if (cp >= 0xE0000 && cp <= 0xE007F) return true; // теги флагов
        int type = Character.getType(cp);
        return type == Character.SURROGATE || type == Character.PRIVATE_USE;
    }
}
