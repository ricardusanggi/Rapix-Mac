package id.rap.rapix;

import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.graphics.Color;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.DisplayMetrics;
import android.util.Log;
import android.view.Display;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.view.Surface;
import android.view.SurfaceHolder;
import android.view.SurfaceView;
import android.view.View;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.view.inputmethod.InputMethodManager;
import android.widget.FrameLayout;
import android.widget.TextView;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Layar kedua Rapix untuk tablet: video H.264 dari MacBook lewat kabel USB (adb reverse ke localhost:8765),
 * diputar dengan decoder hardware di refresh rate tinggi. Sentuhan dikirim balik sebagai mouse,
 * dengan gerakan yang sama seperti halaman browser Rapix.
 */
public class MainActivity extends Activity implements SurfaceHolder.Callback {
    private static final String TAG = "Rapix";
    private static final String HOST = "127.0.0.1";
    private static final int PORT = 8765;
    private static final int TARGET_HZ = 90;

    private InputSurface surfaceView;
    private TextView status;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final ExecutorService sender = Executors.newSingleThreadExecutor();
    private final Worker worker = new Worker();  // tablet ikut mengerjakan tugas dari Mac

    private volatile Surface surface;
    private volatile boolean active;
    private volatile WebSocket ws;
    private volatile VideoPlayer player;
    private Thread netThread;
    private float density;

    // ---------- tampilan ----------
    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        density = getResources().getDisplayMetrics().density;

        surfaceView = new InputSurface(this, new InputSurface.Keys() {
            @Override public void text(String s) { send(json("t", "text", "s", s)); }
            @Override public void key(int code) { tapKey(code); }
        });
        surfaceView.getHolder().addCallback(this);
        surfaceView.setOnTouchListener((v, e) -> { onTouch(e); return true; });
        surfaceView.setOnGenericMotionListener((v, e) -> onHover(e));

