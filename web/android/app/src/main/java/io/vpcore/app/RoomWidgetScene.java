package io.vpcore.app;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Canvas;
import android.graphics.ColorMatrix;
import android.graphics.ColorMatrixColorFilter;
import android.graphics.Matrix;
import android.graphics.Paint;
import android.graphics.Rect;
import android.util.Base64;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.util.Iterator;

/**
 * Картинка комнаты для виджета (RoomWidget) из арта персоны (GET /room/art):
 * фон комнаты и спрайт на месте из точек пола, как рисует раздел «Комната»
 * (web/src/room/RoomScene.tsx, roomModel.spotPoint):
 *  - сцена в пропорциях веба (720×430), фон — object-fit: cover, точки пола —
 *    доли кадра;
 *  - спрайт шириной 120 px при высоте сцены 430 px, «ступни» (anchor) — в точке;
 *  - поза: свой спрайт позы, иначе базовый с тем же сдвигом, что в CSS
 *    (сидит — сжат по высоте, смотрит — наклон, спит — лёжа и темнее);
 *  - нет в комнате (away) — пустая комната.
 * Виджет показывает сцену целиком, без обрезки.
 *
 * Арт бывает в мегабайтах: на телефоне хранится уменьшенным (BG_MAX_W,
 * SPRITE_MAX_H) в files/room_widget/<персона>/. Готовая сцена — RGB_565 не
 * больше заданного числа пикселей (его выбирает RoomWidget).
 */
final class RoomWidgetScene {

    private static final int BG_MAX_W = 1280;
    private static final int SPRITE_MAX_H = 640;
    private static final int SCENE_W = 960;
    private static final int SCENE_H = 573; // 720:430
    private static final float SPRITE_W_FRAC = 120f / 430f;
    // Спящая — на матрасе: на 26 px из 430 выше точки кровати у пола
    private static final float SLEEP_LIFT_FRAC = 26f / 430f;

    private RoomWidgetScene() {
    }

    static File dir(Context ctx, String persona) {
        String safe = persona.replaceAll("[^A-Za-z0-9_.-]", "_");
        return new File(new File(ctx.getFilesDir(), "room_widget"), safe);
    }

    static boolean has(Context ctx, String persona) {
        return new File(dir(ctx, persona), "bg.png").isFile();
    }

    /** Арта больше нет — убрать сохранённый. */
    static void clear(Context ctx, String persona) {
        File[] files = dir(ctx, persona).listFiles();
        if (files == null) return;
        for (File f : files) {
            //noinspection ResultOfMethodCallIgnored
            f.delete();
        }
    }

    /** Ответ /room/art → уменьшенные фон и спрайты + точки (art.json). */
    static void save(Context ctx, String persona, JSONObject art) throws Exception {
        clear(ctx, persona);
        File d = dir(ctx, persona);
        //noinspection ResultOfMethodCallIgnored
        d.mkdirs();
        JSONObject meta = new JSONObject();
        JSONObject bg = art.optJSONObject("room_bg");
        Bitmap bgBmp = bg == null ? null : decodeDataUrl(bg.optString("dataUrl", ""), BG_MAX_W, 0);
        if (bgBmp != null) {
            write(bgBmp, new File(d, "bg.png"));
            bgBmp.recycle();
            JSONObject fp = bg.optJSONObject("floorPoints");
            meta.put("floorPoints", fp == null ? new JSONObject() : fp);
        }
        JSONObject anchors = new JSONObject();
        saveSprite(d, "sprite", art.optJSONObject("sprite"), anchors);
        JSONObject sprites = art.optJSONObject("sprites");
        if (sprites != null) {
            Iterator<String> it = sprites.keys();
            while (it.hasNext()) {
                String pose = it.next();
                if (pose.matches("[a-z_]{1,20}")) saveSprite(d, "sprite_" + pose, sprites.optJSONObject(pose), anchors);
            }
        }
        meta.put("anchors", anchors);
        try (OutputStream out = new FileOutputStream(new File(d, "art.json"))) {
            out.write(meta.toString().getBytes(StandardCharsets.UTF_8));
        }
    }

