package id.rap.rapix;

import android.media.MediaCodec;
import android.media.MediaFormat;
import android.os.Build;
import android.util.Log;
import android.view.Surface;

import java.io.IOException;
import java.nio.ByteBuffer;

/** Memutar aliran H.264 Annex-B dari Rapix dengan decoder hardware langsung ke layar. */
final class VideoPlayer {
    private final MediaCodec codec;
    private final Thread outputThread;
    private final Runnable requestKeyframe;
    private volatile boolean running = true;
    private boolean needKey = true;
    private long pts;

    VideoPlayer(Surface surface, int width, int height, int fps, Runnable requestKeyframe) throws IOException {
        this.requestKeyframe = requestKeyframe;
        codec = MediaCodec.createDecoderByType(MediaFormat.MIMETYPE_VIDEO_AVC);
        try {
            codec.configure(format(width, height, fps, true), surface, null, 0);
        } catch (MediaCodec.CodecException e) {  // decoder menolak petunjuk latensi rendah: pakai format dasar
            Log.w("Rapix", "format latensi rendah ditolak: " + e.getMessage());
            codec.reset();
            codec.configure(format(width, height, fps, false), surface, null, 0);
        }
        codec.start();

        // tampilkan setiap frame begitu selesai di-decode, tanpa antre menunggu jadwal
        outputThread = new Thread(() -> {
            MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
            long since = System.nanoTime();
            int frames = 0;
            while (running) {
                try {
                    int idx = codec.dequeueOutputBuffer(info, 20_000);
                    if (idx >= 0) { codec.releaseOutputBuffer(idx, true); frames++; }
                    long now = System.nanoTime();
                    if (now - since >= 2_000_000_000L) {  // catatan untuk diagnosis: adb logcat -s Rapix
                        Log.i("Rapix", "video " + (frames * 1_000_000_000L / (now - since)) + " fps");
                        since = now; frames = 0;
                    }
                } catch (IllegalStateException e) {
                    return;
                }
            }
        }, "rapix-render");
        outputThread.start();
    }

    private static MediaFormat format(int width, int height, int fps, boolean lowLatency) {
        MediaFormat fmt = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, width, height);
        if (lowLatency) {
            fmt.setInteger(MediaFormat.KEY_PRIORITY, 0);  // realtime
            fmt.setInteger(MediaFormat.KEY_OPERATING_RATE, fps);
            if (Build.VERSION.SDK_INT >= 30) fmt.setInteger(MediaFormat.KEY_LOW_LATENCY, 1);
        }
        return fmt;
    }

    /** Paket dari server: byte 0 = 1 untuk keyframe, sisanya data H.264. */
    void feed(byte[] packet) {
        boolean key = packet[0] == 1;
        if (needKey && !key) return;
        needKey = false;
        try {
            int idx = codec.dequeueInputBuffer(50_000);
            if (idx < 0) { skipToKeyframe(); return; }  // decoder penuh: lewati sampai keyframe berikutnya
            ByteBuffer buf = codec.getInputBuffer(idx);
            buf.clear();
            buf.put(packet, 1, packet.length - 1);
            pts += 11_111;
            codec.queueInputBuffer(idx, 0, packet.length - 1, pts, key ? MediaCodec.BUFFER_FLAG_KEY_FRAME : 0);
        } catch (IllegalStateException e) {
            skipToKeyframe();
        }
    }

    private void skipToKeyframe() {
        needKey = true;
        requestKeyframe.run();
    }

    void release() {
        running = false;
        try { outputThread.join(200); } catch (InterruptedException ignored) { }
        try { codec.stop(); } catch (IllegalStateException ignored) { }
        codec.release();
    }
}
