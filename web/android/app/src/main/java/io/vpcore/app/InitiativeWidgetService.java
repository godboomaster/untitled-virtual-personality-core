package io.vpcore.app;

import android.content.Context;
import android.content.Intent;
import android.text.format.DateFormat;
import android.text.format.DateUtils;
import android.view.View;
import android.widget.RemoteViews;
import android.widget.RemoteViewsService;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.Date;
import java.util.List;

/** Строки списка виджета «Инициатива» (InitiativeWidget): новые сверху. */
public class InitiativeWidgetService extends RemoteViewsService {

    @Override
    public RemoteViewsFactory onGetViewFactory(Intent intent) {
        return new Factory(getApplicationContext());
    }

    private static final class Factory implements RemoteViewsFactory {
        private final Context ctx;
        private final List<JSONObject> items = new ArrayList<>();

        Factory(Context ctx) {
            this.ctx = ctx;
        }

        @Override
        public void onCreate() {
        }

        @Override
        public void onDataSetChanged() {
            JSONArray all = InitiativeWidget.load(ctx);
            items.clear();
            for (int i = all.length() - 1; i >= 0; i--) {
                JSONObject it = all.optJSONObject(i);
                if (it != null) items.add(it);
            }
        }

        @Override
        public void onDestroy() {
            items.clear();
        }

        @Override
        public int getCount() {
            return items.size();
        }

        @Override
        public RemoteViews getViewAt(int position) {
            RemoteViews row = new RemoteViews(ctx.getPackageName(), R.layout.widget_initiative_item);
            if (position < 0 || position >= items.size()) return row;
            JSONObject it = items.get(position);
            String persona = it.optString("persona", "");
            row.setViewVisibility(R.id.item_divider, position == 0 ? View.GONE : View.VISIBLE);
            row.setTextViewText(R.id.item_name, it.optString("name", persona));
            row.setTextViewText(R.id.item_time, when((long) (it.optDouble("ts", 0) * 1000)));
            row.setTextViewText(R.id.item_text, it.optString("text", ""));
            row.setOnClickFillInIntent(R.id.item_root,
                    new Intent().putExtra(BackgroundInboxService.EXTRA_PERSONA, persona));
            return row;
        }

        // Сегодня — время, раньше — дата. Не «5 мин назад»: виджет
        // перерисовывается с новым сообщением, а не каждую минуту
        private String when(long ms) {
            if (ms <= 0) return "";
            if (DateUtils.isToday(ms)) return DateFormat.getTimeFormat(ctx).format(new Date(ms));
            return DateUtils.formatDateTime(ctx, ms,
                    DateUtils.FORMAT_SHOW_DATE | DateUtils.FORMAT_ABBREV_MONTH | DateUtils.FORMAT_NO_YEAR);
        }

        @Override
        public RemoteViews getLoadingView() {
            return null;
        }

        @Override
        public int getViewTypeCount() {
            return 1;
        }

        @Override
        public long getItemId(int position) {
            if (position < 0 || position >= items.size()) return position;
            JSONObject it = items.get(position);
            return (it.optString("persona") + "|" + it.optDouble("ts", 0)).hashCode();
        }

        @Override
        public boolean hasStableIds() {
            return true;
        }
    }
}