    private static void saveSprite(File d, String name, JSONObject sp, JSONObject anchors) throws Exception {
        if (sp == null) return;
        Bitmap b = decodeDataUrl(sp.optString("dataUrl", ""), 0, SPRITE_MAX_H);
        if (b == null) return;
        write(b, new File(d, name + ".png"));
        b.recycle();
        JSONObject a = sp.optJSONObject("anchor");
        anchors.put(name, new JSONObject()
                .put("x", a == null ? 0.5 : a.optDouble("x", 0.5))
                .put("y", a == null ? 1.0 : a.optDouble("y", 1.0)));
    }

    /** Комната целиком: фон, персона на месте spot в позе pose (itemZone — у
     * мест item:…); больше maxPixels — уменьшить. */
    static Bitmap render(Context ctx, String persona, String spot, String pose, String itemZone, int maxPixels) {
        File d = dir(ctx, persona);
        Bitmap bg = BitmapFactory.decodeFile(new File(d, "bg.png").getPath());
        if (bg == null) return null;
        JSONObject meta;
        try {
            meta = new JSONObject(readText(new File(d, "art.json")));
        } catch (Exception e) {
            meta = new JSONObject();
        }
        Bitmap out = Bitmap.createBitmap(SCENE_W, SCENE_H, Bitmap.Config.RGB_565);
        Canvas c = new Canvas(out);
        Paint paint = new Paint(Paint.FILTER_BITMAP_FLAG | Paint.ANTI_ALIAS_FLAG);
        c.drawBitmap(bg, coverSrc(bg.getWidth(), bg.getHeight(), SCENE_W, SCENE_H), new Rect(0, 0, SCENE_W, SCENE_H), paint);
        bg.recycle();
        if (!"away".equals(spot) && !"away".equals(pose)) drawSprite(c, d, meta, spot, pose, itemZone, paint);
        if ((long) SCENE_W * SCENE_H > maxPixels) {
            float s = (float) Math.sqrt((double) maxPixels / ((long) SCENE_W * SCENE_H));
            Bitmap small = Bitmap.createScaledBitmap(out, Math.round(SCENE_W * s), Math.round(SCENE_H * s), true);
            if (small != out) out.recycle();
            out = small;
        }
        return out;
    }

    private static void drawSprite(Canvas c, File d, JSONObject meta, String spot, String pose, String itemZone,
                                   Paint paint) {
        // Спрайт позы, иначе базовый со сдвигом позы
        String key = "sprite_" + pose;
        Bitmap sprite = BitmapFactory.decodeFile(new File(d, key + ".png").getPath());
        boolean own = sprite != null;
        if (!own) {
            key = "sprite";
            sprite = BitmapFactory.decodeFile(new File(d, "sprite.png").getPath());
        }
        if (sprite == null) return;
        JSONObject anchors = meta.optJSONObject("anchors");
        JSONObject a = anchors == null ? null : anchors.optJSONObject(key);
        float ax = a == null ? 0.5f : (float) a.optDouble("x", 0.5);
        float ay = a == null ? 1f : (float) a.optDouble("y", 1.0);

        float[] pt = point(meta.optJSONObject("floorPoints"), spot, pose, itemZone);
        float scale = SPRITE_W_FRAC * SCENE_H / sprite.getWidth();
        Matrix m = new Matrix();
        m.postTranslate(-ax * sprite.getWidth(), -ay * sprite.getHeight());
        m.postScale(scale, scale);
        if (!own) {
            switch (pose) {
                case "sit":
                case "read":
                case "write":
                    m.postScale(1f, 0.92f);
                    break;
                case "look":
                    m.postRotate(-3f);
                    break;
                case "sleep":
                    m.postRotate(-90f);
                    m.postScale(0.9f, 0.9f);
                    paint.setAlpha(217);
                    ColorMatrix dim = new ColorMatrix();
                    dim.setScale(0.85f, 0.85f, 0.85f, 1f);
                    paint.setColorFilter(new ColorMatrixColorFilter(dim));
                    break;
                default:
                    break;
            }
        }
        m.postTranslate(pt[0] * SCENE_W, pt[1] * SCENE_H);
        c.drawBitmap(sprite, m, paint);
        sprite.recycle();
    }

