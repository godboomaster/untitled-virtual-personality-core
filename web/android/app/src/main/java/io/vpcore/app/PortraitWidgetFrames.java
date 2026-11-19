package io.vpcore.app;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.util.Base64;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;

/**
 * Кадры виджета «Живой портрет» (PortraitWidget). Атласы живого портрета
 * персоны (GET /api/personas/{p}/portrait, app/api/portrait_api.py) хранятся
 * как есть — files/portrait_widget/<персона>/<лист>.webp и сетка в grid.json,
 * — а из них режутся клетки: смотрит прямо, моргнула, чуть в сторону, рот
 * приоткрыт или открыт. Своих листов у персоны нет — те же клетки из листов
 * головы её фигуры из «Комнаты» (files/portrait_head/<персона>/): их
 * печатает веб (web/src/portrait/widgetHeads.tsx) и присылает плагином.
 *
 * Из кадров складывается «сценка» для ViewFlipper: ожидание (idle) — смотрит
 * на вас, моргает, изредка отводит взгляд; речь (talk) — рот ходит слогами,
 * с паузами между словами, как в вебе (web/src/portrait/LivePortrait.tsx);
 * думает над ответом (think) — взгляд вверх и в сторону, то в одну, то в
 * другую, иногда моргает; перед этим, если только что услышала реплику, —
 * кивок (голова на ряд вниз и обратно, глаза открыты, как в вебе); читает
 * сообщение пользователя (read) — в облачке слева вверху, строка за строкой.
 * Гладят или спит — один кадр down: глаза закрыты, голова на ряд ниже.
 * Одинаковые кадры сценки — один и тот же Bitmap: RemoteViews передаёт его
 * лаунчеру один раз.
 */
final class PortraitWidgetFrames {

    // Листы, нужные виджету: A — основной, D — моргание, B и C — рот
    private static final String[] SHEETS = {"A", "B", "C", "D"};
    // Куда отводит взгляд: на клетку от «прямо» — в стороны и вверх-вбок
    private static final int[][] GLANCES = {{0, -1}, {0, 1}, {-1, -1}, {-1, 1}};

    // Нарезанные кадры: персона@размер → кадры и время файла атласа A
    private static final Map<String, Frames> CACHE = new HashMap<>();
    private static final Map<String, Long> CACHE_STAMP = new HashMap<>();

    private PortraitWidgetFrames() {
    }

    /** Кадры портрета одной персоны. */
    static final class Frames {
        Bitmap front;      // смотрит прямо
        Bitmap blink;      // моргнула (нет листа D — null)
        final List<Bitmap> glances = new ArrayList<>(); // чуть в сторону
        Bitmap half;       // рот приоткрыт (нет листов рта — null)
        Bitmap open;       // рот открыт
        Bitmap down;       // глаза закрыты, голова опущена: гладят, спит
        Bitmap nod;        // кивок: голова на ряд ниже, глаза открыты
        // Думает: вверх-влево, вверх-вправо (и их моргание; нет листа D —
        // null), между ними — вверх прямо. Сетка меньше 3×3 — пусто
        final List<Bitmap> thinkGaze = new ArrayList<>();
        final List<Bitmap> thinkBlink = new ArrayList<>();
        Bitmap thinkUp;
        // Читает облачко пользователя слева вверху: взгляд вверх-влево на
        // дальний край строки и на ближний — для ряда первых строк и ряда
        // последних (голова чуть ниже); моргание — на ближнем крае (нет листа
        // D — null). Сетка в одну колонку — пусто
        final List<Bitmap> readFar = new ArrayList<>();
        final List<Bitmap> readNear = new ArrayList<>();
        Bitmap readBlink;

        boolean canTalk() {
            return half != null || open != null;
        }

        /** Ожидание: n кадров по кругу — прямо, моргания, один взгляд в сторону. */
        List<Bitmap> idle(int n, Random rnd) {
            List<Bitmap> seq = new ArrayList<>(n);
            for (int i = 0; i < n; i++) seq.add(front);
            // Взгляд в сторону: 5–8 кадров во второй половине сценки
            int glanceEnd = -1;
            if (!glances.isEmpty() && n >= 24) {
                int len = 5 + rnd.nextInt(4);
                int start = n / 2 + rnd.nextInt(Math.max(1, n / 2 - len - 2));
                glanceEnd = start + len;
                Bitmap g = glances.get(rnd.nextInt(glances.size()));
                for (int i = start; i < glanceEnd; i++) seq.set(i, g);
            }
            // Моргания: одно в первой половине, одно после взгляда (иногда двойное)
            if (blink != null) {
                blinkAt(seq, 4 + rnd.nextInt(Math.max(1, n / 2 - 8)), rnd);
                int after = glanceEnd > 0 ? glanceEnd + 2 : n * 3 / 4;
                if (after < n - 1) blinkAt(seq, after + rnd.nextInt(Math.max(1, n - 1 - after)), rnd);
            }
            return seq;
        }

