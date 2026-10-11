package io.vpcore.app;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.security.MessageDigest;

/**
 * Скачивание файла модели с докачкой: в .part (Range), сервер без докачки —
 * заново. Проверку файла и переименование делает вызывающий (модель
 * телефона — по началу файла, модель векторов — по SHA-256).
 */
final class ModelDownload {

    interface Progress {
        /** Размер файла целиком (0 — сервер не сказал) и сколько уже есть. */
        void start(long total, long have);

        void bytes(long have);

        boolean cancelled();
    }

    private ModelDownload() {
    }

    static void fetch(String url, File part, Progress p) throws IOException {
        long have = part.exists() ? part.length() : 0;
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setInstanceFollowRedirects(true);
        c.setConnectTimeout(15_000);
        c.setReadTimeout(60_000);
        if (have > 0) c.setRequestProperty("Range", "bytes=" + have + "-");
        try {
            int code = c.getResponseCode();
            if (code == 200) have = 0; // сервер не умеет докачку — заново
            else if (code == 416 && have > 0) {
                // Докачивать нечего: .part уже целиком (оборвалось до проверки)
                p.start(have, have);
                return;
            } else if (code != 206) throw new IOException("HTTP " + code);
            long rest = c.getContentLengthLong();
            long total = rest > 0 ? have + rest : 0;
            p.start(total, have);
            try (InputStream in = c.getInputStream(); OutputStream out = new FileOutputStream(part, have > 0)) {
                byte[] buf = new byte[1 << 16];
                int n;
                while ((n = in.read(buf)) > 0) {
                    if (p.cancelled()) throw new IOException("cancelled");
                    out.write(buf, 0, n);
                    have += n;
                    p.bytes(have);
                }
            }
            if (total > 0 && part.length() != total) throw new IOException("файл скачан не полностью");
        } finally {
            c.disconnect();
        }
    }

    static String sha256(File f) throws Exception {
        MessageDigest md = MessageDigest.getInstance("SHA-256");
        try (InputStream in = new FileInputStream(f)) {
            byte[] buf = new byte[1 << 16];
            int n;
            while ((n = in.read(buf)) > 0) md.update(buf, 0, n);
        }
        StringBuilder sb = new StringBuilder();
        for (byte b : md.digest()) sb.append(String.format("%02x", b));
        return sb.toString();
    }
}