    // Точка места в долях кадра — как spotPoint() веба для сцены с фоном
    private static float[] point(JSONObject fps, String spot, String pose, String itemZone) {
        if (spot.startsWith("item:")) {
            // Предмет у зоны (без пользовательской метки): точка зоны, стены и полки — выше пола
            String zone = itemZone == null ? "desk" : itemZone;
            float[] base = fp(fps, zone);
            float lift = zone.equals("wall") || zone.equals("shelf") ? 0.3f
                    : zone.equals("window") ? 0.2f : zone.equals("desk") ? 0.08f : 0f;
            return new float[] {base[0], Math.min(0.95f, Math.max(0.05f, base[1] - lift))};
        }
        float[] p = fp(fps, spot);
        if ("bed".equals(spot) && "sleep".equals(pose) && fps != null && fps.has("bed")) {
            p[1] -= SLEEP_LIFT_FRAC;
        }
        return p;
    }

    private static float[] fp(JSONObject fps, String key) {
        JSONObject p = fps == null ? null : fps.optJSONObject(key);
        if (p == null && fps != null) p = fps.optJSONObject("desk");
        if (p == null) return new float[] {0.5f, 0.8f};
        return new float[] {(float) p.optDouble("x", 0.5), (float) p.optDouble("y", 0.8)};
    }

    // Часть исходника, которая при object-fit: cover попадает в кадр w×h
    private static Rect coverSrc(int sw, int sh, int w, int h) {
        float s = Math.max((float) w / sw, (float) h / sh);
        int cw = Math.round(w / s);
        int ch = Math.round(h / s);
        int x = (sw - cw) / 2;
        int y = (sh - ch) / 2;
        return new Rect(x, y, x + cw, y + ch);
    }

    /** data:image/…;base64,… → картинка не шире maxW и не выше maxH (0 — без предела). */
    static Bitmap decodeDataUrl(String url, int maxW, int maxH) {
        int comma = url.indexOf(',');
        if (!url.startsWith("data:image/") || comma < 0) return null;
        byte[] raw;
        try {
            raw = Base64.decode(url.substring(comma + 1), Base64.DEFAULT);
        } catch (IllegalArgumentException e) {
            return null;
        }
        BitmapFactory.Options o = new BitmapFactory.Options();
        o.inJustDecodeBounds = true;
        BitmapFactory.decodeByteArray(raw, 0, raw.length, o);
        if (o.outWidth <= 0 || o.outHeight <= 0) return null;
        int sample = 1;
        while ((maxW > 0 && o.outWidth / (sample * 2) >= maxW) || (maxH > 0 && o.outHeight / (sample * 2) >= maxH)) {
            sample *= 2;
        }
        BitmapFactory.Options d = new BitmapFactory.Options();
        d.inSampleSize = sample;
        Bitmap b = BitmapFactory.decodeByteArray(raw, 0, raw.length, d);
        if (b == null) return null;
        float s = 1f;
        if (maxW > 0 && b.getWidth() > maxW) s = Math.min(s, (float) maxW / b.getWidth());
        if (maxH > 0 && b.getHeight() > maxH) s = Math.min(s, (float) maxH / b.getHeight());
        if (s < 1f) {
            Bitmap scaled = Bitmap.createScaledBitmap(b, Math.max(1, Math.round(b.getWidth() * s)),
                    Math.max(1, Math.round(b.getHeight() * s)), true);
            if (scaled != b) b.recycle();
            b = scaled;
        }
        return b;
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

    private static void write(Bitmap b, File f) throws Exception {
        try (OutputStream out = new FileOutputStream(f)) {
            b.compress(Bitmap.CompressFormat.PNG, 100, out);
        }
    }
}