        /** Речь: n кадров — слоги «открыт → прикрыт», паузы между словами. */
        List<Bitmap> talk(int n, Random rnd) {
            Bitmap wide = open != null ? open : half;
            Bitmap mid = half != null ? half : open;
            List<Bitmap> seq = new ArrayList<>(n + 2);
            while (seq.size() < n) {
                if (rnd.nextFloat() < 0.15f) {
                    int pause = 1 + rnd.nextInt(2);
                    for (int i = 0; i < pause; i++) seq.add(front);
                    continue;
                }
                seq.add(rnd.nextFloat() < 0.55f ? wide : mid);
                seq.add(rnd.nextFloat() < 0.3f ? mid : front);
            }
            return seq.subList(0, n);
        }

        /**
         * Думает: n кадров — взгляд вверх и в сторону по 1,4–2,8 с (как в
         * вебе), чаще в другую сторону, переход — через «вверх прямо»;
         * моргает раз-другой. withNod — сначала кивок (кадры сверх n): сценка
         * идёт по кругу, поэтому кивок — только в первой, следующую
         * PortraitWidget присылает без него.
         */
        List<Bitmap> think(int n, Random rnd, boolean withNod) {
            List<Bitmap> seq = new ArrayList<>(n + 3);
            if (withNod) {
                seq.add(nod);
                seq.add(nod);
                seq.add(front);
            }
            int start = seq.size();
            List<Bitmap> sides = thinkGaze.isEmpty() ? glances : thinkGaze;
            if (sides.isEmpty()) {
                seq.addAll(idle(n, rnd));
                return seq;
            }
            int side = rnd.nextInt(sides.size());
            while (seq.size() - start < n) {
                int len = 9 + rnd.nextInt(10);
                for (int i = 0; i < len && seq.size() - start < n; i++) seq.add(sides.get(side));
                if (sides.size() > 1 && rnd.nextFloat() < 0.7f) {
                    int other = (side + 1 + rnd.nextInt(sides.size() - 1)) % sides.size();
                    if (thinkUp != null && sides == thinkGaze && seq.size() - start < n) seq.add(thinkUp);
                    side = other;
                }
            }
            // Моргание — кадр того же взгляда с закрытыми глазами
            if (sides == thinkGaze && !thinkBlink.isEmpty()) {
                int blinks = 1 + (rnd.nextFloat() < 0.4f ? 1 : 0);
                for (int b = 0; b < blinks; b++) {
                    int i = start + 2 + rnd.nextInt(Math.max(1, n - 4));
                    int k = i < seq.size() ? thinkGaze.indexOf(seq.get(i)) : -1;
                    if (k >= 0 && k < thinkBlink.size() && thinkBlink.get(k) != null) seq.set(i, thinkBlink.get(k));
                }
            }
            return seq;
        }

        /**
         * Читает облачко пользователя (слева вверху, PortraitWidget): n кадров —
         * поворот к облачку, потом строки по ~0,9 с: дальний край, ближний, и
         * снова дальний — следующая строка; последняя треть строк — голова на
         * ряд ниже; длинное — раз моргает между строками. withNod — сначала
         * кивок (кадры сверх n), как у think. Сетка не позволяет — думает.
         */
        List<Bitmap> read(int n, Random rnd, boolean withNod) {
            if (readFar.isEmpty()) return think(n, rnd, withNod);
            List<Bitmap> seq = new ArrayList<>(n + 3);
            if (withNod) {
                seq.add(nod);
                seq.add(nod);
                seq.add(front);
            }
            int start = seq.size();
            // Поворот: сначала ближний край, оттуда — к началу строки
            seq.add(readNear.get(0));
            int lines = Math.max(1, Math.round(n / 6f));
            List<Integer> breaks = new ArrayList<>();
            for (int line = 0; seq.size() - start < n; line++) {
                int row = line * 3 >= lines * 2 ? 1 : 0;
                int far = 2 + rnd.nextInt(2);
                int near = 3 + rnd.nextInt(2);
                for (int i = 0; i < far && seq.size() - start < n; i++) seq.add(readFar.get(row));
                for (int i = 0; i < near && seq.size() - start < n; i++) seq.add(readNear.get(row));
                breaks.add(seq.size() - 1);
            }
            if (readBlink != null && n >= 16 && breaks.size() > 2) {
                int i = breaks.get(breaks.size() / 2);
                if (seq.get(i) == readNear.get(0)) seq.set(i, readBlink);
            }
            return seq;
        }

