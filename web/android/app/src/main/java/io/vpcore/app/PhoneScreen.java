package io.vpcore.app;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.app.KeyguardManager;
import android.content.Context;
import android.content.pm.PackageManager;
import android.graphics.Path;
import android.graphics.Rect;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.util.DisplayMetrics;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

/* Снимок экрана и действия службы спец. возможностей (PhoneControlService).

   Снимок — пронумерованные видимые элементы окон на экране (приложение
   сверху, системное окно поверх него — шторка, диалог разрешения): подпись
   (текст, описание или подсказка поля), вид, флаги и место. Подпись строки
   списка без своего текста — тексты внутри неё («Кара · привет»). У текста
   внутри нажимаемого — номер этого нажимаемого (p): «нажми «Кара»» жмёт
   строку, а не надпись. Текст полей паролей не читается (Android его и так
   прячет).

   Действие ссылается на номер из снимка (snap + i). Экран за это время
   сменился — тот же элемент ищется в свежем снимке по подписи и месту;
   не нашёлся — gone, ядро скажет честно. Нажатие — ACTION_CLICK самого
   элемента или ближайшего нажимаемого предка, не вышло — касание жестом в
   середину элемента. */
final class PhoneScreen {

    /** Отказ с кодом — ядро переводит его в реплику (phone_control.py). */
    static final class Failure extends Exception {
        final String code;

        Failure(String code, String message) {
            super(message);
            this.code = code;
        }
    }

    static final int MAX_ELEMENTS = 200;
    private static final int MAX_LABEL = 80;
    private static final int MAX_DEPTH = 60;
    // Свой элемент в свежем снимке: та же подпись и середина не дальше
    private static final int SAME_PLACE_DP = 48;
    // Безымянных строк, которым за снимок вызываем подпись (probeLabel)
    private static final int MAX_PROBES = 40;

    private static final Object lock = new Object();
    private static int seq;
    private static int snapId;
    private static final List<AccessibilityNodeInfo> nodes = new ArrayList<>();
    private static final List<JSONObject> elems = new ArrayList<>();

    private PhoneScreen() {}

    // ── Снимок ────────────────────────────────────────────────────────

    static JSONObject snapshot(PhoneControlService s) throws Failure {
        synchronized (lock) {
            try {
                return snapshotLocked(s);
            } catch (JSONException e) {
                throw new Failure("failed", "снимок: " + e.getMessage());
            }
        }
    }

