package io.vpcore.app;

import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProviderInfo;
import android.content.Intent;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.LayoutInflater;
import android.view.View;
import android.view.ViewGroup;
import android.widget.BaseAdapter;
import android.widget.ListView;
import android.widget.TextView;

import androidx.appcompat.app.AppCompatActivity;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * Выбор персоны для виджета «Комната» (RoomWidget) или «Живой портрет»
 * (PortraitWidget) — какого, видно по провайдеру виджета: при установке на
 * рабочий стол и при перенастройке (Android 12+, долгое нажатие на виджет).
 * Список — с ядра (GET /api/personas) по адресу и токену фоновой службы; ядро
 * не ответило — последний сохранённый список и «Повторить».
 */
public class PersonaWidgetConfigure extends AppCompatActivity {

    private int widgetId = AppWidgetManager.INVALID_APPWIDGET_ID;
    // Виджет «Живой портрет», иначе «Комната»
    private boolean portrait;
    private final Handler main = new Handler(Looper.getMainLooper());
    private JSONArray personas = new JSONArray();
    private TextView status;
    private View retry;
    private BaseAdapter adapter;

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        // Закрыли без выбора — виджет не ставится
        setResult(RESULT_CANCELED);
        Bundle extras = getIntent().getExtras();
        if (extras != null) {
            widgetId = extras.getInt(AppWidgetManager.EXTRA_APPWIDGET_ID, AppWidgetManager.INVALID_APPWIDGET_ID);
        }
        if (widgetId == AppWidgetManager.INVALID_APPWIDGET_ID) {
            finish();
            return;
        }
        AppWidgetProviderInfo info = AppWidgetManager.getInstance(this).getAppWidgetInfo(widgetId);
        portrait = info != null && info.provider != null
                && PortraitWidget.class.getName().equals(info.provider.getClassName());
        setContentView(R.layout.widget_configure);
        ((TextView) findViewById(R.id.cfg_title)).setText(portrait ? R.string.portrait_cfg_title : R.string.room_cfg_title);
        status = findViewById(R.id.cfg_status);
        retry = findViewById(R.id.cfg_retry);
        ListView list = findViewById(R.id.cfg_list);
        adapter = new BaseAdapter() {
            @Override
            public int getCount() {
                return personas.length();
            }

            @Override
            public Object getItem(int i) {
                return personas.optJSONObject(i);
            }

            @Override
            public long getItemId(int i) {
                return i;
            }

            @Override
            public View getView(int i, View convert, ViewGroup parent) {
                View row = convert != null ? convert
                        : LayoutInflater.from(parent.getContext()).inflate(R.layout.widget_configure_item, parent, false);
                JSONObject p = personas.optJSONObject(i);
                ((TextView) row.findViewById(R.id.cfg_item_name)).setText(p == null ? "" : p.optString("name"));
                ((TextView) row.findViewById(R.id.cfg_item_id)).setText(p == null ? "" : p.optString("id"));
                return row;
            }
        };
        list.setAdapter(adapter);
        list.setOnItemClickListener((parent, view, pos, id) -> pick(personas.optJSONObject(pos)));
        retry.setOnClickListener(v -> load());
        findViewById(R.id.cfg_cancel).setOnClickListener(v -> finish());
        load();
    }

    private void load() {
        retry.setVisibility(View.GONE);
        if (!RoomWidget.serviceEnabled(this)) {
            show(cached(), getString(portrait ? R.string.portrait_w_off_hint : R.string.room_w_off_hint));
            return;
        }
        status.setVisibility(View.VISIBLE);
        status.setText(R.string.widget_cfg_loading);
        new Thread(() -> {
            JSONArray got = null;
            try {
                got = RoomWidget.fetchPersonas(this);
            } catch (Exception ignored) {
                // нет связи — ниже сохранённый список
            }
            JSONArray result = got;
            main.post(() -> {
                if (isFinishing()) return;
                if (result != null) {
                    show(result, result.length() == 0 ? getString(R.string.widget_cfg_empty) : null);
                } else {
                    show(cached(), getString(R.string.widget_cfg_error));
                    retry.setVisibility(View.VISIBLE);
                }
            });
        }, "widget-personas").start();
    }

    private JSONArray cached() {
        try {
            return new JSONArray(RoomWidget.prefs(this).getString(RoomWidget.KEY_PERSONAS, "[]"));
        } catch (Exception e) {
            return new JSONArray();
        }
    }

    private void show(JSONArray list, String message) {
        personas = list;
        adapter.notifyDataSetChanged();
        status.setVisibility(message == null ? View.GONE : View.VISIBLE);
        if (message != null) status.setText(message);
    }

    private void pick(JSONObject p) {
        if (p == null) return;
        String id = p.optString("id");
        String name = p.optString("name", id);
        if (portrait) PortraitWidget.configure(this, widgetId, id, name);
        else RoomWidget.configure(this, widgetId, id, name);
        setResult(RESULT_OK, new Intent().putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, widgetId));
        finish();
    }
}