        private void blinkAt(List<Bitmap> seq, int i, Random rnd) {
            if (i <= 0 || i >= seq.size() || seq.get(i) != front) return;
            seq.set(i, blink);
            if (rnd.nextFloat() < 0.2f && i + 2 < seq.size() && seq.get(i + 2) == front) seq.set(i + 2, blink);
        }
    }

    static File dir(Context ctx, String persona) {
        return dirIn(ctx, "portrait_widget", persona);
    }

    // Листы головы фигуры — у персоны без своего портрета
    private static File headDir(Context ctx, String persona) {
        return dirIn(ctx, "portrait_head", persona);
    }

    private static File dirIn(Context ctx, String root, String persona) {
        String safe = persona.replaceAll("[^A-Za-z0-9_.-]", "_");
        return new File(new File(ctx.getFilesDir(), root), safe);
    }

    /** Ответ /portrait → атласы на диск. Портрета нет (нет листа A) — false. */
    static boolean save(Context ctx, String persona, JSONObject portrait) throws Exception {
        clear(ctx, persona);
        return writeSheets(dir(ctx, persona), portrait);
    }

    /** Листы головы фигуры (тот же вид, что ответ /portrait) — на диск. */
    static void saveHead(Context ctx, String persona, JSONObject portrait) throws Exception {
        File d = headDir(ctx, persona);
        deleteFiles(d);
        dropCache(persona);
        writeSheets(d, portrait);
    }

    // Листы data-URL → <лист>.webp и сетка в grid.json; нет листа A — false
    private static boolean writeSheets(File d, JSONObject portrait) throws Exception {
        JSONObject sheets = portrait.optJSONObject("sheets");
        if (sheets == null || sheets.optString("A", "").isEmpty()) return false;
        //noinspection ResultOfMethodCallIgnored
        d.mkdirs();
        for (String key : SHEETS) {
            String url = sheets.optString(key, "");
            int comma = url.indexOf(',');
            if (!url.startsWith("data:image/") || comma < 0) continue;
            write(new File(d, key + ".webp"), Base64.decode(url.substring(comma + 1), Base64.DEFAULT));
        }
        JSONObject grid = new JSONObject()
                .put("rows", portrait.optInt("rows", 5))
                .put("cols", portrait.optInt("cols", 5));
        write(new File(d, "grid.json"), grid.toString().getBytes(StandardCharsets.UTF_8));
        return new File(d, "A.webp").isFile();
    }

    /** Портрета больше нет — убрать сохранённый. */
    static void clear(Context ctx, String persona) {
        deleteFiles(dir(ctx, persona));
        dropCache(persona);
    }

    private static void deleteFiles(File d) {
        File[] files = d.listFiles();
        if (files != null) {
            for (File f : files) {
                //noinspection ResultOfMethodCallIgnored
                f.delete();
            }
        }
    }

    private static void dropCache(String persona) {
        synchronized (CACHE) {
            CACHE.keySet().removeIf(k -> k.startsWith(persona + "@"));
        }
    }