    private static JSONObject snapshotLocked(PhoneControlService s) throws JSONException, Failure {
        nodes.clear();
        elems.clear();
        snapId = ++seq;
        JSONObject out = new JSONObject();
        out.put("snap", snapId);
        PowerManager pm = (PowerManager) s.getSystemService(Context.POWER_SERVICE);
        if (pm != null && !pm.isInteractive()) {
            out.put("screen_off", true);
            out.put("elements", new JSONArray());
            return out;
        }
        KeyguardManager km = (KeyguardManager) s.getSystemService(Context.KEYGUARD_SERVICE);
        if (km != null && km.isKeyguardLocked()) out.put("locked", true);
        DisplayMetrics dm = s.getResources().getDisplayMetrics();
        int h = dm.heightPixels;
        out.put("w", dm.widthPixels);
        out.put("h", h);

        List<AccessibilityNodeInfo> roots = new ArrayList<>();
        String pkg = null;
        CharSequence title = null;
        List<AccessibilityWindowInfo> ws = new ArrayList<>(s.getWindows());
        // Верхнее окно — первым: его элементы и номера идут раньше
        Collections.sort(ws, (a, b) -> Integer.compare(b.getLayer(), a.getLayer()));
        for (AccessibilityWindowInfo w : ws) {
            int type = w.getType();
            if (type == AccessibilityWindowInfo.TYPE_INPUT_METHOD
                || type == AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY
                || type == AccessibilityWindowInfo.TYPE_SPLIT_SCREEN_DIVIDER) continue;
            Rect r = new Rect();
            w.getBoundsInScreen(r);
            // Строка состояния и панель навигации — не экран; шторка и
            // системный диалог — да (они высокие)
            if (type == AccessibilityWindowInfo.TYPE_SYSTEM && r.height() < h / 3) continue;
            AccessibilityNodeInfo root = w.getRoot();
            if (root == null) continue;
            if (pkg == null && type == AccessibilityWindowInfo.TYPE_APPLICATION) {
                pkg = str(root.getPackageName());
                if (Build.VERSION.SDK_INT >= 24) title = w.getTitle();
            }
            roots.add(root);
        }
        if (roots.isEmpty()) {
            AccessibilityNodeInfo root = s.getRootInActiveWindow();
            if (root == null) throw new Failure("no_window", "окно на экране не читается");
            roots.add(root);
        }
        // Окна приложения нет (шторка опущена до конца) — пакет верхнего окна
        if (pkg == null || pkg.isEmpty()) pkg = str(roots.get(0).getPackageName());
        String t = clean(title);
        if (!pkg.isEmpty()) {
            out.put("pkg", pkg);
            String label = appLabel(s, pkg);
            // Пакет не виден (видимость пакетов Android 11+) — имя из заголовка окна
            out.put("app", label.equals(pkg) && !t.isEmpty() ? t : label);
            if (pkg.equals(homePackage(s))) out.put("home", true);
        }
        if (!t.isEmpty()) out.put("title", t);

        boolean more = false;
        // Большие списки: страницы вкладок лежат одна на другой (Telegram:
        // список «All» поверх страницы другой папки), и Android считает
        // видимыми обе. Из списков на одном и том же месте виден нарисованный
        // позже — более ранний и всё в нём выкидываем, иначе «нажми X»
        // попадёт по строке скрытой страницы, а касание — в чужой чат сверху
        long screenArea = (long) dm.widthPixels * h;
        int slack = Math.round(8 * dm.density);
        List<Rect> lists = new ArrayList<>();
        List<Integer> listParent = new ArrayList<>();
        List<Boolean> hiddenList = new ArrayList<>();
        List<Integer> elemList = new ArrayList<>();
        List<Boolean> needsProbe = new ArrayList<>();
        // Обход в глубину: {узел, номер нажимаемого предка, глубина, большой список}
        ArrayDeque<Object[]> stack = new ArrayDeque<>();
        for (int k = roots.size() - 1; k >= 0; k--) stack.push(new Object[]{roots.get(k), -1, 0, -1});
        while (!stack.isEmpty()) {
            Object[] top = stack.pop();
            AccessibilityNodeInfo n = (AccessibilityNodeInfo) top[0];
            int parentAction = (Integer) top[1];
            int depth = (Integer) top[2];
            int list = (Integer) top[3];
            if (!n.isVisibleToUser()) continue;
            Rect b = new Rect();
            n.getBoundsInScreen(b);
            boolean editable = n.isEditable();
            boolean checkable = n.isCheckable();
            // Строка Telegram: «нажать» есть только в списке действий, флага
            // clickable нет — нажимается касанием (tap), но в снимке она строка
            boolean actionOnly = !n.isClickable() && !n.isLongClickable()
                && hasAction(n, AccessibilityNodeInfo.AccessibilityAction.ACTION_CLICK);
            boolean clickable = n.isClickable() || n.isLongClickable() || actionOnly;
            boolean scrollable = n.isScrollable();
            boolean pw = n.isPassword();
            if (scrollable && (long) b.width() * b.height() * 10 >= screenArea * 4) {
                int id = lists.size();
                for (int j = 0; j < id; j++) {
                    if (!hiddenList.get(j) && !onChain(j, list, listParent) && sameRect(lists.get(j), b, slack)) {
                        hiddenList.set(j, true);
                    }
                }
                lists.add(new Rect(b));
                listParent.add(list);
                hiddenList.add(false);
                list = id;
            }
            // У поля подпись (t) — его содержимое, имя — подсказка (h): пустое
            // поле «Поиск» не должно выглядеть заполненным словом «Поиск»
            String hint = editable ? fieldHint(n) : "";
            String own = editable ? fieldValue(n, pw, hint) : ownLabel(n, pw);
            boolean actionable = clickable || editable || checkable;
            int mine = parentAction;
            if ((actionable || scrollable || !own.isEmpty()) && b.width() > 0 && b.height() > 0) {
                if (elems.size() >= MAX_ELEMENTS) {
                    more = true;
                    break;
                }
                String label = own;
                if (label.isEmpty() && (clickable || checkable)) label = childText(n);
                JSONObject e = new JSONObject();
                e.put("i", elems.size());
                e.put("k", actionOnly && !editable && !checkable ? "item"
                    : kind(n, editable, checkable, clickable, scrollable, own));
                e.put("t", label);
                if (!hint.isEmpty()) e.put("h", hint);
                if (clickable) e.put("c", 1);
                if (editable) e.put("ed", 1);
                if (checkable) e.put("on", n.isChecked() ? 1 : 0);
                if (!n.isEnabled()) e.put("off", 1);
                if (n.isFocused()) e.put("f", 1);
                if (n.isSelected()) e.put("sel", 1);
                if (pw) e.put("pw", 1);
                if (scrollable) e.put("sc", 1);
                e.put("b", new JSONArray().put(b.left).put(b.top).put(b.right).put(b.bottom));
                if (parentAction >= 0) e.put("p", parentAction);
                String id = n.getViewIdResourceName();
                if (id != null) {
                    int slash = id.indexOf(":id/");
                    e.put("id", slash >= 0 ? id.substring(slash + 4) : id);
                }
                elems.add(e);
                nodes.add(n);
                elemList.add(list);
                // Строка со своим рисованием (Telegram): описание ставит себе
                // только в событии доступности, а переиспользованная под другой
                // чат — не обновляет. Подпись вызываем ниже, каждый снимок
                needsProbe.add(actionOnly && !editable && clean(n.getText()).isEmpty());
                if (actionable) mine = elems.size() - 1;
            }
            if (depth >= MAX_DEPTH) continue;
            for (int c = n.getChildCount() - 1; c >= 0; c--) {
                AccessibilityNodeInfo child = n.getChild(c);
                if (child != null) stack.push(new Object[]{child, mine, depth + 1, list});
            }
        }
        if (hiddenList.contains(true)) {
            // Скрытые страницы — вон; номера и ссылки на нажимаемого предка — заново
            int[] remap = new int[elems.size()];
            List<JSONObject> keptE = new ArrayList<>();
            List<AccessibilityNodeInfo> keptN = new ArrayList<>();
            List<Boolean> keptP = new ArrayList<>();
            for (int k = 0; k < elems.size(); k++) {
                if (underHidden(elemList.get(k), listParent, hiddenList)) {
                    remap[k] = -1;
                    continue;
                }
                remap[k] = keptE.size();
                keptE.add(elems.get(k));
                keptN.add(nodes.get(k));
                keptP.add(needsProbe.get(k));
            }
            for (JSONObject e : keptE) {
                e.put("i", remap[e.getInt("i")]);
                if (e.has("p")) {
                    int np = remap[e.getInt("p")];
                    if (np >= 0) e.put("p", np);
                    else e.remove("p");
                }
            }
            elems.clear();
            elems.addAll(keptE);
            nodes.clear();
            nodes.addAll(keptN);
            needsProbe = keptP;
        }
        // Подписи строк, которые их прячут, — только видимым
        int probes = 0;
        for (int k = 0; k < elems.size() && probes < MAX_PROBES; k++) {
            if (!needsProbe.get(k)) continue;
            probes++;
            String fresh = probeLabel(nodes.get(k));
            if (fresh != null && !fresh.isEmpty()) elems.get(k).put("t", fresh);
        }
        JSONArray arr = new JSONArray();
        for (JSONObject e : elems) arr.put(e);
        out.put("elements", arr);
        if (more) out.put("more", true);
        return out;
    }

