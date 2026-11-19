package io.vpcore.app;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.BitmapRegionDecoder;
import android.graphics.Rect;
import android.os.Build;
import android.util.LruCache;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.InputStream;

/**
 * Клетки атласов головы для аватара поверх приложений (AvatarOverlayService).
 * Те же листы, что у виджета «Живой портрет» (PortraitWidgetFrames): свой
 * портрет персоны (files/portrait_widget/<персона>/), а нет его — голова её
 * фигуры из «Комнаты» (files/portrait_head/<персона>/). A — глаза открыты,
 * D — закрыты (моргание, гладят, спит), B и C — рот приоткрыт и открыт
 * (говорит).
 *
 * Память — главное. Лист целиком не раскрывается никогда (1280×1280 —
 * 6,5 МБ на лист): BitmapRegionDecoder вырезает из файла одну клетку, когда
 * она понадобилась, и кладёт в небольшой LRU. Голова обычно смотрит на вас
 * и моргает — в кэше живут две-три клетки; взгляд в сторону дорезает свои.
 * Клетка сразу масштабируется под окно: рисовать её потом — простое
 * копирование (окно рисуется процессором, см. AvatarOverlayService).
 * Резать — только в фоновом потоке (AvatarHeadView), get — откуда угодно.
 */
final class AvatarCells {

    static final int A = 0;
    static final int D = 1;
    // Рот (говорит): B — приоткрыт, C — открыт; нет листов — рот не двигается
    static final int B = 2;
    static final int C = 3;
    private static final String[] SHEETS = {"A", "D", "B", "C"};

    // Клеток в кэше — по байтам: окно 338 px — 450 КБ клетка, около 13 клеток
    private static final int CACHE_BYTES = 6 * 1024 * 1024;

    final int rows;
    final int cols;
    private final BitmapRegionDecoder[] dec = new BitmapRegionDecoder[SHEETS.length];
    private final int cw;
    private final int ch;
    private volatile int maxPx;
    private final LruCache<Integer, Bitmap> cache = new LruCache<Integer, Bitmap>(CACHE_BYTES) {
        @Override
        protected int sizeOf(Integer key, Bitmap b) {
            return b.getByteCount();
        }
    };

    private AvatarCells(int rows, int cols, int cw, int ch, int maxPx) {
        this.rows = rows;
        this.cols = cols;
        this.cw = cw;
        this.ch = ch;
        this.maxPx = maxPx;
    }

    /** Папка листов персоны: свой портрет главнее головы фигуры; нет ни того, ни другого — null. */
    static File dirOf(Context ctx, String persona) {
        String safe = persona.replaceAll("[^A-Za-z0-9_.-]", "_");
        for (String root : new String[]{"portrait_widget", "portrait_head"}) {
            File d = new File(new File(ctx.getFilesDir(), root), safe);
            if (new File(d, "A.webp").isFile()) return d;
        }
        return null;
    }

    /** Открыть листы персоны; клетки при вырезании масштабируются ровно до maxPx. Нет листов — null. */
    static AvatarCells open(Context ctx, String persona, int maxPx) {
        File d = dirOf(ctx, persona);
        if (d == null) return null;
        int rows = 5;
        int cols = 5;
        try {
            JSONObject grid = new JSONObject(readText(new File(d, "grid.json")));
            rows = Math.max(1, grid.optInt("rows", 5));
            cols = Math.max(1, grid.optInt("cols", 5));
        } catch (Exception ignored) {
            // нет сетки — обычная 5×5
        }
        BitmapRegionDecoder a = decoder(new File(d, "A.webp"));
        if (a == null) return null;
        AvatarCells cells = new AvatarCells(rows, cols, a.getWidth() / cols, a.getHeight() / rows, maxPx);
        cells.dec[A] = a;
        cells.dec[D] = decoder(new File(d, "D.webp"));
        cells.dec[B] = decoder(new File(d, "B.webp"));
        cells.dec[C] = decoder(new File(d, "C.webp"));
        return cells;
    }

    /** Новый размер окна: клетки режутся заново под него. Не изменился — false. */
    boolean resize(int px) {
        if (px == maxPx) return false;
        maxPx = px;
        cache.evictAll();
        return true;
    }

    boolean has(int sheet) {
        return dec[sheet] != null;
    }

    /** Вырезанная клетка из кэша (ещё не резали — null). */
    Bitmap get(int sheet, int r, int c) {
        return cache.get(key(sheet, r, c));
    }

    /** Вырезать клетку (только фоновый поток). Нет листа или вне сетки — null. */
    Bitmap load(int sheet, int r, int c) {
        if (r < 0 || r >= rows || c < 0 || c >= cols) return null;
        Bitmap have = cache.get(key(sheet, r, c));
        if (have != null) return have;
        BitmapRegionDecoder d = dec[sheet];
        if (d == null) return null;
        // Размер на время резки: окно сменило его посередине — в кэш не класть
        final int maxPx = this.maxPx;
        BitmapFactory.Options o = new BitmapFactory.Options();
        o.inSampleSize = 1;
        while (Math.max(cw, ch) / (o.inSampleSize * 2) >= maxPx) o.inSampleSize *= 2;
        Bitmap b;
        synchronized (d) {
            if (d.isRecycled()) return null;
            b = d.decodeRegion(new Rect(c * cw, r * ch, (c + 1) * cw, (r + 1) * ch), o);
        }
        if (b == null) return null;
        // Ровно под окно (и вверх тоже): кадр потом рисуется 1:1, без
        // масштабирования процессором на каждой отрисовке
        if (Math.max(b.getWidth(), b.getHeight()) != maxPx) {
            float s = (float) maxPx / Math.max(b.getWidth(), b.getHeight());
            b = Bitmap.createScaledBitmap(b, Math.max(1, Math.round(b.getWidth() * s)),
                    Math.max(1, Math.round(b.getHeight() * s)), true);
        }
        b.prepareToDraw();
        if (maxPx == this.maxPx) cache.put(key(sheet, r, c), b);
        return b;
    }

    void close() {
        cache.evictAll();
        for (BitmapRegionDecoder d : dec) {
            if (d == null) continue;
            synchronized (d) {
                d.recycle();
            }
        }
    }

    private int key(int sheet, int r, int c) {
        return (sheet * rows + r) * cols + c;
    }

    @SuppressWarnings("deprecation")
    private static BitmapRegionDecoder decoder(File f) {
        if (!f.isFile()) return null;
        try {
            return Build.VERSION.SDK_INT >= 31
                    ? BitmapRegionDecoder.newInstance(f.getPath())
                    : BitmapRegionDecoder.newInstance(f.getPath(), false);
        } catch (Exception e) {
            return null;
        }
    }

    private static String readText(File f) throws Exception {
        try (InputStream in = new FileInputStream(f)) {
            ByteArrayOutputStream buf = new ByteArrayOutputStream();
            byte[] b = new byte[4096];
            int n;
            while ((n = in.read(b)) > 0) buf.write(b, 0, n);
            return buf.toString("UTF-8");
        }
    }
}
