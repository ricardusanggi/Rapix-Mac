package id.rap.rapix;

import android.app.Activity;
import android.graphics.Color;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.DisplayMetrics;
import android.util.Log;
import android.view.Choreographer;
import android.view.Display;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.Surface;
import android.view.SurfaceHolder;
import android.view.SurfaceView;
import android.view.View;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
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

    private SurfaceView surfaceView;
    private TextView status;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final ExecutorService sender = Executors.newSingleThreadExecutor();

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

        surfaceView = new SurfaceView(this);
        surfaceView.getHolder().addCallback(this);
        surfaceView.setOnTouchListener((v, e) -> { onTouch(e); return true; });

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
        logVsync();
    }

    /** Catat refresh rate layar yang benar-benar berjalan (jumlah vsync per detik). */
    private void logVsync() {
        Choreographer.getInstance().postFrameCallback(new Choreographer.FrameCallback() {
            long since;
            int frames;
            @Override public void doFrame(long t) {
                if (since == 0) since = t;
                frames++;
                if (t - since >= 2_000_000_000L) {
                    Log.i(TAG, "layar " + (frames * 1_000_000_000L / (t - since)) + " Hz");
                    since = t; frames = 0;
                }
                if (!isFinishing()) Choreographer.getInstance().postFrameCallback(this);
            }
        });
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

    private void connectLoop() {
        while (active) {
            WebSocket s = new WebSocket();
            ws = s;
            try {
                String code = accessCode();
                s.connect(HOST, PORT, "/ws?k=" + code);
                sendScreenInfo();
                s.readLoop(new WebSocket.Listener() {
                    @Override public void onText(String text) { handleText(text); }
                    @Override public void onBinary(byte[] data) {
                        VideoPlayer p = player;
                        if (p != null) p.feed(data);
                    }
                });
            } catch (IOException | JSONException e) {
                Log.w(TAG, "koneksi: " + e.getMessage());
                setStatus("Menunggu Rapix di MacBook…\nPastikan kabel USB tercolok dan Rapix berjalan.");
            } finally {
                s.close();
                releasePlayer();
            }
            try { Thread.sleep(1500); } catch (InterruptedException e) { return; }
        }
    }

    /** Lewat USB tablet dianggap lokal oleh Rapix, jadi kode akses bisa dibaca dari status panel. */
    private String accessCode() throws IOException, JSONException {
        HttpURLConnection c = (HttpURLConnection) new URL("http://" + HOST + ":" + PORT + "/api/status").openConnection();
        c.setConnectTimeout(3000);
        c.setReadTimeout(3000);
        try (InputStream in = c.getInputStream()) {
            ByteArrayOutputStream buf = new ByteArrayOutputStream();
            byte[] b = new byte[8192];
            for (int n; (n = in.read(b)) > 0; ) buf.write(b, 0, n);
            String url = new JSONObject(buf.toString("UTF-8")).getString("usb_url");
            return url.substring(url.lastIndexOf('/') + 1);
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

    // ---------- sentuhan -> mouse (sama seperti halaman browser) ----------
    private static final int PENDING = 1, DRAG = 2, SCROLL = 3, DONE = 4;
    private int mode;
    private float startX, startY, scrollY0, scrollAcc, lastTapX, lastTapY;
    private long lastTapT, lastMoveT;
    private final Runnable longPress = () -> {
        if (mode == PENDING) { mode = DONE; sendPoint("rclick", startX, startY); }
    };

    private void sendPoint(String type, float x, float y) {
        float nx = Math.min(1, Math.max(0, x / surfaceView.getWidth()));
        float ny = Math.min(1, Math.max(0, y / surfaceView.getHeight()));
        send("{\"t\":\"" + type + "\",\"x\":" + nx + ",\"y\":" + ny + "}");
    }

    private float avgY(MotionEvent e) {
        float s = 0;
        for (int i = 0; i < e.getPointerCount(); i++) s += e.getY(i);
        return s / e.getPointerCount();
    }

    private void onTouch(MotionEvent e) {
        float x = e.getX(), y = e.getY();
        boolean finger = e.getToolType(0) == MotionEvent.TOOL_TYPE_FINGER;
        switch (e.getActionMasked()) {
            case MotionEvent.ACTION_DOWN:
                startX = x; startY = y;
                sendPoint("move", x, y);
                if (!finger) {  // stylus / mouse: langsung tekan
                    mode = DRAG; sendPoint("down", x, y);
                } else {
                    mode = PENDING;
                    ui.postDelayed(longPress, 600);
                }
                break;
            case MotionEvent.ACTION_POINTER_DOWN:
                if (e.getPointerCount() == 2) {
                    ui.removeCallbacks(longPress);
                    if (mode == DRAG) sendPoint("up", x, y);
                    mode = SCROLL; scrollY0 = avgY(e); scrollAcc = 0;
                }
                break;
            case MotionEvent.ACTION_MOVE:
                if (mode == SCROLL && e.getPointerCount() >= 2) {
                    float ay = avgY(e);
                    scrollAcc += ay - scrollY0; scrollY0 = ay;
                    float step = 12 * density;
                    int steps = (int) (scrollAcc / step);
                    if (steps != 0) { send("{\"t\":\"scroll\",\"dy\":" + steps + "}"); scrollAcc -= steps * step; }
                    break;
                }
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
            case MotionEvent.ACTION_POINTER_UP:
                if (mode == SCROLL && e.getPointerCount() - 1 < 2) mode = DONE;
                break;
        }
    }
}