    // j — сам list или его предок среди больших списков
    private static boolean onChain(int j, int list, List<Integer> parent) {
        for (int x = list; x >= 0; x = parent.get(x)) if (x == j) return true;
        return false;
    }

    private static boolean underHidden(int list, List<Integer> parent, List<Boolean> hidden) {
        for (int x = list; x >= 0; x = parent.get(x)) if (hidden.get(x)) return true;
        return false;
    }

    private static boolean sameRect(Rect a, Rect b, int slack) {
        return Math.abs(a.left - b.left) <= slack && Math.abs(a.top - b.top) <= slack
            && Math.abs(a.right - b.right) <= slack && Math.abs(a.bottom - b.bottom) <= slack;
    }

    private static boolean hasAction(AccessibilityNodeInfo n, AccessibilityNodeInfo.AccessibilityAction a) {
        List<AccessibilityNodeInfo.AccessibilityAction> list = n.getActionList();
        return list != null && list.contains(a);
    }

    /** Строка без текста в дереве (Telegram рисует его сам и отдаёт только в
     * событие доступности: onPopulateAccessibilityEvent → setContentDescription).
     * Событие вызываем сами — фокус доступности, где система его даёт (он
     * без чтеца экрана не рисуется), иначе «выделить» и сразу снять — и
     * читаем описание, которое строка себе поставила. null — вызвать событие
     * не вышло (тогда остаётся то описание, что было). */
    private static String probeLabel(AccessibilityNodeInfo n) {
        String how;
        if (n.performAction(AccessibilityNodeInfo.ACTION_ACCESSIBILITY_FOCUS)) {
            n.performAction(AccessibilityNodeInfo.ACTION_CLEAR_ACCESSIBILITY_FOCUS);
            how = "focus";
        } else if (!n.isSelected() && n.performAction(AccessibilityNodeInfo.ACTION_SELECT)) {
            n.performAction(AccessibilityNodeInfo.ACTION_CLEAR_SELECTION);
            how = "select";
        } else {
            return null;
        }
        if (!n.refresh()) return null;
        String s = clean(n.getContentDescription());
        if (s.isEmpty()) s = clean(n.getText());
        if (!probeLogged) {
            probeLogged = true;
            android.util.Log.i("PhoneControl", "зонд строки: " + how + ", подпись " + (s.isEmpty() ? "пустая" : "есть"));
        }
        return s;
    }

