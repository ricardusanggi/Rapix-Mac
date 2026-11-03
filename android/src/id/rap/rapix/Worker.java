package id.rap.rapix;

import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Matrix;
import android.media.ExifInterface;
import android.util.Log;

import java.io.BufferedInputStream;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.DataInputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Tablet ikut mengerjakan tugas dari MacBook. Server HTTP kecil di 127.0.0.1:8766 (dicapai Mac lewat
 * "adb forward"); satu permintaan = satu file. Saat ini tugasnya ubah ukuran / format gambar.
 *
 *   GET  /info                                   -> {"cores":8}
 *   POST /gambar?lebar=1920&format=webp&kualitas=80   body = file gambar, balasan = gambar hasil
 */
final class Worker {
    static final int PORT = 8766;
    private static final String TAG = "Rapix";

    private final ExecutorService pool = Executors.newFixedThreadPool(Runtime.getRuntime().availableProcessors());
    private ServerSocket server;

    void start() {
        new Thread(() -> {
            try {
                // hanya dari dalam tablet sendiri (adb forward), tidak terbuka ke Wi-Fi
                server = new ServerSocket(PORT, 16, InetAddress.getByName("127.0.0.1"));
                Log.i(TAG, "worker siap di port " + PORT);
                while (true) {
                    Socket s = server.accept();
                    pool.execute(() -> handle(s));
                }
            } catch (IOException e) {
                Log.i(TAG, "worker berhenti: " + e.getMessage());
            }
        }, "rapix-worker").start();
    }

    void stop() {
        try { if (server != null) server.close(); } catch (IOException ignored) { }
    }

    private void handle(Socket s) {
        try (Socket sock = s) {
            DataInputStream in = new DataInputStream(new BufferedInputStream(sock.getInputStream(), 1 << 16));
            String[] req = readLine(in).split(" ");
            int length = 0;
            for (String h; !(h = readLine(in)).isEmpty(); )
                if (h.toLowerCase().startsWith("content-length:")) length = Integer.parseInt(h.substring(15).trim());
            byte[] body = new byte[length];
            in.readFully(body);

            String path = req[1], route = path.split("\\?")[0];
            Map<String, String> q = query(path);
            OutputStream out = sock.getOutputStream();
            if (route.equals("/info")) {
                reply(out, 200, "application/json",
                        ("{\"cores\":" + Runtime.getRuntime().availableProcessors() + "}").getBytes(StandardCharsets.UTF_8));
            } else if (route.equals("/gambar")) {
                reply(out, 200, "application/octet-stream", image(body, q));
            } else {
                reply(out, 404, "text/plain", "tidak ada".getBytes(StandardCharsets.UTF_8));
            }
        } catch (Exception e) {  // satu file gagal tidak boleh menghentikan worker
            Log.w(TAG, "tugas gagal: " + e);
            try { reply(s.getOutputStream(), 500, "text/plain", String.valueOf(e).getBytes(StandardCharsets.UTF_8)); }
            catch (IOException ignored) { }
        }
    }

    /** Perkecil ke lebar maksimum (tidak diperbesar), putar sesuai EXIF, lalu simpan sebagai JPEG/WebP/PNG. */
    private static byte[] image(byte[] data, Map<String, String> q) throws IOException {
        int maxW = Integer.parseInt(q.getOrDefault("lebar", "0"));
        int quality = Integer.parseInt(q.getOrDefault("kualitas", "80"));
        String fmt = q.getOrDefault("format", "jpeg");

        BitmapFactory.Options opt = new BitmapFactory.Options();
        opt.inJustDecodeBounds = true;
        BitmapFactory.decodeByteArray(data, 0, data.length, opt);
        int rotation = exifRotation(data);
        boolean sideways = rotation == 90 || rotation == 270;
        int srcW = sideways ? opt.outHeight : opt.outWidth;
        if (srcW <= 0) throw new IOException("bukan gambar");

        // decode di ukuran mendekati target dulu (jauh lebih hemat memori), lalu skala halus
        opt.inJustDecodeBounds = false;
        opt.inSampleSize = 1;
        while (maxW > 0 && srcW / (opt.inSampleSize * 2) >= maxW) opt.inSampleSize *= 2;
        Bitmap bmp = BitmapFactory.decodeByteArray(data, 0, data.length, opt);

        Matrix m = new Matrix();
        if (rotation != 0) m.postRotate(rotation);
        int curW = sideways ? bmp.getHeight() : bmp.getWidth();
        if (maxW > 0 && curW > maxW) m.postScale((float) maxW / curW, (float) maxW / curW);
        if (!m.isIdentity()) {
            Bitmap t = Bitmap.createBitmap(bmp, 0, 0, bmp.getWidth(), bmp.getHeight(), m, true);
            bmp.recycle();
            bmp = t;
        }

        Bitmap.CompressFormat cf = fmt.equals("webp") ? Bitmap.CompressFormat.WEBP_LOSSY
                : fmt.equals("png") ? Bitmap.CompressFormat.PNG : Bitmap.CompressFormat.JPEG;
        ByteArrayOutputStream out = new ByteArrayOutputStream(data.length / 2);
        bmp.compress(cf, quality, out);
        bmp.recycle();
        return out.toByteArray();
    }

    private static int exifRotation(byte[] data) {
        try {
            switch (new ExifInterface(new ByteArrayInputStream(data))
                    .getAttributeInt(ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_NORMAL)) {
                case ExifInterface.ORIENTATION_ROTATE_90: return 90;
                case ExifInterface.ORIENTATION_ROTATE_180: return 180;
                case ExifInterface.ORIENTATION_ROTATE_270: return 270;
            }
        } catch (IOException ignored) { }
        return 0;
    }

    private static Map<String, String> query(String path) {
        Map<String, String> q = new HashMap<>();
        int i = path.indexOf('?');
        if (i < 0) return q;
        for (String kv : path.substring(i + 1).split("&")) {
            String[] p = kv.split("=", 2);
            try { q.put(URLDecoder.decode(p[0], "UTF-8"), p.length > 1 ? URLDecoder.decode(p[1], "UTF-8") : ""); }
            catch (IOException ignored) { }
        }
        return q;
    }

    private static String readLine(DataInputStream in) throws IOException {
        StringBuilder sb = new StringBuilder();
        int c;
        while ((c = in.read()) != -1 && c != '\n') if (c != '\r') sb.append((char) c);
        return sb.toString();
    }

    private static void reply(OutputStream out, int code, String type, byte[] body) throws IOException {
        out.write(("HTTP/1.1 " + code + " OK\r\nContent-Type: " + type + "\r\nContent-Length: " + body.length
                + "\r\nConnection: close\r\n\r\n").getBytes(StandardCharsets.US_ASCII));
        out.write(body);
        out.flush();
    }
}