        status = new TextView(this);
        status.setTextColor(Color.WHITE);
        status.setTextSize(18);
        status.setGravity(Gravity.CENTER);
        status.setBackgroundColor(Color.BLACK);
        status.setText("Menghubungkan ke MacBook…");

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.BLACK);
        root.addView(surfaceView);
        root.addView(status, new FrameLayout.LayoutParams(-1, -1));
        setContentView(root);

        requestRefreshRate(TARGET_HZ);
        hideSystemBars();
        worker.start();
        IntentFilter f = new IntentFilter(Intent.ACTION_POWER_CONNECTED);
        f.addAction("android.hardware.usb.action.USB_STATE");
        registerReceiver(usbReceiver, f);
    }

    @Override
    protected void onDestroy() {
        unregisterReceiver(usbReceiver);
        worker.stop();
        super.onDestroy();
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) hideSystemBars();
    }

    private void hideSystemBars() {
        if (Build.VERSION.SDK_INT >= 30) {
            WindowInsetsController c = getWindow().getInsetsController();
            if (c != null) {
                c.hide(WindowInsets.Type.systemBars());
                c.setSystemBarsBehavior(WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
            }
        } else {
            getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_FULLSCREEN
                    | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY);
        }
    }

    /** Minta mode layar dengan refresh terdekat ke target; aplikasi boleh meminta di atas batas 60 Hz sistem. */
    private void requestRefreshRate(float hz) {
        Display d = getWindowManager().getDefaultDisplay();
        Display.Mode cur = d.getMode(), best = cur;
        for (Display.Mode m : d.getSupportedModes()) {
            if (m.getPhysicalWidth() != cur.getPhysicalWidth() || m.getPhysicalHeight() != cur.getPhysicalHeight()) continue;
            if (Math.abs(m.getRefreshRate() - hz) < Math.abs(best.getRefreshRate() - hz)) best = m;
        }
        WindowManager.LayoutParams lp = getWindow().getAttributes();
        lp.preferredDisplayModeId = best.getModeId();
        getWindow().setAttributes(lp);
        Log.i(TAG, "meminta mode " + best.getRefreshRate() + " Hz");
    }

    private void setStatus(String text) {
        ui.post(() -> {
            status.setText(text);
            status.setVisibility(text == null ? View.GONE : View.VISIBLE);
        });
    }

    // ---------- surface & koneksi ----------
    @Override
    public void surfaceCreated(SurfaceHolder holder) {
        surface = holder.getSurface();
        if (Build.VERSION.SDK_INT >= 30)
            surface.setFrameRate(TARGET_HZ, Surface.FRAME_RATE_COMPATIBILITY_FIXED_SOURCE);
        active = true;
        netThread = new Thread(this::connectLoop, "rapix-net");
        netThread.start();
    }

    @Override
    public void surfaceChanged(SurfaceHolder holder, int format, int w, int h) { }

    @Override
    public void surfaceDestroyed(SurfaceHolder holder) {
        active = false;
        WebSocket s = ws;
        if (s != null) s.close();
        try { netThread.join(1000); } catch (InterruptedException ignored) { }
        releasePlayer();
        surface = null;
    }

    private void releasePlayer() {
        VideoPlayer p = player;
        player = null;
        if (p != null) p.release();
    }

    /**
     * Utamakan kabel USB (paling cepat). Saat tersambung kabel, aplikasi mengambil kunci pairing dan alamat
     * Wi-Fi Mac lalu menyimpannya; tanpa kabel, aplikasi tersambung lewat Wi-Fi memakai kunci itu.
     */
    private void connectLoop() {
        while (active) {
            WebSocket s = null;
            try {
                boolean usb = pairOverUsb();
                String token = prefs().getString("token", "");
                if (usb) {
                    s = open(HOST, PORT, token);
                } else {
                    String saved = prefs().getString("hosts", "");
                    if (saved.isEmpty()) throw new IOException("belum dipasangkan");
                    s = openFirst(saved.split(","), prefs().getInt("port", PORT), token);
                }
                ws = s;
                link = usb ? "usb" : "wifi";
                Log.i(TAG, "tersambung lewat " + (usb ? "kabel USB" : "Wi-Fi"));
                if (!usb) watchForCable(s);
                sendScreenInfo();
                s.readLoop(new WebSocket.Listener() {
                    @Override public void onText(String text) { handleText(text); }
                    @Override public void onBinary(byte[] data) {
                        VideoPlayer p = player;
                        if (p != null) p.feed(data);
                    }
                });
            } catch (IOException e) {
                Log.w(TAG, "koneksi: " + e.getMessage());
                boolean paired = !prefs().getString("hosts", "").isEmpty();
                String msg = String.valueOf(e.getMessage());
                setStatus(msg.contains(" 403 ") || msg.contains(" 429 ")
                        ? "MacBook menolak koneksi Wi-Fi.\nColok kabel USB sekali untuk memasangkan ulang."
                        : paired ? "Menunggu Rapix di MacBook…\nLewat kabel USB atau Wi-Fi yang sama."
                        : "Menunggu Rapix di MacBook…\nColok kabel USB sekali; setelah itu bisa lewat Wi-Fi.");
            } finally {
                link = null;
                if (s != null) s.close();
                releasePlayer();
                synchronized (cableHint) { cableHint.notifyAll(); }  // pengecek kabel koneksi ini ikut berhenti
            }
            try { Thread.sleep(1500); } catch (InterruptedException e) { return; }
        }
    }

    private volatile String link;  // "usb" / "wifi" / null

    private android.content.SharedPreferences prefs() {
        return getSharedPreferences("rapix", MODE_PRIVATE);
    }

    /** Lewat USB tablet dianggap lokal oleh Rapix, jadi kunci pairing dan alamat Wi-Fi Mac bisa diambil. */
    private boolean pairOverUsb() {
        try {
            JSONObject p = new JSONObject(httpGet("http://" + HOST + ":" + PORT + "/api/pair", 1500));
            StringBuilder hosts = new StringBuilder();
            for (int i = 0; i < p.getJSONArray("hosts").length(); i++)
                hosts.append(i > 0 ? "," : "").append(p.getJSONArray("hosts").getString(i));
            prefs().edit().putString("token", p.getString("token")).putString("hosts", hosts.toString())
                    .putInt("port", p.optInt("port", PORT)).apply();
            return true;
        } catch (IOException | JSONException e) {
            return false;
        }
    }

    private static WebSocket open(String host, int port, String token) throws IOException {
        WebSocket s = new WebSocket();
        try {
            s.connect(host, port, "/ws?k=" + token);
            return s;
        } catch (IOException e) {
            s.close();
            throw e;
        }
    }

    /** Coba setiap alamat Mac yang tersimpan (IP lalu nama .local); pakai yang pertama berhasil. */
    private static WebSocket openFirst(String[] hosts, int port, String token) throws IOException {
        IOException last = new IOException("tidak ada alamat");
        for (String h : hosts) {
            try {
                return open(h, port, token);
            } catch (IOException e) {
                last = e;
            }
        }
        throw last;
    }

    /**
     * Selama lewat Wi-Fi, begitu kabel dicolok dan Rapix terjangkau lewat kabel, pindah ke kabel yang lebih
     * cepat. Dicek lewat /api/pair (bukan sekadar port terbuka) karena terowongan adb bisa ada walau Rapix
     * di Mac tidak berjalan.
     */
    private void watchForCable(WebSocket s) {
        new Thread(() -> {
            long checkUntil = 0;
            while (active && ws == s && "wifi".equals(link)) {
                // hemat baterai: tidak mengecek apa pun sampai sistem memberi tahu kabel dicolok; setelah itu
                // cek tiap 0,3 detik selama 15 detik (waktu Mac menyiapkan terowongan USB), lalu tidur lagi
                synchronized (cableHint) {
                    long now = System.currentTimeMillis();
                    try { cableHint.wait(now < checkUntil ? 300 : 0); } catch (InterruptedException e) { return; }
                    if (cablePlugged) { cablePlugged = false; checkUntil = System.currentTimeMillis() + 15_000; }
                }
                if (System.currentTimeMillis() >= checkUntil) continue;
                if ("wifi".equals(link) && ws == s && pairOverUsb()) {
                    Log.i(TAG, "kabel USB tersambung, pindah dari Wi-Fi");
                    s.close();
                    return;
                }
            }
        }, "rapix-cable").start();
    }

    private final Object cableHint = new Object();
    private boolean cablePlugged;

    /** Sinyal sistem saat kabel USB dicolok (data atau daya): bangunkan pengecek kabel. */
    private final BroadcastReceiver usbReceiver = new BroadcastReceiver() {
        @Override public void onReceive(Context ctx, Intent i) {
            boolean usbData = "android.hardware.usb.action.USB_STATE".equals(i.getAction())
                    && i.getBooleanExtra("connected", false);
            if (usbData || Intent.ACTION_POWER_CONNECTED.equals(i.getAction())) {
                Log.i(TAG, "kabel dicolok");
                synchronized (cableHint) { cablePlugged = true; cableHint.notifyAll(); }
            }
        }
    };

    private static String httpGet(String url, int timeout) throws IOException {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(timeout);
        c.setReadTimeout(timeout);
        try (InputStream in = c.getInputStream()) {
            ByteArrayOutputStream buf = new ByteArrayOutputStream();
            byte[] b = new byte[8192];
            for (int n; (n = in.read(b)) > 0; ) buf.write(b, 0, n);
            return buf.toString("UTF-8");
        } finally {
            c.disconnect();
        }
    }

    private void sendScreenInfo() throws IOException {
        DisplayMetrics m = new DisplayMetrics();
        getWindowManager().getDefaultDisplay().getRealMetrics(m);
        int w = Math.max(m.widthPixels, m.heightPixels), h = Math.min(m.widthPixels, m.heightPixels);
        try {
            JSONObject o = new JSONObject().put("t", "screen")
                    .put("w", Math.round(w / m.density)).put("h", Math.round(h / m.density))
                    .put("dpr", m.density).put("h264", true);
            ws.sendText(o.toString());
        } catch (JSONException ignored) { }
    }

    private void handleText(String text) {
        try {
            JSONObject d = new JSONObject(text);
            String t = d.optString("t");
            if (t.equals("error")) {
                setStatus("Gagal: " + d.optString("msg"));
            } else if (t.equals("hello")) {
                if (!"h264".equals(d.optString("codec"))) {
                    setStatus("Rapix di MacBook belum mengirim video H.264.\nPerbarui Rapix lalu coba lagi.");
                    return;
                }
                int fps = d.optInt("fps", TARGET_HZ);
                releasePlayer();
                // ukuran sebenarnya dibaca decoder dari aliran video; ini hanya perkiraan awal (Retina 2x)
                player = new VideoPlayer(surface, d.getInt("w") * 2, d.getInt("h") * 2, fps,
                        () -> send("{\"t\":\"key\"}"));
                setStatus(null);
                Log.i(TAG, "video " + d.getInt("w") + "x" + d.getInt("h") + " @ " + fps + " fps");
            }
        } catch (JSONException | IOException e) {
            Log.w(TAG, "pesan: " + e.getMessage());
        } catch (RuntimeException e) {  // mis. decoder video menolak; tampilkan, jangan tutup aplikasi
            Log.e(TAG, "decoder", e);
            setStatus("Decoder video tablet gagal: " + e.getMessage());
        }
    }

    private void send(String json) {
        WebSocket s = ws;
        if (s == null) return;
        sender.execute(() -> {
            try { s.sendText(json); } catch (IOException ignored) { }
        });
    }

    /** Pesan JSON dari pasangan kunci-nilai: json("t", "kb", "code", 29). */
    private static String json(Object... kv) {
        JSONObject o = new JSONObject();
        try {
            for (int i = 0; i + 1 < kv.length; i += 2) o.put((String) kv[i], kv[i + 1]);
        } catch (JSONException ignored) { }
        return o.toString();
    }

    // ---------- keyboard tablet -> keyboard Mac ----------
    /** Keyboard fisik (keyboard MatePad / Bluetooth): setiap tombol diteruskan, termasuk Ctrl+C dan kawan-kawan. */
    @Override
    public boolean dispatchKeyEvent(KeyEvent e) {
        int code = e.getKeyCode();
        boolean system = code == KeyEvent.KEYCODE_BACK || code == KeyEvent.KEYCODE_VOLUME_UP
                || code == KeyEvent.KEYCODE_VOLUME_DOWN || code == KeyEvent.KEYCODE_POWER;
        if (system || ws == null) return super.dispatchKeyEvent(e);
        if (e.getAction() == KeyEvent.ACTION_MULTIPLE) {  // teks gabungan dari sebagian keyboard
            if (e.getCharacters() != null) send(json("t", "text", "s", e.getCharacters()));
            return true;
        }
        int ch = e.getUnicodeChar();
        send(json("t", "kb", "code", code, "down", e.getAction() == KeyEvent.ACTION_DOWN,
                "meta", e.getMetaState(), "ch", ch > 0 ? String.valueOf((char) ch) : ""));
        return true;
    }

    private void tapKey(int code) {
        send(json("t", "kb", "code", code, "down", true, "meta", 0));
        send(json("t", "kb", "code", code, "down", false, "meta", 0));
    }

    /** Tiga jari mengetuk layar: munculkan / sembunyikan keyboard layar tablet. */
    private void toggleSoftKeyboard() {
        InputMethodManager imm = (InputMethodManager) getSystemService(INPUT_METHOD_SERVICE);
        surfaceView.requestFocus();
        softKeyboard = !softKeyboard;
        if (softKeyboard) imm.showSoftInput(surfaceView, InputMethodManager.SHOW_FORCED);
        else imm.hideSoftInputFromWindow(surfaceView.getWindowToken(), 0);
    }

    private boolean softKeyboard;

    // ---------- pen tablet -> pena gambar Mac (tekanan, kemiringan, melayang) ----------
    private static boolean isPen(MotionEvent e) {
        int tool = e.getToolType(0);
        return tool == MotionEvent.TOOL_TYPE_STYLUS || tool == MotionEvent.TOOL_TYPE_ERASER;
    }

    private void sendPen(String action, MotionEvent e) {
        float nx = Math.min(1, Math.max(0, e.getX() / surfaceView.getWidth()));
        float ny = Math.min(1, Math.max(0, e.getY() / surfaceView.getHeight()));
        // kemiringan Android (sudut dari tegak + arah) -> komponen X/Y -1..1 seperti pena Mac
        double tilt = e.getAxisValue(MotionEvent.AXIS_TILT), dir = e.getOrientation();
        float tx = (float) (Math.sin(tilt) * Math.sin(dir)), ty = (float) (-Math.sin(tilt) * Math.cos(dir));
        send(json("t", "pen", "a", action, "x", nx, "y", ny, "p", e.getPressure(), "tx", tx, "ty", ty));
    }

    private final Runnable penLeave = () -> send(json("t", "pen", "a", "leave"));

    private void onPen(MotionEvent e) {
        ui.removeCallbacks(penLeave);
        switch (e.getActionMasked()) {
            case MotionEvent.ACTION_DOWN:
                // tombol samping pena ditekan saat menyentuh = klik kanan
                if ((e.getButtonState() & MotionEvent.BUTTON_STYLUS_PRIMARY) != 0) sendPoint("rclick", e.getX(), e.getY());
                else sendPen("down", e);
                penButton = (e.getButtonState() & MotionEvent.BUTTON_STYLUS_PRIMARY) != 0;
                break;
            case MotionEvent.ACTION_MOVE:
                if (!penButton) sendPen("move", e);
                break;
            case MotionEvent.ACTION_UP:
            case MotionEvent.ACTION_CANCEL:
                if (!penButton) sendPen("up", e);
                penButton = false;
                break;
        }
    }

    private boolean penButton;

    private boolean onHover(MotionEvent e) {
        if (!isPen(e)) return false;
        switch (e.getActionMasked()) {
            case MotionEvent.ACTION_HOVER_ENTER:
            case MotionEvent.ACTION_HOVER_MOVE:
                ui.removeCallbacks(penLeave);
                sendPen("hover", e);
                return true;
            case MotionEvent.ACTION_HOVER_EXIT:
                // pena menyentuh layar juga memicu HOVER_EXIT; tunggu sebentar sebelum menganggapnya pergi
                ui.postDelayed(penLeave, 400);
                return true;
        }
        return false;
    }

    // ---------- sentuhan -> mouse & trackpad ----------
    // satu jari: ketuk = klik, ketuk dua kali = klik ganda, tahan = klik kanan, geser = seret
    // dua jari: geser = gulir halus (dengan momentum), cubit = zoom, ketuk = klik kanan
    // tiga jari: geser = Spaces / Mission Control / App Exposé, ketuk = keyboard layar
    private static final int PENDING = 1, DRAG = 2, TWO = 3, SCROLL = 4, PINCH = 5, THREE = 6, DONE = 7;
    private int mode;
    private float startX, startY, lastTapX, lastTapY;
    private long lastTapT, lastMoveT;
    private float cx0, cy0, lcx, lcy, span0, lastSpan, velX, velY;
    private long multiT0, lastScrollT;
    private boolean swiped;
    private final Runnable longPress = () -> {
        if (mode == PENDING) { mode = DONE; sendPoint("rclick", startX, startY); }
    };

    private void sendPoint(String type, float x, float y) {
        float nx = Math.min(1, Math.max(0, x / surfaceView.getWidth()));
        float ny = Math.min(1, Math.max(0, y / surfaceView.getHeight()));
        send("{\"t\":\"" + type + "\",\"x\":" + nx + ",\"y\":" + ny + "}");
    }

    /** Titik tengah semua jari yang masih menempel (jari yang sedang diangkat tidak dihitung). */
    private float[] centroid(MotionEvent e) {
        float sx = 0, sy = 0;
        int n = 0, up = e.getActionMasked() == MotionEvent.ACTION_POINTER_UP ? e.getActionIndex() : -1;
        for (int i = 0; i < e.getPointerCount(); i++) {
            if (i == up) continue;
            sx += e.getX(i); sy += e.getY(i); n++;
        }
        return new float[]{sx / Math.max(n, 1), sy / Math.max(n, 1)};
    }

    private static float span(MotionEvent e) {
        return (float) Math.hypot(e.getX(0) - e.getX(1), e.getY(0) - e.getY(1));
    }

    private void sendScroll(String phase, float dx, float dy) {
        send(json("t", "scroll2", "ph", phase, "dx", dx / surfaceView.getWidth(), "dy", dy / surfaceView.getHeight(),
                "vx", phase.equals("end") ? velX : 0, "vy", phase.equals("end") ? velY : 0));
    }

    /** Dua jari atau lebih: bedakan gulir, cubit, ketuk, dan geser tiga jari. */
    private void onMultiTouch(MotionEvent e) {
        long now = System.currentTimeMillis();
        float[] c = centroid(e);
        int fingers = e.getPointerCount();
        switch (e.getActionMasked()) {
            case MotionEvent.ACTION_POINTER_DOWN:
                ui.removeCallbacks(longPress);
                if (mode == DRAG) sendPoint("up", lcx, lcy);
                if (fingers == 2 && mode != THREE) {
                    mode = TWO; multiT0 = now; span0 = lastSpan = span(e);
                } else if (fingers >= 3 && mode != THREE) {
                    mode = THREE; swiped = false;
                    if (now - multiT0 > 400) multiT0 = now;
                }
                cx0 = lcx = c[0]; cy0 = lcy = c[1];
                break;
            case MotionEvent.ACTION_MOVE:
                float moved = (float) Math.hypot(c[0] - cx0, c[1] - cy0);
                if (mode == TWO) {
                    float pinch = Math.abs(span(e) - span0);
                    if (pinch > 30 * density && pinch > moved) {
                        mode = PINCH; lastSpan = span(e);
                    } else if (moved > 10 * density) {
                        mode = SCROLL; velX = velY = 0; lastScrollT = now;
                        sendScroll("begin", c[0] - lcx, c[1] - lcy);
                        lcx = c[0]; lcy = c[1];
                    }
                } else if (mode == SCROLL) {
                    float dx = c[0] - lcx, dy = c[1] - lcy;
                    long dt = Math.max(1, now - lastScrollT);
                    // kecepatan (relatif layar per detik), dihaluskan, untuk momentum saat jari diangkat
                    velX = 0.7f * velX + 0.3f * (dx / surfaceView.getWidth()) * 1000f / dt;
                    velY = 0.7f * velY + 0.3f * (dy / surfaceView.getHeight()) * 1000f / dt;
                    lastScrollT = now;
                    if (dx != 0 || dy != 0) sendScroll("move", dx, dy);
                    lcx = c[0]; lcy = c[1];
                } else if (mode == PINCH && fingers >= 2) {
                    float ratio = span(e) / lastSpan;
                    if (ratio > 1.12f || ratio < 1 / 1.12f) {
                        send(json("t", "zoom", "steps", ratio > 1 ? 1 : -1));
                        lastSpan = span(e);
                    }
                } else if (mode == THREE && !swiped && moved > 60 * density) {
                    float dx = c[0] - cx0, dy = c[1] - cy0;
                    String dir = Math.abs(dx) > Math.abs(dy) ? (dx < 0 ? "left" : "right") : (dy < 0 ? "up" : "down");
                    send(json("t", "swipe", "dir", dir));
                    swiped = true;
                }
                break;
            case MotionEvent.ACTION_POINTER_UP:
                int left = fingers - 1;
                if (mode == SCROLL && left < 2) {
                    if (now - lastScrollT > 80) velX = velY = 0;  // jari berhenti sebelum diangkat: tanpa momentum
                    sendScroll("end", 0, 0);
                    mode = DONE;
                } else if (mode == TWO && left < 2) {
                    if (now - multiT0 < 300) sendPoint("rclick", c[0], c[1]);  // ketuk dua jari = klik kanan
                    mode = DONE;
                } else if (mode == PINCH && left < 2) {
                    mode = DONE;
                } else if (mode == THREE && left < 3) {
                    float moved3 = (float) Math.hypot(c[0] - cx0, c[1] - cy0);
                    if (!swiped && now - multiT0 < 400 && moved3 < 20 * density) toggleSoftKeyboard();
                    mode = DONE;
                }
                break;
        }
    }

    private void onTouch(MotionEvent e) {
        if (isPen(e)) { onPen(e); return; }
        float x = e.getX(), y = e.getY();
        boolean finger = e.getToolType(0) == MotionEvent.TOOL_TYPE_FINGER;
        int action = e.getActionMasked();
        if (action == MotionEvent.ACTION_POINTER_DOWN || action == MotionEvent.ACTION_POINTER_UP
                || (action == MotionEvent.ACTION_MOVE && e.getPointerCount() >= 2)
                || (action == MotionEvent.ACTION_MOVE && mode >= TWO)) {
            onMultiTouch(e);
            return;
        }
        switch (action) {
            case MotionEvent.ACTION_DOWN:
                send(json("t", "scroll_stop"));  // jari menyentuh: hentikan gulir momentum, seperti trackpad
                startX = lcx = x; startY = lcy = y;
                sendPoint("move", x, y);
                if (!finger) {  // stylus / mouse: langsung tekan
                    mode = DRAG; sendPoint("down", x, y);
                } else {
                    mode = PENDING;
                    ui.postDelayed(longPress, 600);
                }
                break;
            case MotionEvent.ACTION_MOVE:
                lcx = x; lcy = y;
                if (mode == PENDING && Math.hypot(x - startX, y - startY) > 8 * density) {
                    ui.removeCallbacks(longPress);
                    mode = DRAG;
                    sendPoint("down", startX, startY);
                }
                if (mode == DRAG) {
                    long now = System.currentTimeMillis();
                    if (now - lastMoveT > 8) { lastMoveT = now; sendPoint("move", x, y); }
                }
                break;
            case MotionEvent.ACTION_UP:
            case MotionEvent.ACTION_CANCEL:
                if (mode == PENDING) {
                    ui.removeCallbacks(longPress);
                    long now = System.currentTimeMillis();
                    if (now - lastTapT < 350 && Math.hypot(x - lastTapX, y - lastTapY) < 25 * density) {
                        sendPoint("dclick", x, y); lastTapT = 0;
                    } else {
                        sendPoint("down", x, y); sendPoint("up", x, y);
                        lastTapT = now; lastTapX = x; lastTapY = y;
                    }
                } else if (mode == DRAG) {
                    sendPoint("move", x, y); sendPoint("up", x, y);
                }
                mode = 0;
                break;
        }
    }
}