    private static boolean probeLogged;

    private static String ownLabel(AccessibilityNodeInfo n, boolean pw) {
        String s = pw ? "" : clean(n.getText());
        if (s.isEmpty()) s = clean(n.getContentDescription());
        if (s.isEmpty() && Build.VERSION.SDK_INT >= 26) s = clean(n.getHintText());
        return s;
    }

    private static String fieldHint(AccessibilityNodeInfo n) {
        String h = Build.VERSION.SDK_INT >= 26 ? clean(n.getHintText()) : "";
        return h.isEmpty() ? clean(n.getContentDescription()) : h;
    }

    private static String fieldValue(AccessibilityNodeInfo n, boolean pw, String hint) {
        if (pw) return "";
        if (Build.VERSION.SDK_INT >= 26 && n.isShowingHintText()) return "";
        String v = clean(n.getText());
        // Старые версии отдают подсказку вместо пустого текста
        return v.equals(hint) ? "" : v;
    }

    // Подпись строки без своего текста — первые тексты внутри неё
    private static String childText(AccessibilityNodeInfo n) {
        StringBuilder sb = new StringBuilder();
        ArrayDeque<Object[]> q = new ArrayDeque<>();
        q.add(new Object[]{n, 0});
        int found = 0;
        while (!q.isEmpty() && found < 3) {
            Object[] top = q.poll();
            AccessibilityNodeInfo x = (AccessibilityNodeInfo) top[0];
            int d = (Integer) top[1];
            if (x != n) {
                if (!x.isVisibleToUser()) continue;
                String s = ownLabel(x, x.isPassword());
                if (!s.isEmpty()) {
                    if (sb.length() > 0) sb.append(" · ");
                    sb.append(s);
                    found++;
                }
            }
            if (d >= 4) continue;
            for (int c = 0; c < x.getChildCount(); c++) {
                AccessibilityNodeInfo child = x.getChild(c);
                if (child != null) q.add(new Object[]{child, d + 1});
            }
        }
        return cut(sb.toString());
    }

