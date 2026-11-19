package io.vpcore.app;

import android.annotation.SuppressLint;
import android.content.Context;
import android.graphics.PixelFormat;
import android.graphics.Rect;
import android.graphics.drawable.ColorDrawable;
import android.graphics.drawable.StateListDrawable;
import android.os.Build;
import android.text.TextUtils;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import androidx.core.content.ContextCompat;

import java.util.List;

/**
 * Меню аватара поверх приложений — долгое нажатие на голову
 * (AvatarOverlayService). Плоское, в палитре виджетов (светлая или тёмная —
 * по теме телефона), рядом с головой: сбоку, где больше места, а не влезает —
 * над ней или под ней.
 *
 * Пункты: «Написать», «Сменить персону ›» (список персон, у которых на
 * телефоне есть листы головы; «‹ Назад»), «Открыть приложение», «Сквозь
 * касания», «Убрать» (есть виджет этой персоны — «На виджет»: голова
 * уезжает на него). Тап мимо меню — закрыть (окно ловит ACTION_OUTSIDE:
 * координат чужого касания Android не даёт, но «мимо» сообщает).
 */
final class AvatarMenu {

    interface Host {
        Rect headRect();

        String personaName();

        /** Персоны с листами головы: {id, имя}; текущая — currentPersona. */
        List<String[]> personas();

        String currentPersona();

        boolean passThrough();

        void write();

        void switchTo(String id, String name);

        void openApp();

        void togglePass();

        void hideAvatar();

        /** Есть виджет этой персоны: «На виджет» вместо «Убрать». */
        boolean hasWidget();

        void toWidget();
    }

    private final Context ctx;
    private final WindowManager wm;
    private final Host host;
    private final float dp;
    private FrameLayout root;
    private WindowManager.LayoutParams lp;

    AvatarMenu(Context ctx, WindowManager wm, Host host) {
        this.ctx = ctx;
        this.wm = wm;
        this.host = host;
        this.dp = ctx.getResources().getDisplayMetrics().density;
    }

    boolean showing() {
        return root != null;
    }

    void show() {
        dismiss();
        root = new FrameLayout(ctx) {
            @SuppressLint("ClickableViewAccessibility")
            @Override
            public boolean dispatchTouchEvent(MotionEvent e) {
                if (e.getActionMasked() == MotionEvent.ACTION_OUTSIDE) {
                    dismiss();
                    return true;
                }
                return super.dispatchTouchEvent(e);
            }
        };
        root.setBackground(ContextCompat.getDrawable(ctx, R.drawable.widget_bubble_bg));
        int pad = Math.round(1 * dp);
        root.setPadding(pad, pad, pad, pad);
        int type = Build.VERSION.SDK_INT >= 26
                ? WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
                : WindowManager.LayoutParams.TYPE_PHONE;
        lp = new WindowManager.LayoutParams(Math.round(220 * dp), WindowManager.LayoutParams.WRAP_CONTENT, type,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                        | WindowManager.LayoutParams.FLAG_WATCH_OUTSIDE_TOUCH
                        | WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
                PixelFormat.TRANSLUCENT);
        lp.gravity = Gravity.TOP | Gravity.START;
        lp.setTitle("VPC avatar menu");
        mainPage();
        try {
            wm.addView(root, lp);
        } catch (RuntimeException e) {
            root = null;
        }
    }

    void dismiss() {
        if (root == null) return;
        try {
            wm.removeView(root);
        } catch (RuntimeException ignored) {
            // окна уже нет
        }
        root = null;
    }

    // ── Страницы ──────────────────────────────────────────────────────

    private void mainPage() {
        LinearLayout list = page();
        list.addView(title(host.personaName()));
        list.addView(item(ctx.getString(R.string.avatar_menu_write), v -> {
            dismiss();
            host.write();
        }));
        list.addView(item(ctx.getString(R.string.avatar_menu_persona) + "  ›", v -> personaPage()));
        list.addView(item(ctx.getString(R.string.avatar_menu_app), v -> {
            dismiss();
            host.openApp();
        }));
        list.addView(item(ctx.getString(host.passThrough() ? R.string.avatar_overlay_catch : R.string.avatar_overlay_pass), v -> {
            dismiss();
            host.togglePass();
        }));
        if (host.hasWidget()) {
            list.addView(item(ctx.getString(R.string.avatar_menu_widget), v -> {
                dismiss();
                host.toWidget();
            }));
        } else {
            list.addView(item(ctx.getString(R.string.avatar_overlay_hide), v -> {
                dismiss();
                host.hideAvatar();
            }));
        }
        setPage(list);
    }