    /**
     * Кадры персоны размером не больше px (клетки атласа крупнее — уменьшаются):
     * из её портрета, а нет его — из листов головы; нет и их — null.
     */
    static Frames load(Context ctx, String persona, int px) {
        File d = dir(ctx, persona);
        File a = new File(d, "A.webp");
        if (!a.isFile()) {
            d = headDir(ctx, persona);
            a = new File(d, "A.webp");
        }
        if (!a.isFile()) return null;
        String key = persona + "@" + px;
        synchronized (CACHE) {
            Long stamp = CACHE_STAMP.get(key);
            if (stamp != null && stamp == a.lastModified() && CACHE.containsKey(key)) return CACHE.get(key);
        }
        int rows = 5;
        int cols = 5;
        try {
            JSONObject grid = new JSONObject(readText(new File(d, "grid.json")));
            rows = Math.max(1, grid.optInt("rows", 5));
            cols = Math.max(1, grid.optInt("cols", 5));
        } catch (Exception ignored) {
            // нет сетки — обычная 5×5
        }
        Bitmap atlasA = BitmapFactory.decodeFile(a.getPath());
        if (atlasA == null) return null;
        // Каждый лист — один раз: клеток из D нужно несколько
        Bitmap atlasD = decode(d, "D");
        int r0 = rows / 2;
        int c0 = cols / 2;
        Frames f = new Frames();
        f.front = cell(atlasA, rows, cols, r0, c0, px);
        // Клетки A — по одной на ракурс: одинаковые кадры — один Bitmap
        Map<Integer, Bitmap> cellsA = new HashMap<>();
        cellsA.put(r0 * cols + c0, f.front);
        for (int[] g : GLANCES) {
            Bitmap b = cellOf(atlasA, cellsA, rows, cols, r0 + g[0], c0 + g[1], px);
            if (b != null) f.glances.add(b);
        }
        f.blink = atlasD == null ? null : cell(atlasD, rows, cols, r0, c0, px);
        f.half = front(d, "B", rows, cols, r0, c0, px);
        f.open = front(d, "C", rows, cols, r0, c0, px);
        // Голова на ряд ниже (как petted в вебе); нет листа D — с открытыми глазами
        int rDown = Math.min(rows - 1, r0 + 1);
        f.nod = cellOf(atlasA, cellsA, rows, cols, rDown, c0, px);
        f.down = atlasD == null ? f.nod : cell(atlasD, rows, cols, rDown, c0, px);
        // Думает: на ряд выше и на клетку в сторону (thinking в вебе: −0,55…0,55 × −0,5)
        if (r0 > 0 && c0 > 0 && c0 < cols - 1) {
            for (int dc : new int[]{-1, 1}) {
                f.thinkGaze.add(cellOf(atlasA, cellsA, rows, cols, r0 - 1, c0 + dc, px));
                f.thinkBlink.add(atlasD == null ? null : cell(atlasD, rows, cols, r0 - 1, c0 + dc, px));
            }
            f.thinkUp = cellOf(atlasA, cellsA, rows, cols, r0 - 1, c0, px);
        }
        // Читает облачко слева вверху: на ряд выше (последние строки — на
        // уровне глаз: облачко над головой, строки идут вниз) и влево — от
        // дальнего края строки (две клетки от «прямо») к ближнему (одна);
        // сетка узкая — ближний край прямо. Крайние ряды не берём: на них
        // лица почти не видно
        if (cols > 1) {
            int far = Math.max(0, c0 - 2);
            int near = c0 - 1 > far ? c0 - 1 : Math.min(cols - 1, far + 1);
            int rFirst = Math.max(0, r0 - 1);
            int rLast = r0;
            for (int r : new int[]{rFirst, rLast}) {
                f.readFar.add(cellOf(atlasA, cellsA, rows, cols, r, far, px));
                f.readNear.add(cellOf(atlasA, cellsA, rows, cols, r, near, px));
            }
            f.readBlink = atlasD == null ? null : cell(atlasD, rows, cols, rFirst, near, px);
        }
        synchronized (CACHE) {
            CACHE.put(key, f);
            CACHE_STAMP.put(key, a.lastModified());
        }
        return f;
    }

    // Клетка «прямо» другого листа (нет листа — null)
    private static Bitmap front(File d, String sheet, int rows, int cols, int r, int c, int px) {
        Bitmap atlas = decode(d, sheet);
        return atlas == null ? null : cell(atlas, rows, cols, r, c, px);
    }

    // Лист с диска (нет — null)
    private static Bitmap decode(File d, String sheet) {
        File file = new File(d, sheet + ".webp");
        return file.isFile() ? BitmapFactory.decodeFile(file.getPath()) : null;
    }

    // Клетка листа A: уже вырезанная — тот же Bitmap; вне сетки — null
    private static Bitmap cellOf(Bitmap atlas, Map<Integer, Bitmap> cells, int rows, int cols, int r, int c, int px) {
        if (r < 0 || r >= rows || c < 0 || c >= cols) return null;
        Bitmap b = cells.get(r * cols + c);
        if (b == null) {
            b = cell(atlas, rows, cols, r, c, px);
            cells.put(r * cols + c, b);
        }
        return b;
    }

    private static Bitmap cell(Bitmap atlas, int rows, int cols, int r, int c, int px) {
        int cw = atlas.getWidth() / cols;
        int ch = atlas.getHeight() / rows;
        Bitmap b = Bitmap.createBitmap(atlas, c * cw, r * ch, cw, ch);
        if (px > 0 && Math.max(cw, ch) > px) {
            float s = (float) px / Math.max(cw, ch);
            b = Bitmap.createScaledBitmap(b, Math.max(1, Math.round(cw * s)), Math.max(1, Math.round(ch * s)), true);
        }
        return b;
    }

    // Запись через временный файл: оборванная запись не оставит половину атласа
    private static void write(File f, byte[] data) throws Exception {
        File tmp = new File(f.getPath() + ".tmp");
        try (FileOutputStream out = new FileOutputStream(tmp)) {
            out.write(data);
        }
        if (!tmp.renameTo(f)) throw new IllegalStateException("не записан " + f.getName());
    }

    private static String readText(File f) throws Exception {
        try (InputStream in = new FileInputStream(f)) {
            ByteArrayOutputStream buf = new ByteArrayOutputStream();
            byte[] b = new byte[8192];
            int n;
            while ((n = in.read(b)) > 0) buf.write(b, 0, n);
            return buf.toString("UTF-8");
        }
    }
}