    private static String kind(AccessibilityNodeInfo n, boolean editable, boolean checkable,
                               boolean clickable, boolean scrollable, String own) {
        String cls = str(n.getClassName());
        if (editable) return "field";
        if (checkable) return cls.contains("Switch") ? "switch" : "check";
        if (clickable) return own.isEmpty() ? "item" : "button";
        if (scrollable) return "list";
        if (cls.contains("Image")) return "image";
        return "text";
    }

    private static String appLabel(Context ctx, String pkg) {
        try {
            PackageManager pm = ctx.getPackageManager();
            return String.valueOf(pm.getApplicationLabel(pm.getApplicationInfo(pkg, 0)));
        } catch (PackageManager.NameNotFoundException e) {
            return pkg;
        }
    }

    // Рабочий стол: <queries> в манифесте открывает его пакет приложению
    private static String homePackage(Context ctx) {
        android.content.pm.ResolveInfo ri = ctx.getPackageManager().resolveActivity(
            new android.content.Intent(android.content.Intent.ACTION_MAIN)
                .addCategory(android.content.Intent.CATEGORY_HOME),
            PackageManager.MATCH_DEFAULT_ONLY);
        return ri == null || ri.activityInfo == null ? "" : ri.activityInfo.packageName;
    }

    private static String str(CharSequence cs) {
        return cs == null ? "" : cs.toString();
    }

    private static String clean(CharSequence cs) {
        if (cs == null) return "";
        return cut(cs.toString().replaceAll("\\s+", " ").trim());
    }

    private static String cut(String s) {
        return s.length() > MAX_LABEL ? s.substring(0, MAX_LABEL - 1) + "…" : s;
    }

    // ── Какой узел имелся в виду ──────────────────────────────────────

    private static AccessibilityNodeInfo resolve(PhoneControlService s, JSONObject args) throws Failure {
        int snap = args.optInt("snap", -1);
        int i = args.optInt("i", -1);
        synchronized (lock) {
            if (snap == snapId && i >= 0 && i < nodes.size()) {
                AccessibilityNodeInfo n = nodes.get(i);
                if (n.refresh() && n.isVisibleToUser()) return n;
            }
        }
        // Экран сменился: тот же элемент — по подписи и месту в свежем снимке
        String label = args.optString("t", "");
        if (label.isEmpty()) throw new Failure("gone", "элемента больше нет на экране");
        JSONArray bb = args.optJSONArray("b");
        snapshot(s);
        float density = s.getResources().getDisplayMetrics().density;
        int maxDist = Math.round(SAME_PLACE_DP * density);
        synchronized (lock) {
            AccessibilityNodeInfo best = null;
            int bestDist = Integer.MAX_VALUE;
            int same = 0;
            for (int k = 0; k < elems.size(); k++) {
                JSONObject e = elems.get(k);
                if (!label.equalsIgnoreCase(e.optString("t", ""))) continue;
                same++;
                int dist = 0;
                JSONArray eb = e.optJSONArray("b");
                if (bb != null && eb != null) {
                    int dx = (eb.optInt(0) + eb.optInt(2) - bb.optInt(0) - bb.optInt(2)) / 2;
                    int dy = (eb.optInt(1) + eb.optInt(3) - bb.optInt(1) - bb.optInt(3)) / 2;
                    dist = Math.abs(dx) + Math.abs(dy);
                    if (dist > maxDist) continue;
                }
                if (dist < bestDist) {
                    bestDist = dist;
                    best = nodes.get(k);
                }
            }
            if (best != null && (bb != null || same == 1)) return best;
        }
        throw new Failure("gone", "элемента больше нет на экране");
    }

    // ── Действия ──────────────────────────────────────────────────────