    private void personaPage() {
        LinearLayout list = page();
        list.addView(item("‹  " + ctx.getString(R.string.avatar_menu_back), v -> mainPage()));
        String cur = host.currentPersona();
        List<String[]> all = host.personas();
        if (all.isEmpty()) {
            list.addView(title(ctx.getString(R.string.avatar_menu_no_personas)));
        }
        for (String[] p : all) {
            boolean mine = p[0].equals(cur);
            TextView row = item((mine ? "✓  " : "    ") + p[1], v -> {
                dismiss();
                if (!mine) host.switchTo(p[0], p[1]);
            });
            list.addView(row);
        }
        setPage(list);
    }

    // Страница — в прокрутке: персон бывает много; окно — не выше 60% экрана
    private void setPage(LinearLayout list) {
        if (root == null) return;
        root.removeAllViews();
        ScrollView scroll = new ScrollView(ctx);
        scroll.setVerticalScrollBarEnabled(false);
        scroll.addView(list);
        root.addView(scroll);
        place();
        if (root.isAttachedToWindow()) {
            try {
                wm.updateViewLayout(root, lp);
            } catch (RuntimeException ignored) {
                // окна уже нет
            }
        }
    }

    private LinearLayout page() {
        LinearLayout list = new LinearLayout(ctx);
        list.setOrientation(LinearLayout.VERTICAL);
        int v = Math.round(4 * dp);
        list.setPadding(0, v, 0, v);
        return list;
    }

    private TextView title(String text) {
        TextView t = new TextView(ctx);
        t.setText(text);
        t.setTextColor(ContextCompat.getColor(ctx, R.color.widget_text_dim));
        t.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        t.setSingleLine(true);
        t.setEllipsize(TextUtils.TruncateAt.END);
        int h = Math.round(16 * dp);
        t.setPadding(h, Math.round(6 * dp), h, Math.round(4 * dp));
        return t;
    }

    private TextView item(String text, View.OnClickListener click) {
        TextView t = new TextView(ctx);
        t.setText(text);
        t.setTextColor(ContextCompat.getColor(ctx, R.color.widget_text));
        t.setTextSize(TypedValue.COMPLEX_UNIT_SP, 15);
        t.setSingleLine(true);
        t.setEllipsize(TextUtils.TruncateAt.END);
        t.setGravity(Gravity.CENTER_VERTICAL);
        int h = Math.round(16 * dp);
        t.setPadding(h, 0, h, 0);
        t.setMinHeight(Math.round(46 * dp));
        t.setLayoutParams(new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                Math.round(46 * dp)));
        // Нажатие — плоская подсветка цветом рамки, без ряби
        StateListDrawable bg = new StateListDrawable();
        bg.addState(new int[]{android.R.attr.state_pressed},
                new ColorDrawable(ContextCompat.getColor(ctx, R.color.widget_border)));
        bg.addState(new int[]{}, new ColorDrawable(0));
        t.setBackground(bg);
        t.setOnClickListener(click);
        return t;
    }

    // ── Где ───────────────────────────────────────────────────────────

    // Сбоку от головы, где больше места; не влезает — над ней или под ней
    private void place() {
        Rect head = host.headRect();
        Rect screen = screen();
        int margin = Math.round(8 * dp);
        int gap = Math.round(6 * dp);
        int mw = lp.width;
        int maxH = Math.round(screen.height() * 0.6f);
        root.measure(View.MeasureSpec.makeMeasureSpec(mw, View.MeasureSpec.EXACTLY),
                View.MeasureSpec.makeMeasureSpec(maxH, View.MeasureSpec.AT_MOST));
        int mh = root.getMeasuredHeight();
        lp.height = mh;
        boolean leftSide = head.centerX() > screen.width() / 2;
        int sideX = leftSide ? head.left - gap - mw : head.right + gap;
        if (sideX >= margin && sideX + mw <= screen.width() - margin) {
            lp.x = sideX;
            lp.y = Math.max(margin, Math.min(screen.height() - mh - margin, head.top));
        } else {
            lp.x = Math.max(margin, Math.min(screen.width() - mw - margin, head.centerX() - mw / 2));
            int above = head.top - gap - mh;
            lp.y = above >= margin ? above : Math.min(screen.height() - mh - margin, head.bottom + gap);
        }
    }

    private Rect screen() {
        if (Build.VERSION.SDK_INT >= 30) return wm.getCurrentWindowMetrics().getBounds();
        android.util.DisplayMetrics m = new android.util.DisplayMetrics();
        //noinspection deprecation
        wm.getDefaultDisplay().getRealMetrics(m);
        return new Rect(0, 0, m.widthPixels, m.heightPixels);
    }
}
