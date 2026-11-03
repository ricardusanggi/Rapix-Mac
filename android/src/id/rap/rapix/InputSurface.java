package id.rap.rapix;

import android.content.Context;
import android.text.InputType;
import android.view.KeyEvent;
import android.view.SurfaceView;
import android.view.inputmethod.BaseInputConnection;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.InputConnection;

/**
 * Layar video yang juga bisa menerima keyboard layar (IME). Ketikan tidak disimpan di tablet,
 * langsung diteruskan ke MacBook: teks sebagai karakter, hapus/enter sebagai tombol.
 */
final class InputSurface extends SurfaceView {
    interface Keys {
        void text(String s);
        void key(int keyCode);  // tekan + lepas satu tombol, mis. backspace atau enter
    }

    private final Keys keys;

    InputSurface(Context ctx, Keys keys) {
        super(ctx);
        this.keys = keys;
        setFocusable(true);
        setFocusableInTouchMode(true);
    }

    @Override
    public boolean onCheckIsTextEditor() {
        return true;
    }

    @Override
    public InputConnection onCreateInputConnection(EditorInfo info) {
        // tanpa saran kata dan tanpa layar penuh: setiap huruf langsung dikirim
        info.inputType = InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
                | InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD;
        info.imeOptions = EditorInfo.IME_FLAG_NO_FULLSCREEN | EditorInfo.IME_FLAG_NO_EXTRACT_UI
                | EditorInfo.IME_ACTION_NONE;
        return new BaseInputConnection(this, false) {
            @Override public boolean commitText(CharSequence text, int newCursorPosition) {
                String s = text.toString();
                if (s.equals("\n")) keys.key(KeyEvent.KEYCODE_ENTER);
                else if (!s.isEmpty()) keys.text(s);
                return true;
            }

            @Override public boolean deleteSurroundingText(int before, int after) {
                for (int i = 0; i < before; i++) keys.key(KeyEvent.KEYCODE_DEL);
                for (int i = 0; i < after; i++) keys.key(KeyEvent.KEYCODE_FORWARD_DEL);
                return true;
            }

            @Override public boolean sendKeyEvent(KeyEvent e) {
                if (e.getAction() == KeyEvent.ACTION_DOWN) keys.key(e.getKeyCode());
                return true;
            }

            @Override public boolean performEditorAction(int action) {
                keys.key(KeyEvent.KEYCODE_ENTER);
                return true;
            }
        };
    }
}