    static JSONObject tap(PhoneControlService s, JSONObject args) throws Failure, JSONException {
        AccessibilityNodeInfo n = resolve(s, args);
        Rect b = new Rect();
        n.getBoundsInScreen(b);
        if (!n.isClickable() && hasAction(n, AccessibilityNodeInfo.AccessibilityAction.ACTION_CLICK)) {
            // Строка со своим обработчиком касаний (Telegram): ACTION_CLICK
            // без флага clickable система не исполняет — касание в её середину,
            // а не нажатие на предка-контейнер
            if (!n.isEnabled()) throw new Failure("disabled", "элемент неактивен");
            if (n.performAction(AccessibilityNodeInfo.ACTION_CLICK)) return new JSONObject().put("method", "click");
            if (tapAt(s, b.centerX(), b.centerY())) return new JSONObject().put("method", "gesture");
            throw new Failure("failed", "нажатие не прошло");
        }
        AccessibilityNodeInfo t = n;
        for (int up = 0; t != null && !t.isClickable() && up < 8; up++) t = t.getParent();
        if (t != null && t.isClickable() && !t.isEnabled()) throw new Failure("disabled", "элемент неактивен");
        String method;
        if (t != null && t.isClickable() && t.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
            method = "click";
        } else if (tapAt(s, b.centerX(), b.centerY())) {
            method = "gesture";
        } else {
            throw new Failure("failed", "нажатие не прошло");
        }
        return new JSONObject().put("method", method);
    }

