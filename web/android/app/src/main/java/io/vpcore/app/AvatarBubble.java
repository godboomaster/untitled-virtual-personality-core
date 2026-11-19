package io.vpcore.app;

import android.content.Context;
import android.graphics.Color;
import android.graphics.PixelFormat;
import android.graphics.Rect;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.text.Layout;
import android.text.SpannableString;
import android.text.Spanned;
import android.text.StaticLayout;
import android.text.TextPaint;
import android.text.style.ForegroundColorSpan;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.WindowManager;
import android.widget.TextView;

import androidx.core.content.ContextCompat;

import java.util.ArrayList;
import java.util.List;

/**
 * Облачко реплики у аватара поверх приложений (AvatarOverlayService): над
 * головой, а не помещается сверху — под ней. Отдельное маленькое окно: голова
 * остаётся своего размера, облачко — по тексту.
 *
 * Длинная реплика — частями, каждая влезает целиком (PortraitWidget.splitParts,
 * как у виджета и портрета на компьютере). Часть — слово за словом, пока
 * губы ходят; облачко сразу во весь размер части (ещё не сказанные слова
 * прозрачные, но занимают место) — не растёт и не прыгает, как на
 * компьютере. Темп — скорость текста из «Общих» (PortraitWidget.wordMsFor,
 * partMsFor: та же, что у виджета; «сразу» — вся часть одним разом).
 * Договорила — часть висит, пока её можно прочесть, потом следующая;
 * последняя — ещё LAST_HOLD_MS. Тап по облачку — чат с персоной в
 * приложении. Экран погас — пауза: часть начнётся заново, когда на неё
 * посмотрят.
 */
final class AvatarBubble {

    interface Host {
        /** Голова на экране: где (px экрана). */
        Rect headRect();

        /** Губы: говорит часть или замолчала. */
        void talking(boolean on);

        /** Тап по облачку. */
        void tapped();
    }

    private static final long LAST_HOLD_MS = 6_000;
    // Губы — не меньше (часть «сразу» или из одного слова)
    private static final long TALK_MIN_MS = 800;
    private static final int MAX_LINES = 5;
    private static final int MAX_CHARS = 1200;

    private final Context ctx;
    private final WindowManager wm;
    private final Host host;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final TextView view;
    private final WindowManager.LayoutParams lp;
    private boolean added;
    private boolean paused;
    private List<String> parts = new ArrayList<>();
    private int index = -1;
    // Текущая часть по словам и сколько их уже на виду
    private String[] words = new String[0];
    private int shownWords;
    private long talkUntil;

    private final Runnable nextPart = this::nextPart;
    private final Runnable nextWord = this::nextWord;
    private final Runnable quiet = this::quiet;
    private final Runnable gone = this::hide;

    AvatarBubble(Context ctx, WindowManager wm, Host host) {
        this.ctx = ctx;
        this.wm = wm;
        this.host = host;
        float dp = ctx.getResources().getDisplayMetrics().density;
        view = new TextView(ctx);
        view.setBackground(ContextCompat.getDrawable(ctx, R.drawable.widget_bubble_bg));
        view.setTextColor(ContextCompat.getColor(ctx, R.color.widget_text));
        view.setTextSize(TypedValue.COMPLEX_UNIT_SP, 14);
        view.setLineSpacing(0, 1.12f);
        int padH = Math.round(11 * dp);
        int padV = Math.round(8 * dp);
        view.setPadding(padH, padV, padH, padV);
        view.setMaxLines(MAX_LINES + 1);
        view.setOnClickListener(v -> {
            hide();
            this.host.tapped();
        });
        int type = Build.VERSION.SDK_INT >= 26
                ? WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
                : WindowManager.LayoutParams.TYPE_PHONE;
        lp = new WindowManager.LayoutParams(WindowManager.LayoutParams.WRAP_CONTENT,
                WindowManager.LayoutParams.WRAP_CONTENT, type,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE | WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
                PixelFormat.TRANSLUCENT);
        lp.gravity = Gravity.TOP | Gravity.START;
        lp.setTitle("VPC avatar bubble");
    }

    boolean showing() {
        return added;
    }

    /** Сказать реплику (прежняя, если ещё висит, — сменяется). */
    void say(String text) {
        String t = text == null ? "" : text.trim();
        if (t.isEmpty()) return;
        if (t.length() > MAX_CHARS) t = t.substring(0, MAX_CHARS) + "…";
        int width = maxWidth();
        TextPaint paint = view.getPaint();
        int inner = width - view.getPaddingLeft() - view.getPaddingRight();
        parts = PortraitWidget.splitParts(t, s -> lines(s, paint, inner) <= MAX_LINES);
        if (parts.isEmpty()) return;
        index = -1;
        view.setMaxWidth(width);
        ui.removeCallbacksAndMessages(null);
        nextPart();
    }

    /** Убрать облачко (тап, новая реплика пользователя, смена персоны). */
    void hide() {
        ui.removeCallbacksAndMessages(null);
        parts = new ArrayList<>();
        index = -1;
        host.talking(false);
        if (!added) return;
        try {
            wm.removeView(view);
        } catch (RuntimeException ignored) {
            // окна уже нет
        }
        added = false;
    }

