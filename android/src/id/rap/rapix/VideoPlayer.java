package id.rap.rapix;

import android.media.MediaCodec;
import android.media.MediaFormat;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.util.Log;
import android.view.Surface;

import java.io.IOException;
import java.nio.ByteBuffer;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;

/**
 * Memutar aliran H.264 Annex-B dari Rapix dengan decoder hardware langsung ke layar.
 * Memakai mode callback: tidak ada thread yang terus bertanya ke decoder, jadi saat layar Mac diam
 * tablet benar-benar diam (hemat baterai).
 */
final class VideoPlayer {
    private final MediaCodec codec;
    private final HandlerThread thread = new HandlerThread("rapix-decoder");
    private final LinkedBlockingQueue<Integer> freeInputs = new LinkedBlockingQueue<>();
    private final Runnable requestKeyframe;
    private boolean needKey = true;
    private long pts;

    VideoPlayer(Surface surface, int width, int height, int fps, Runnable requestKeyframe) throws IOException {
        this.requestKeyframe = requestKeyframe;
        thread.start();
        codec = MediaCodec.createDecoderByType(MediaFormat.MIMETYPE_VIDEO_AVC);
        try {
            setCallback();
            codec.configure(format(width, height, fps, true), surface, null, 0);
        } catch (MediaCodec.CodecException e) {  // decoder menolak petunjuk latensi rendah: pakai format dasar
            Log.w("Rapix", "format latensi rendah ditolak: " + e.getMessage());
            codec.reset();  // reset juga menghapus callback
            setCallback();
            codec.configure(format(width, height, fps, false), surface, null, 0);
        }
        codec.start();
    }

    private void setCallback() {
        codec.setCallback(new MediaCodec.Callback() {
            @Override public void onInputBufferAvailable(MediaCodec c, int index) {
                freeInputs.offer(index);
            }

            // tampilkan setiap frame begitu selesai di-decode, tanpa antre menunggu jadwal
            @Override public void onOutputBufferAvailable(MediaCodec c, int index, MediaCodec.BufferInfo info) {
                try { c.releaseOutputBuffer(index, true); } catch (IllegalStateException ignored) { }
            }

            @Override public void onError(MediaCodec c, MediaCodec.CodecException e) {
                Log.w("Rapix", "decoder: " + e.getMessage());
            }

            @Override public void onOutputFormatChanged(MediaCodec c, MediaFormat f) { }
        }, new Handler(thread.getLooper()));
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
            Integer idx = freeInputs.poll(50, TimeUnit.MILLISECONDS);
            if (idx == null) { skipToKeyframe(); return; }  // decoder penuh: lewati sampai keyframe berikutnya
            ByteBuffer buf = codec.getInputBuffer(idx);
            buf.clear();
            buf.put(packet, 1, packet.length - 1);
            pts += 11_111;
            codec.queueInputBuffer(idx, 0, packet.length - 1, pts, key ? MediaCodec.BUFFER_FLAG_KEY_FRAME : 0);
        } catch (IllegalStateException | InterruptedException e) {
            skipToKeyframe();
        }
    }

    private void skipToKeyframe() {
        needKey = true;
        requestKeyframe.run();
    }

    void release() {
        try { codec.stop(); } catch (IllegalStateException ignored) { }
        codec.release();
        thread.quitSafely();
    }
}