    static JSONObject type(PhoneControlService s, JSONObject args) throws Failure, JSONException {
        String text = args.optString("text", "");
        AccessibilityNodeInfo n = args.has("i") || args.has("t")
            ? resolve(s, args) : s.findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
        if (n == null) throw new Failure("no_field", "нет поля ввода");
        if (!n.isEditable()) {
            AccessibilityNodeInfo d = findEditable(n, 4);
            if (d == null) throw new Failure("not_field", "это не поле ввода");
            n = d;
        }
        if (!n.isEnabled()) throw new Failure("disabled", "поле неактивно");
        n.performAction(AccessibilityNodeInfo.ACTION_FOCUS);
        Bundle a = new Bundle();
        a.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text);
        if (!n.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, a)) {
            throw new Failure("failed", "поле не приняло текст");
        }
        return new JSONObject().put("method", "set_text");
    }

    private static AccessibilityNodeInfo findEditable(AccessibilityNodeInfo n, int depth) {
        if (n.isEditable()) return n;
        if (depth <= 0) return null;
        for (int c = 0; c < n.getChildCount(); c++) {
            AccessibilityNodeInfo child = n.getChild(c);
            if (child == null) continue;
            AccessibilityNodeInfo r = findEditable(child, depth - 1);
            if (r != null) return r;
        }
        return null;
    }

    static JSONObject scroll(PhoneControlService s, JSONObject args) throws Failure, JSONException {
        String dir = args.optString("dir", "down");
        AccessibilityNodeInfo target = null;
        if (args.has("i") || args.has("t")) {
            target = resolve(s, args);
        } else {
            snapshot(s);
            synchronized (lock) {
                long bestArea = 0;
                for (AccessibilityNodeInfo n : nodes) {
                    if (!n.isScrollable()) continue;
                    Rect b = new Rect();
                    n.getBoundsInScreen(b);
                    long area = (long) b.width() * b.height();
                    if (area > bestArea) {
                        bestArea = area;
                        target = n;
                    }
                }
            }
        }
        if (target != null) {
            AccessibilityNodeInfo.AccessibilityAction act = dirAction(dir);
            if (act != null && target.getActionList().contains(act) && target.performAction(act.getId())) {
                return new JSONObject().put("method", "scroll");
            }
            boolean back = dir.equals("up") || dir.equals("left");
            if (target.performAction(back ? AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD
                                          : AccessibilityNodeInfo.ACTION_SCROLL_FORWARD)) {
                return new JSONObject().put("method", "scroll");
            }
        }
        // Листать нечем (веб-страница, карта, своя отрисовка) — проведём пальцем
        DisplayMetrics dm = s.getResources().getDisplayMetrics();
        float w = dm.widthPixels, h = dm.heightPixels;
        float x0 = w / 2, y0 = h * 0.72f, x1 = w / 2, y1 = h * 0.32f;
        switch (dir) {
            case "up": y0 = h * 0.32f; y1 = h * 0.72f; break;
            case "left": x0 = w * 0.2f; x1 = w * 0.8f; y0 = y1 = h / 2; break;
            case "right": x0 = w * 0.8f; x1 = w * 0.2f; y0 = y1 = h / 2; break;
            default: break;
        }
        Path p = new Path();
        p.moveTo(x0, y0);
        p.lineTo(x1, y1);
        if (!gesture(s, p, 350)) throw new Failure("failed", "пролистать не вышло");
        return new JSONObject().put("method", "swipe");
    }

    // «Вниз» по-человечески — показать то, что ниже: ACTION_SCROLL_DOWN
    private static AccessibilityNodeInfo.AccessibilityAction dirAction(String dir) {
        if (Build.VERSION.SDK_INT < 23) return null;
        switch (dir) {
            case "up": return AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_UP;
            case "left": return AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_LEFT;
            case "right": return AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_RIGHT;
            default: return AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_DOWN;
        }
    }

    static JSONObject global(PhoneControlService s, JSONObject args) throws Failure, JSONException {
        String name = args.optString("name", "");
        int action;
        switch (name) {
            case "back": action = AccessibilityService.GLOBAL_ACTION_BACK; break;
            case "home": action = AccessibilityService.GLOBAL_ACTION_HOME; break;
            case "recents": action = AccessibilityService.GLOBAL_ACTION_RECENTS; break;
            case "notifications": action = AccessibilityService.GLOBAL_ACTION_NOTIFICATIONS; break;
            case "quick_settings": action = AccessibilityService.GLOBAL_ACTION_QUICK_SETTINGS; break;
            default: throw new Failure("bad_args", "нет такого действия: " + name);
        }
        if (!s.performGlobalAction(action)) throw new Failure("failed", "система не выполнила " + name);
        return new JSONObject().put("method", name);
    }

    static JSONObject apps(Context ctx) throws JSONException {
        JSONArray arr = new JSONArray();
        for (PhoneActionsCore.App a : PhoneActionsCore.listApps(ctx)) {
            arr.put(new JSONObject().put("label", a.label).put("pkg", a.pkg));
        }
        return new JSONObject().put("apps", arr);
    }

    static JSONObject openApp(PhoneControlService s, JSONObject args) throws Failure, JSONException {
        String pkg = args.optString("pkg", "");
        try {
            s.startActivity(PhoneActionsCore.openAppIntent(s, pkg));
        } catch (PhoneActionsCore.Failure e) {
            throw new Failure(e.code, e.getMessage());
        } catch (RuntimeException e) {
            throw new Failure("failed", "не открылось: " + e.getMessage());
        }
        return new JSONObject().put("method", "launch").put("pkg", pkg.toLowerCase(Locale.ROOT));
    }

    // ── Жесты ─────────────────────────────────────────────────────────

    private static boolean tapAt(PhoneControlService s, int x, int y) {
        Path p = new Path();
        p.moveTo(x, y);
        return gesture(s, p, 60);
    }

    private static boolean gesture(PhoneControlService s, Path path, long durationMs) {
        GestureDescription g = new GestureDescription.Builder()
            .addStroke(new GestureDescription.StrokeDescription(path, 0, durationMs))
            .build();
        CountDownLatch done = new CountDownLatch(1);
        AtomicBoolean ok = new AtomicBoolean(false);
        boolean sent = s.dispatchGesture(g, new AccessibilityService.GestureResultCallback() {
            @Override
            public void onCompleted(GestureDescription d) {
                ok.set(true);
                done.countDown();
            }

            @Override
            public void onCancelled(GestureDescription d) {
                done.countDown();
            }
        }, new Handler(Looper.getMainLooper()));
        if (!sent) return false;
        try {
            done.await(durationMs + 2000, TimeUnit.MILLISECONDS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
        return ok.get();
    }
}
