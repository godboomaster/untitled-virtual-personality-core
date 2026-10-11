package io.vpcore.app;

import android.content.ActivityNotFoundException;
import android.content.ContentResolver;
import android.content.ContentValues;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.os.Environment;
import android.provider.MediaStore;
import android.util.Base64;
import android.webkit.MimeTypeMap;
import androidx.core.content.FileProvider;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

/* Файл, присланный ядром в чат («пришли файл X»). В WebView скачивание
   blob-ссылок не работает (<a download> молчит), а по VPC Link файл и так
   приходит в память страницы — поэтому веб передаёт его сюда частями base64.

   begin {name, mime} → {id}: временный файл в кэше приложения;
   append {id, data}: дописать часть (base64);
   finish {id} → {uri, name}: в «Загрузки» — Download/Virtual Persona. На
     Android 10+ через MediaStore (разрешений не нужно); на 7–9 — в папку
     приложения на внешнем хранилище (тоже без разрешений), наружу её файл
     отдаётся через FileProvider;
   cancel {id}: бросить недописанный файл;
   open {uri, mime}: открыть приложением телефона (ACTION_VIEW). */
@CapacitorPlugin(name = "FileSave")
public class FileSavePlugin extends Plugin {

    private static final String SUBDIR = "Virtual Persona";

    private static final class Pending {
        File tmp;
        String name;
        String mime;
    }

    private final Map<String, Pending> pending = new ConcurrentHashMap<>();

    // Имя без пути и запрещённых символов: имя пришло с ядра
    private static String safeName(String name) {
        String n = name == null ? "" : name.replaceAll("[\\\\/:*?\"<>|\\p{Cntrl}]", "_").trim();
        if (n.isEmpty() || n.equals(".") || n.equals("..")) n = "file";
        return n.length() > 120 ? n.substring(n.length() - 120) : n;
    }

    private static String mimeFor(String name, String mime) {
        if (mime != null && !mime.isEmpty() && !mime.equals("application/octet-stream")) return mime;
        int dot = name.lastIndexOf('.');
        if (dot >= 0) {
            String m = MimeTypeMap.getSingleton().getMimeTypeFromExtension(name.substring(dot + 1).toLowerCase());
            if (m != null) return m;
        }
        return "application/octet-stream";
    }

    @PluginMethod
    public void begin(PluginCall call) {
        try {
            Pending p = new Pending();
            p.name = safeName(call.getString("name", "file"));
            p.mime = mimeFor(p.name, call.getString("mime", ""));
            File dir = new File(getContext().getCacheDir(), "sent-files");
            if (!dir.isDirectory() && !dir.mkdirs()) throw new IOException("cache dir");
            p.tmp = File.createTempFile("part-", ".bin", dir);
            String id = UUID.randomUUID().toString();
            pending.put(id, p);
            JSObject ret = new JSObject();
            ret.put("id", id);
            call.resolve(ret);
        } catch (IOException e) {
            call.reject("Не удалось начать сохранение: " + e.getMessage());
        }
    }

    @PluginMethod
    public void append(PluginCall call) {
        Pending p = pending.get(call.getString("id", ""));
        if (p == null) {
            call.reject("unknown id");
            return;
        }
        try (OutputStream out = new FileOutputStream(p.tmp, true)) {
            out.write(Base64.decode(call.getString("data", ""), Base64.DEFAULT));
            call.resolve();
        } catch (IOException | IllegalArgumentException e) {
            call.reject("Не удалось записать файл: " + e.getMessage());
        }
    }

    @PluginMethod
    public void cancel(PluginCall call) {
        Pending p = pending.remove(call.getString("id", ""));
        if (p != null) //noinspection ResultOfMethodCallIgnored
            p.tmp.delete();
        call.resolve();
    }

    @PluginMethod
    public void finish(PluginCall call) {
        Pending p = pending.remove(call.getString("id", ""));
        if (p == null) {
            call.reject("unknown id");
            return;
        }
        try {
            Uri uri = Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q ? toMediaStore(p) : toAppDownloads(p);
            JSObject ret = new JSObject();
            ret.put("uri", uri.toString());
            ret.put("name", p.name);
            ret.put("mime", p.mime);
            call.resolve(ret);
        } catch (IOException e) {
            call.reject("Не удалось сохранить файл: " + e.getMessage());
        } finally {
            //noinspection ResultOfMethodCallIgnored
            p.tmp.delete();
        }
    }

    // Android 10+: общая папка «Загрузки» через MediaStore; совпадение имени
    // MediaStore разводит сам («отчёт (1).pdf»)
    private Uri toMediaStore(Pending p) throws IOException {
        ContentResolver cr = getContext().getContentResolver();
        ContentValues v = new ContentValues();
        v.put(MediaStore.MediaColumns.DISPLAY_NAME, p.name);
        v.put(MediaStore.MediaColumns.MIME_TYPE, p.mime);
        v.put(MediaStore.MediaColumns.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS + "/" + SUBDIR);
        v.put(MediaStore.MediaColumns.IS_PENDING, 1);
        Uri uri = cr.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, v);
        if (uri == null) throw new IOException("MediaStore insert");
        try (InputStream in = new FileInputStream(p.tmp); OutputStream out = cr.openOutputStream(uri)) {
            if (out == null) throw new IOException("MediaStore stream");
            copy(in, out);
        } catch (IOException e) {
            cr.delete(uri, null, null);
            throw e;
        }
        ContentValues done = new ContentValues();
        done.put(MediaStore.MediaColumns.IS_PENDING, 0);
        cr.update(uri, done, null, null);
        return uri;
    }

    // Android 7–9: папка приложения (Android/data/…/files/Download) — без
    // разрешения на общую память; открывается через FileProvider
    private Uri toAppDownloads(Pending p) throws IOException {
        File base = getContext().getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS);
        if (base == null) base = new File(getContext().getFilesDir(), "Download");
        File dir = new File(base, SUBDIR);
        if (!dir.isDirectory() && !dir.mkdirs()) throw new IOException("downloads dir");
        File dst = new File(dir, p.name);
        int dot = p.name.lastIndexOf('.');
        String stem = dot > 0 ? p.name.substring(0, dot) : p.name;
        String ext = dot > 0 ? p.name.substring(dot) : "";
        for (int i = 1; dst.exists() && i < 1000; i++) dst = new File(dir, stem + " (" + i + ")" + ext);
        try (InputStream in = new FileInputStream(p.tmp); OutputStream out = new FileOutputStream(dst)) {
            copy(in, out);
        }
        return FileProvider.getUriForFile(getContext(), getContext().getPackageName() + ".fileprovider", dst);
    }

    private static void copy(InputStream in, OutputStream out) throws IOException {
        byte[] buf = new byte[64 * 1024];
        int n;
        while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
    }

    @PluginMethod
    public void open(PluginCall call) {
        String uri = call.getString("uri", "");
        if (uri.isEmpty()) {
            call.reject("no uri");
            return;
        }
        Intent intent = new Intent(Intent.ACTION_VIEW);
        intent.setDataAndType(Uri.parse(uri), mimeFor("", call.getString("mime", "")));
        intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK);
        try {
            getActivity().startActivity(intent);
            call.resolve();
        } catch (ActivityNotFoundException e) {
            call.reject("no_app", "no_app");
        }
    }
}
