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
 * приоткрыт или открыт.
 *
 * Из кадров складывается «сценка» для ViewFlipper: ожидание (idle) — смотрит
 * на вас, моргает, изредка отводит взгляд; речь (talk) — рот ходит слогами,
 * с паузами между словами, как в вебе (web/src/portrait/LivePortrait.tsx).
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

        private void blinkAt(List<Bitmap> seq, int i, Random rnd) {
            if (i <= 0 || i >= seq.size() || seq.get(i) != front) return;
            seq.set(i, blink);
            if (rnd.nextFloat() < 0.2f && i + 2 < seq.size() && seq.get(i + 2) == front) seq.set(i + 2, blink);
        }
    }

    static File dir(Context ctx, String persona) {
        String safe = persona.replaceAll("[^A-Za-z0-9_.-]", "_");
        return new File(new File(ctx.getFilesDir(), "portrait_widget"), safe);
    }

    /** Ответ /portrait → атласы на диск. Портрета нет (нет листа A) — false. */
    static boolean save(Context ctx, String persona, JSONObject portrait) throws Exception {
        clear(ctx, persona);
        JSONObject sheets = portrait.optJSONObject("sheets");
        if (sheets == null || sheets.optString("A", "").isEmpty()) return false;
        File d = dir(ctx, persona);
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
        File[] files = dir(ctx, persona).listFiles();
        if (files != null) {
            for (File f : files) {
                //noinspection ResultOfMethodCallIgnored
                f.delete();
            }
        }
        synchronized (CACHE) {
            CACHE.keySet().removeIf(k -> k.startsWith(persona + "@"));
        }
    }

    /** Кадры персоны размером не больше px (клетки атласа крупнее — уменьшаются); нет — null. */
    static Frames load(Context ctx, String persona, int px) {
        File d = dir(ctx, persona);
        File a = new File(d, "A.webp");
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
        int r0 = rows / 2;
        int c0 = cols / 2;
        Frames f = new Frames();
        f.front = cell(atlasA, rows, cols, r0, c0, px);
        for (int[] g : GLANCES) {
            int r = r0 + g[0];
            int c = c0 + g[1];
            if (r >= 0 && r < rows && c >= 0 && c < cols) f.glances.add(cell(atlasA, rows, cols, r, c, px));
        }
        f.blink = front(d, "D", rows, cols, r0, c0, px);
        f.half = front(d, "B", rows, cols, r0, c0, px);
        f.open = front(d, "C", rows, cols, r0, c0, px);
        // Голова на ряд ниже (как petted в вебе); нет листа D — с открытыми глазами
        int rDown = Math.min(rows - 1, r0 + 1);
        f.down = front(d, "D", rows, cols, rDown, c0, px);
        if (f.down == null) f.down = cell(atlasA, rows, cols, rDown, c0, px);
        synchronized (CACHE) {
            CACHE.put(key, f);
            CACHE_STAMP.put(key, a.lastModified());
        }
        return f;
    }

    // Клетка «прямо» другого листа (нет листа — null)
    private static Bitmap front(File d, String sheet, int rows, int cols, int r, int c, int px) {
        File file = new File(d, sheet + ".webp");
        if (!file.isFile()) return null;
        Bitmap atlas = BitmapFactory.decodeFile(file.getPath());
        return atlas == null ? null : cell(atlas, rows, cols, r, c, px);
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
