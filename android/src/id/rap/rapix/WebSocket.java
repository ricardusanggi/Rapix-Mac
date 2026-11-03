package id.rap.rapix;

import java.io.BufferedInputStream;
import java.io.DataInputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.Base64;

/** Klien WebSocket minimal (RFC 6455) supaya aplikasi tidak butuh library tambahan. */
final class WebSocket {
    interface Listener {
        void onText(String text);
        void onBinary(byte[] data);
    }

    private final Socket socket = new Socket();
    private DataInputStream in;
    private OutputStream out;
    private final SecureRandom random = new SecureRandom();

    void connect(String host, int port, String path) throws IOException {
        socket.connect(new InetSocketAddress(host, port), 3000);
        socket.setTcpNoDelay(true);  // sentuhan langsung terkirim, tanpa ditahan Nagle
        in = new DataInputStream(new BufferedInputStream(socket.getInputStream(), 1 << 16));
        out = socket.getOutputStream();

        byte[] nonce = new byte[16];
        random.nextBytes(nonce);
        String req = "GET " + path + " HTTP/1.1\r\n"
                + "Host: " + host + ":" + port + "\r\n"
                + "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                + "Sec-WebSocket-Key: " + Base64.getEncoder().encodeToString(nonce) + "\r\n"
                + "Sec-WebSocket-Version: 13\r\n\r\n";
        out.write(req.getBytes(StandardCharsets.US_ASCII));
        out.flush();

        String status = readLine();
        if (!status.contains(" 101 ")) throw new IOException("ditolak server: " + status);
        while (!readLine().isEmpty()) { }  // lewati header balasan
    }

    private String readLine() throws IOException {
        StringBuilder sb = new StringBuilder();
        int c;
        while ((c = in.read()) != -1 && c != '\n') if (c != '\r') sb.append((char) c);
        if (c == -1) throw new IOException("koneksi ditutup");
        return sb.toString();
    }

    /** Membaca pesan sampai koneksi putus; ping dibalas otomatis. */
    void readLoop(Listener listener) throws IOException {
        byte[] message = null;
        int messageOp = 0;
        while (true) {
            int b0 = in.readUnsignedByte(), b1 = in.readUnsignedByte();
            boolean fin = (b0 & 0x80) != 0;
            int op = b0 & 0x0F;
            long len = b1 & 0x7F;
            if (len == 126) len = in.readUnsignedShort();
            else if (len == 127) len = in.readLong();
            byte[] payload = new byte[(int) len];  // server tidak memakai mask
            in.readFully(payload);

            if (op == 0x8) throw new IOException("ditutup server");
            if (op == 0x9) { send(0xA, payload); continue; }
            if (op == 0xA) continue;

            if (op != 0) { message = payload; messageOp = op; }
            else if (message != null) {  // potongan lanjutan
                byte[] joined = new byte[message.length + payload.length];
                System.arraycopy(message, 0, joined, 0, message.length);
                System.arraycopy(payload, 0, joined, message.length, payload.length);
                message = joined;
            }
            if (!fin || message == null) continue;
            if (messageOp == 0x1) listener.onText(new String(message, StandardCharsets.UTF_8));
            else if (messageOp == 0x2) listener.onBinary(message);
            message = null;
        }
    }

    void sendText(String text) throws IOException {
        send(0x1, text.getBytes(StandardCharsets.UTF_8));
    }

    /** Frame dari klien wajib di-mask (RFC 6455 5.3). */
    private synchronized void send(int op, byte[] payload) throws IOException {
        int len = payload.length;
        byte[] frame = new byte[len + (len < 126 ? 6 : 8)];
        int i = 0;
        frame[i++] = (byte) (0x80 | op);
        if (len < 126) {
            frame[i++] = (byte) (0x80 | len);
        } else {
            frame[i++] = (byte) (0x80 | 126);
            frame[i++] = (byte) (len >> 8);
            frame[i++] = (byte) len;
        }
        byte[] mask = new byte[4];
        random.nextBytes(mask);
        System.arraycopy(mask, 0, frame, i, 4);
        i += 4;
        for (int j = 0; j < len; j++) frame[i + j] = (byte) (payload[j] ^ mask[j & 3]);
        out.write(frame);
        out.flush();
    }

    void close() {
        try { socket.close(); } catch (IOException ignored) { }
    }
}