    /** Экран погас: часть замирает и начнётся заново, когда на неё посмотрят. */
    void pause() {
        if (paused) return;
        paused = true;
        ui.removeCallbacksAndMessages(null);
        host.talking(false);
    }

    void resume() {
        if (!paused) return;
        paused = false;
        if (index >= 0 && index < parts.size()) {
            index--;
            nextPart();
        }
    }

    /** Голова сдвинулась или сменила размер — облачко за ней. */
    void follow() {
        if (!added) return;
        place();
        try {
            wm.updateViewLayout(view, lp);
        } catch (RuntimeException ignored) {
            // окна уже нет
        }
    }

    /** «Сквозь касания»: облачко тоже не ловит нажатия и чуть прозрачное. */
    void passThrough(boolean pass, float alpha) {
        int flags = pass
                ? lp.flags | WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE
                : lp.flags & ~WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE;
        float a = pass ? alpha : 1f;
        if (flags == lp.flags && a == lp.alpha) return;
        lp.flags = flags;
        lp.alpha = a;
        if (!added) return;
        try {
            wm.updateViewLayout(view, lp);
        } catch (RuntimeException ignored) {
            // окна уже нет
        }
    }

    private void nextPart() {
        index++;
        if (index >= parts.size()) {
            hide();
            return;
        }
        String part = parts.get(index);
        words = part.split(" ");
        // «Сразу» (0 слов/с) — вся часть одним разом
        shownWords = PortraitWidget.wordMsFor(ctx, words[0]) > 0 ? 1 : words.length;
        render();
        place();
        try {
            if (added) {
                wm.updateViewLayout(view, lp);
            } else {
                wm.addView(view, lp);
                added = true;
            }
        } catch (RuntimeException e) {
            parts = new ArrayList<>();
            return;
        }
        if (paused) return;
        host.talking(true);
        talkUntil = SystemClock.uptimeMillis() + TALK_MIN_MS;
        typeOn();
    }

    // Следующее слово — через столько, сколько говорится предыдущее
    private void typeOn() {
        if (shownWords < words.length) {
            ui.postDelayed(nextWord, Math.max(16, PortraitWidget.wordMsFor(ctx, words[shownWords])));
            return;
        }
        // Договорила: губы — до конца минимума, часть висит, пока её читают
        long now = SystemClock.uptimeMillis();
        if (now >= talkUntil) quiet();
        else ui.postDelayed(quiet, talkUntil - now);
        long hold = PortraitWidget.partMsFor(ctx, parts.get(index));
        if (index + 1 < parts.size()) ui.postDelayed(nextPart, hold);
        else ui.postDelayed(gone, hold + LAST_HOLD_MS);
    }

    private void nextWord() {
        if (paused) return;
        shownWords++;
        render();
        typeOn();
    }

    // Часть целиком; ещё не сказанные слова — прозрачные, но на своих местах
    private void render() {
        String part = parts.get(index);
        int end = 0;
        for (int i = 0; i < Math.min(shownWords, words.length); i++) {
            end += words[i].length() + (i > 0 ? 1 : 0);
        }
        end = Math.min(end, part.length());
        if (end >= part.length()) {
            view.setText(part);
            return;
        }
        SpannableString text = new SpannableString(part);
        text.setSpan(new ForegroundColorSpan(Color.TRANSPARENT), end, part.length(),
                Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
        view.setText(text);
    }

    private void quiet() {
        host.talking(false);
    }

    // Над головой по центру, в пределах экрана; сверху места нет — под головой
    private void place() {
        float dp = ctx.getResources().getDisplayMetrics().density;
        Rect head = host.headRect();
        Rect screen = screen();
        int w = maxWidth();
        view.measure(View.MeasureSpec.makeMeasureSpec(w, View.MeasureSpec.AT_MOST),
                View.MeasureSpec.makeMeasureSpec(screen.height(), View.MeasureSpec.AT_MOST));
        int bw = view.getMeasuredWidth();
        int bh = view.getMeasuredHeight();
        int margin = Math.round(8 * dp);
        int gap = Math.round(2 * dp);
        lp.x = Math.max(margin, Math.min(screen.width() - bw - margin, head.centerX() - bw / 2));
        int above = head.top + Math.round(head.height() * 0.06f) - bh - gap;
        lp.y = above >= Math.round(28 * dp) ? above : Math.min(screen.height() - bh - margin, head.bottom + gap);
    }

    private int maxWidth() {
        float dp = ctx.getResources().getDisplayMetrics().density;
        return Math.min(Math.round(260 * dp), screen().width() - Math.round(32 * dp));
    }

    private Rect screen() {
        if (Build.VERSION.SDK_INT >= 30) return wm.getCurrentWindowMetrics().getBounds();
        android.util.DisplayMetrics m = new android.util.DisplayMetrics();
        //noinspection deprecation
        wm.getDefaultDisplay().getRealMetrics(m);
        return new Rect(0, 0, m.widthPixels, m.heightPixels);
    }

    private static int lines(String s, TextPaint paint, int width) {
        if (width <= 0) return Integer.MAX_VALUE;
        StaticLayout l = StaticLayout.Builder.obtain(s, 0, s.length(), paint, width)
                .setAlignment(Layout.Alignment.ALIGN_NORMAL)
                .setLineSpacing(0, 1.12f)
                .build();
        return l.getLineCount();
    }
}
