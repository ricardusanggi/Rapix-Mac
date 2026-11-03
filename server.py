"""
Rapix - jadikan tablet sebagai LAYAR KEDUA MacBook lewat Wi-Fi. Dibuat oleh RAP.

Jalankan di MacBook:  ./Jalankan.command   (atau: python3 server.py)
Buka alamat yang muncul di browser tablet. Saat tablet tersambung, MacBook
otomatis mendapat monitor baru seukuran layar tablet.
"""
import argparse
import asyncio
import collections
import io
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import warnings
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import mss
import pyautogui
from aiohttp import WSMsgType, web
from PIL import Image, ImageDraw

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0
warnings.filterwarnings("ignore", category=DeprecationWarning)

IS_MAC = platform.system() == "Darwin"
IS_WIN = platform.system() == "Windows"
FROZEN = getattr(sys, "frozen", False)  # True saat berjalan sebagai aplikasi hasil installer
BUNDLE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))

if IS_MAC:
    # mss bawaannya merekam di resolusi nominal (Retina 2x jadi 1x) sehingga teks buram di tablet;
    # tanpa flag NominalResolution layar Retina terekam penuh
    import mss.darwin
    mss.darwin.IMAGE_OPTIONS = mss.darwin.kCGWindowImageBoundsIgnoreFraming | mss.darwin.kCGWindowImageShouldBeOpaque
STATIC = BUNDLE / "static"
HELPER = BUNDLE / "virtualdisplay"
STREAMER = BUNDLE / "screenstream"  # perekam layar -> video H.264 (ScreenCaptureKit + VideoToolbox)
STATE = {"frame": None, "seq": 0}

# aplikasi yang dibuka dari Finder/Start Menu tidak mewarisi PATH terminal; tambahkan lokasi umum adb
_extra = [
    "/opt/homebrew/bin", "/usr/local/bin",
    str(Path.home() / "Library/Android/sdk/platform-tools"),
    str(Path(os.environ.get("LOCALAPPDATA", "")) / "Android/Sdk/platform-tools"),
]
os.environ["PATH"] = os.pathsep.join([os.environ.get("PATH", "")] + _extra)


# ---------- log (juga ditampilkan di halaman panel) ----------
class Log(io.TextIOBase):
    def __init__(self, orig, path):
        self.orig, self.lines, self.part = orig, collections.deque(maxlen=40), ""
        self.file = None
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.file = open(path, "a", encoding="utf-8")

    def write(self, s):
        for out in (self.orig, self.file):
            if out:
                try:
                    out.write(s)
                    out.flush()
                except (OSError, ValueError):
                    pass
        *done, self.part = (self.part + s).split("\n")
        self.lines.extend(line for line in done if line.strip())
        return len(s)


def access_code():
    # kode acak di alamat tablet supaya orang lain di Wi-Fi yang sama tidak bisa mengendalikan komputer;
    # disimpan agar alamat (dan ikon Home Screen di tablet) tetap sama setiap kali dibuka
    f = log_path().with_name("Rapix-kode.txt")
    try:
        code = f.read_text().strip()
        if code.isalnum() and len(code) >= 5:
            return code
    except OSError:
        pass
    import secrets
    code = "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(5))
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(code)
    except OSError:
        pass
    return code


def settings_path():
    return log_path().with_name("Rapix-setelan.json")


def load_settings():
    try:
        return json.loads(settings_path().read_text())
    except (OSError, ValueError):
        return {}


def save_settings(**changes):
    data = {**load_settings(), **changes}
    try:
        settings_path().parent.mkdir(parents=True, exist_ok=True)
        settings_path().write_text(json.dumps(data))
    except OSError:
        pass


def log_path():
    if IS_MAC:
        return Path.home() / "Library/Logs/Rapix.log"
    if IS_WIN:
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Rapix/log.txt"
    return Path.home() / ".rapix.log"


LOG = Log(sys.stdout, log_path() if FROZEN else None)
sys.stdout = sys.stderr = LOG


# ---------- izin macOS ----------
def mac_permissions(prompt=False):
    """Kembalikan status izin Screen Recording & Accessibility (None jika tidak relevan)."""
    if not IS_MAC:
        return {"screen": None, "access": None}
    try:
        import Quartz
        from HIServices import AXIsProcessTrusted, AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt
    except ImportError:
        return {"screen": None, "access": None}
    screen = bool(Quartz.CGPreflightScreenCaptureAccess()) or screen_access_live(Quartz)
    stale = bool(screen and capture_blocked(Quartz))
    if stale:  # sakelar menyala tapi macOS tetap menyamarkan layar (izin milik versi lama)
        screen = False
    access = bool(AXIsProcessTrusted())
    if prompt:  # munculkan dialog macOS sehingga aplikasi masuk daftar di System Settings
        if not screen:
            Quartz.CGRequestScreenCaptureAccess()
        if not access:
            AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
    return {"screen": screen, "access": access, "screen_stale": stale}


def screen_access_live(Quartz):
    """CGPreflightScreenCaptureAccess baru berubah setelah aplikasi dibuka ulang. Judul jendela aplikasi
    lain hanya terbaca jika izin Screen Recording aktif, jadi ini mendeteksi izin yang baru dinyalakan."""
    windows = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []
    for w in windows:
        if w.get("kCGWindowOwnerPID") == os.getpid() or w.get("kCGWindowOwnerName") in ("Window Server", "Dock"):
            continue
        if w.get("kCGWindowName"):
            return True
    return False


_blocked_cache = {"t": 0.0, "v": None}


def capture_blocked(Quartz):
    """True jika tangkapan jendela aplikasi lain hanya berisi satu warna polos: tanda macOS menyamarkan
    layar karena izin Screen Recording tidak berlaku. None jika tidak bisa dipastikan (tak ada jendela)."""
    if time.time() - _blocked_cache["t"] < 5:
        return _blocked_cache["v"]
    result = None
    opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    windows = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []
    checked = 0
    for w in windows:
        b = w.get("kCGWindowBounds") or {}
        if (w.get("kCGWindowLayer") != 0 or w.get("kCGWindowOwnerPID") == os.getpid()
                or b.get("Width", 0) < 200 or b.get("Height", 0) < 150 or not w.get("kCGWindowAlpha")):
            continue
        img = Quartz.CGWindowListCreateImage(
            Quartz.CGRectNull, Quartz.kCGWindowListOptionIncludingWindow, w["kCGWindowNumber"],
            Quartz.kCGWindowImageBoundsIgnoreFraming | Quartz.kCGWindowImageNominalResolution)
        checked += 1
        if img is not None:
            raw = bytes(Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(img)))
            step = max(4, (len(raw) // 3000) // 4 * 4)
            if len({raw[i:i + 3] for i in range(0, len(raw) - 3, step)}) > 6:
                result = False  # ada isi sungguhan: izin berlaku
                break
        result = True
        if checked >= 3:
            break
    _blocked_cache.update(t=time.time(), v=result)
    return result


# ---------- aplikasi Mac: pasang ke Applications, mulai ulang, buka saat login ----------
LOGIN_PLIST = Path.home() / "Library/LaunchAgents/id.rap.rapix.plist"


def app_bundle():
    """Lokasi Rapix.app saat berjalan sebagai aplikasi Mac, selain itu None."""
    if FROZEN and IS_MAC:
        p = Path(sys.executable).resolve().parents[2]
        return p if p.suffix == ".app" else None
    return None


def login_item(enable=None, bundle=None):
    """Atur/baca pilihan 'buka saat Mac menyala' (LaunchAgent milik pengguna)."""
    bundle = bundle or app_bundle()
    if enable is True and bundle:
        import plistlib
        LOGIN_PLIST.parent.mkdir(parents=True, exist_ok=True)
        with open(LOGIN_PLIST, "wb") as f:
            plistlib.dump({
                "Label": "id.rap.rapix",
                "ProgramArguments": ["/usr/bin/open", "-g", str(bundle), "--args", "--no-panel"],
                "RunAtLoad": True,
            }, f)
    elif enable is False:
        LOGIN_PLIST.unlink(missing_ok=True)
    return LOGIN_PLIST.exists()


def remove_old_versions():
    """Hapus aplikasi bernama lama beserta pengaturan buka-otomatisnya agar tidak ikut menyala."""
    for name in ("id.layarkedua.app.plist", "id.rap.display.plist"):
        (LOGIN_PLIST.parent / name).unlink(missing_ok=True)
    for folder in (Path("/Applications"), Path.home() / "Applications"):
        for name in ("LayarKedua.app", "RAP Display.app"):
            old = folder / name
            if old.exists():
                shutil.rmtree(old, ignore_errors=True)
                print(f"Versi lama dihapus: {old}")


def install_to_applications(port):
    """Dibuka dari DMG/Downloads: salin ke Applications lalu buka dari sana.
    Izin macOS hanya tersimpan dengan benar untuk aplikasi di Applications.
    Mengembalikan True jika proses ini harus berhenti."""
    src = app_bundle()
    if not src or src.parent in (Path("/Applications"), Path.home() / "Applications"):
        return False
    if port_in_use(port):  # versi lama sedang berjalan: hentikan dulu sebelum diganti
        import urllib.request
        try:
            urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/api/quit", method="POST"), timeout=3)
        except OSError:
            pass
        time.sleep(1.5)
    for folder in (Path("/Applications"), Path.home() / "Applications"):
        dst = folder / src.name
        try:
            folder.mkdir(exist_ok=True)
            if dst.exists():
                shutil.rmtree(dst)  # ganti versi lama
            subprocess.run(["ditto", str(src), str(dst)], check=True)
        except (OSError, subprocess.CalledProcessError):
            continue
        print(f"Rapix dipasang di {dst}")
        login_item(True, bundle=dst)  # langsung siap setiap Mac menyala; bisa dimatikan di panel
        subprocess.Popen(["open", "-n", str(dst)])
        return True
    print("Tidak bisa memasang ke Applications; berjalan dari lokasi sekarang.")
    return False


# ---------- utilitas ----------
def local_ip():
    # utamakan alamat Wi-Fi/Ethernet; jika VPN aktif, rute ke internet lewat VPN
    # dan alamatnya tidak bisa dibuka tablet
    if IS_MAC:
        for iface in ("en0", "en1"):
            r = subprocess.run(["ipconfig", "getifaddr", iface], capture_output=True, text=True)
            if r.stdout.strip():
                return r.stdout.strip()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def wired_addresses():
    """Alamat Mac di jaringan kabel (USB tethering dari tablet, atau Ethernet), selain Wi-Fi utama."""
    if not IS_MAC:
        return []
    out = subprocess.run(["ifconfig"], capture_output=True, text=True).stdout
    found, iface = [], None
    for line in out.splitlines():
        if line and not line[0].isspace():
            iface = line.split(":")[0]
        elif line.strip().startswith("inet ") and iface and iface.startswith("en") and iface not in ("en0",):
            ip = line.split()[1]
            if not ip.startswith("169.254."):  # alamat darurat tanpa DHCP: tidak bisa dipakai tablet
                found.append(ip)
    return found


def monitors():
    with mss.mss() as sct:
        return sct.monitors[1:]  # indeks 0 = gabungan semua layar


def mon_key(m):
    return (m["left"], m["top"], m["width"], m["height"])


def print_qr(url):
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.print_ascii(out=LOG.orig, invert=True)  # langsung ke terminal, tidak masuk log panel
    except ImportError:
        pass


# ---------- layar virtual ----------
async def create_virtual_display(app, w, h, hidpi):
    before = {mon_key(m) for m in monitors()}
    proc = await asyncio.create_subprocess_exec(
        str(HELPER), str(w), str(h), "1" if hidpi else "0", str(app["cfg"].hz),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        line = await asyncio.wait_for(proc.stdout.readline(), 10)
    except asyncio.TimeoutError:
        line = b""
    if not line.startswith(b"READY"):
        err = (await proc.stderr.read()).decode().strip()
        raise RuntimeError(err or "Layar virtual gagal dibuat")
    app["vd_proc"] = proc
    app["display_id"] = int(line.split()[1])

    for _ in range(50):  # tunggu macOS memasang layar baru (maks 10 detik)
        await asyncio.sleep(0.2)
        new = [m for m in monitors() if mon_key(m) not in before]
        if new:
            return new[0]
    raise RuntimeError("Layar virtual dibuat tapi tidak terdeteksi macOS")


async def setup_display(app, info):
    cfg = app["cfg"]
    mons = monitors()
    if cfg.monitor is not None:
        return mons[cfg.monitor - 1]

    can_virtual = IS_MAC and HELPER.exists() and not cfg.mirror
    if not can_virtual:
        print("Mode cermin: tablet menampilkan salinan layar utama.")
        return mons[0]

    if cfg.resolution:
        w, h = (int(v) for v in cfg.resolution.lower().split("x"))
        hidpi = cfg.hidpi
    else:  # ikuti ukuran layar tablet
        w, h = int(info.get("w", 1280)), int(info.get("h", 800))
        hidpi = float(info.get("dpr", 1)) >= 1.5
    w, h = w - w % 2, h - h % 2
    print(f"Membuat layar kedua {w}x{h}{' (Retina)' if hidpi else ''}…")
    mon = await create_virtual_display(app, w, h, hidpi)
    print("Layar kedua aktif. Atur posisinya di System Settings → Displays → Arrange.")
    return mon


# ---------- tangkap layar ----------
_sct = {}
# kursor panah (dalam poin layar): tepi putih, isi hitam
CURSOR_EDGE = [(0, 0), (0, 17), (4.5, 13), (7.5, 19.5), (10, 18.5), (7, 12.5), (12.5, 12.5)]
CURSOR_FILL = [(1.4, 3.3), (1.4, 13.6), (4.9, 10.6), (7.9, 17.6), (8.7, 17.3), (5.7, 11.1), (9.2, 11.1)]


def grab_frame(mon, max_width, quality):
    if "s" not in _sct:
        _sct["s"] = mss.mss()
    shot = _sct["s"].grab(mon)
    cx, cy = pyautogui.position()

    # layar tidak berubah -> tidak perlu encode dan kirim ulang
    raw = shot.bgra
    if raw == _sct.get("raw") and (cx, cy) == _sct.get("cur"):
        return None
    _sct["raw"], _sct["cur"] = raw, (cx, cy)

    img = Image.frombytes("RGB", shot.size, raw, "raw", "BGRX")

    # mss tidak merekam kursor di macOS, jadi kursor digambar sendiri
    sx, sy = shot.size[0] / mon["width"], shot.size[1] / mon["height"]
    if mon["left"] <= cx < mon["left"] + mon["width"] and mon["top"] <= cy < mon["top"] + mon["height"]:
        px, py = (cx - mon["left"]) * sx, (cy - mon["top"]) * sy
        # panah seperti kursor Mac; ujungnya tepat di posisi kursor
        draw = ImageDraw.Draw(img)
        for shape, color in ((CURSOR_EDGE, (255, 255, 255)), (CURSOR_FILL, (0, 0, 0))):
            draw.polygon([(px + x * sx, py + y * sy) for x, y in shape], fill=color)

    if img.width > max_width:
        f = -(-img.width // max_width)  # pembulatan ke atas
        if img.width / f >= max_width * 0.6:
            img = img.reduce(f)  # jauh lebih cepat daripada resize, mis. Retina 2x -> 1x
        else:
            img = img.resize((max_width, round(img.height * max_width / img.width)), Image.BILINEAR)

    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


async def capture_loop(app):
    cfg, cond = app["cfg"], app["cond"]
    loop = asyncio.get_running_loop()
    pool = ThreadPoolExecutor(max_workers=1)
    interval = 1.0 / min(cfg.fps, 30)  # JPEG Retina per frame tidak sanggup lebih dari ~30 fps
    while True:
        t0 = time.perf_counter()
        if app["video"] and app["mon"]:
            refresh_monitor(app)  # tablet video tetap butuh posisi layar terbaru untuk sentuhan
        if app["clients"] and app["mon"]:
            try:
                refresh_monitor(app)
                data = await loop.run_in_executor(pool, grab_frame, app["mon"], cfg.max_width, cfg.quality)
                if data is None:
                    await asyncio.sleep(max(0.0, interval - (time.perf_counter() - t0)))
                    continue
                async with cond:
                    STATE["frame"] = data
                    STATE["seq"] += 1
                    cond.notify_all()
            except Exception as e:
                print("Gagal menangkap layar (cek izin Screen Recording):", e)
                await asyncio.sleep(2)
        await asyncio.sleep(max(0.0, interval - (time.perf_counter() - t0)))


# ---------- video H.264 ----------
class VideoClient:
    """Antrean frame video untuk satu tablet. Frame delta tidak boleh dibuang satu-satu (gambar rusak),
    jadi kalau tablet tertinggal antreannya dikosongkan lalu menunggu keyframe berikutnya."""

    def __init__(self):
        self.queue = asyncio.Queue(maxsize=90)  # ~1,5 detik di 60 fps
        self.need_key = True


def video_available(app, info):
    return (IS_MAC and STREAMER.exists() and app.get("display_id") and info.get("h264")
            and not app["cfg"].jpeg and not app.get("video_failed"))


def request_keyframe(app):
    proc = app.get("stream_proc")
    if proc and proc.returncode is None:
        try:
            proc.stdin.write(b"k\n")
        except (BrokenPipeError, ConnectionResetError):
            pass


async def stream_loop(app):
    """Menjalankan screenstream selama ada tablet video, dan meneruskan setiap frame ke semua tablet."""
    cfg = app["cfg"]
    while app["video"]:
        proc = await asyncio.create_subprocess_exec(
            str(STREAMER), str(app["display_id"]), str(cfg.fps), str(cfg.bitrate),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        app["stream_proc"] = proc
        for c in app["video"].values():
            c.need_key = True
        try:
            while True:
                head = await proc.stdout.readexactly(4)
                packet = await proc.stdout.readexactly(int.from_bytes(head, "big"))
                key = packet[0] == 1
                for c in list(app["video"].values()):
                    if c.need_key and not key:
                        continue
                    try:
                        c.queue.put_nowait(packet)
                        c.need_key = False
                    except asyncio.QueueFull:
                        while not c.queue.empty():
                            c.queue.get_nowait()
                        c.need_key = True
                        request_keyframe(app)
        except asyncio.IncompleteReadError:
            pass
        finally:
            if proc.returncode is None:
                proc.terminate()
        code = await proc.wait()
        if code == 3:  # resolusi layar berubah: jalankan ulang dengan ukuran baru
            continue
        err = (await proc.stderr.read()).decode().strip()
        if app["video"] and code not in (0, -15):
            print(f"Video H.264 gagal ({err or code}); pindah ke mode gambar JPEG.")
            app["video_failed"] = True
            for ws in list(app["video"]):
                await ws.close()  # tablet menyambung ulang dan otomatis memakai JPEG
            break
    app["stream_proc"] = None
    app["stream_task"] = None


def refresh_monitor(app):
    # macOS bisa memindahkan layar virtual setelah dibuat (posisi tersimpan, atau diatur di Arrange);
    # baca ulang posisinya dari ID layar agar gambar dan sentuhan selalu ke tempat yang benar
    did = app.get("display_id")
    if not did:
        return
    import Quartz
    r = Quartz.CGDisplayBounds(did)
    if r.size.width:
        app["mon"] = {"left": int(r.origin.x), "top": int(r.origin.y),
                      "width": int(r.size.width), "height": int(r.size.height)}


# ---------- input dari tablet ----------
def handle_input(mon, d):
    t = d.get("t")
    if t in ("move", "down", "up", "rclick", "dclick"):
        x = mon["left"] + float(d["x"]) * (mon["width"] - 1)
        y = mon["top"] + float(d["y"]) * (mon["height"] - 1)
        if t == "move":
            pyautogui.moveTo(x, y)
        elif t == "down":
            pyautogui.mouseDown(x, y)
        elif t == "up":
            pyautogui.mouseUp(x, y)
        elif t == "rclick":
            pyautogui.click(x, y, button="right")
        elif t == "dclick":
            pyautogui.doubleClick(x, y)
    elif t == "scroll":
        pyautogui.scroll(int(d.get("dy", 0)))
    elif IS_MAC and t == "kb":
        key_event(d)
    elif IS_MAC and t == "text":
        type_text(d.get("s", ""))
    elif IS_MAC and t == "pen":
        pen_event(mon, d)


# ---------- keyboard & pen tablet (aplikasi Android) ----------
# kode tombol Android (KeyEvent.KEYCODE_*) -> kode tombol virtual Mac (tata letak US/ABC)
ANDROID_TO_MAC = {
    **dict(zip(range(29, 55), [0, 11, 8, 2, 14, 3, 5, 4, 34, 38, 40, 37, 46, 45, 31, 35, 12, 15, 1, 17, 32, 9, 13, 7, 16, 6])),  # A-Z
    **dict(zip(range(7, 17), [29, 18, 19, 20, 21, 23, 22, 26, 28, 25])),  # 0-9
    66: 36, 160: 76, 67: 51, 112: 117, 61: 48, 62: 49, 111: 53, 115: 57,  # enter, numpad enter, backspace, del, tab, spasi, esc, caps
    19: 126, 20: 125, 21: 123, 22: 124, 122: 115, 123: 119, 92: 116, 93: 121,  # panah, home, end, page up/down
    68: 50, 69: 27, 70: 24, 71: 33, 72: 30, 73: 42, 74: 41, 75: 39, 76: 44, 55: 43, 56: 47,  # ` - = [ ] \ ; ' / , .
    **dict(zip(range(131, 143), [122, 120, 99, 118, 96, 97, 98, 100, 101, 109, 103, 111])),  # F1-F12
    # tombol modifier ikut dikirim supaya tahan Cmd/Shift (mis. Cmd+klik) juga bekerja
    113: 55, 114: 55, 59: 56, 60: 56, 57: 58, 58: 58, 117: 59, 118: 59,
}
# Ctrl di keyboard tablet = Cmd di Mac (Ctrl+C jadi salin), Alt = Option, tombol Windows/Huawei = Control
META_TO_MAC = ((0x1000, 0x100000), (0x1, 0x20000), (0x2, 0x80000), (0x10000, 0x40000))


def mac_flags(meta):
    flags = 0
    for android, mac in META_TO_MAC:
        if meta & android:
            flags |= mac
    return flags


def key_event(d):
    import Quartz
    code = ANDROID_TO_MAC.get(int(d.get("code", -1)))
    if code is None:  # tombol tanpa padanan (mis. huruf non-US): ketik karakternya saja
        if d.get("down") and d.get("ch"):
            type_text(d["ch"])
        return
    e = Quartz.CGEventCreateKeyboardEvent(None, code, bool(d.get("down")))
    Quartz.CGEventSetFlags(e, mac_flags(int(d.get("meta", 0))))
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)


def type_text(s):
    """Teks dari keyboard layar tablet: dikirim per karakter sebagai Unicode, tidak bergantung tata letak."""
    import Quartz
    for ch in s:
        for down in (True, False):
            e = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
            Quartz.CGEventKeyboardSetUnicodeString(e, len(ch.encode("utf-16-le")) // 2, ch)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)


_pen = {"near": False, "down": False}


def pen_event(mon, d):
    """Pen tablet sebagai pena gambar Mac: tekanan dan kemiringan dibaca aplikasi seperti Photoshop/Krita."""
    import Quartz
    a = d.get("a")
    x = mon["left"] + float(d.get("x", 0)) * (mon["width"] - 1)
    y = mon["top"] + float(d.get("y", 0)) * (mon["height"] - 1)
    pressure = max(0.0, min(1.0, float(d.get("p", 0)))) if a in ("down", "move") else 0.0

    if a == "leave":
        if _pen["near"]:
            proximity(False)
        return
    if not _pen["near"]:
        proximity(True)

    if a == "down":
        kind, _pen["down"] = Quartz.kCGEventLeftMouseDown, True
    elif a == "up":
        kind, _pen["down"] = Quartz.kCGEventLeftMouseUp, False
    elif _pen["down"]:
        kind = Quartz.kCGEventLeftMouseDragged
    else:
        kind = Quartz.kCGEventMouseMoved  # pen melayang di atas layar: gerakkan kursor saja

    e = Quartz.CGEventCreateMouseEvent(None, kind, (x, y), Quartz.kCGMouseButtonLeft)
    Quartz.CGEventSetIntegerValueField(e, Quartz.kCGMouseEventSubtype, Quartz.kCGEventMouseSubtypeTabletPoint)
    Quartz.CGEventSetIntegerValueField(e, Quartz.kCGMouseEventClickState, 1)
    Quartz.CGEventSetDoubleValueField(e, Quartz.kCGMouseEventPressure, pressure)
    Quartz.CGEventSetDoubleValueField(e, Quartz.kCGTabletEventPointPressure, pressure)
    Quartz.CGEventSetDoubleValueField(e, Quartz.kCGTabletEventTiltX, float(d.get("tx", 0)))
    Quartz.CGEventSetDoubleValueField(e, Quartz.kCGTabletEventTiltY, float(d.get("ty", 0)))
    Quartz.CGEventSetIntegerValueField(e, Quartz.kCGTabletEventPointX, int(x))
    Quartz.CGEventSetIntegerValueField(e, Quartz.kCGTabletEventPointY, int(y))
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)


def proximity(enter):
    """Beri tahu macOS bahwa pena mendekat/menjauh, supaya aplikasi gambar mengenalinya sebagai pena."""
    import Quartz
    e = Quartz.CGEventCreate(None)
    Quartz.CGEventSetType(e, Quartz.kCGEventTabletProximity)
    Quartz.CGEventSetIntegerValueField(e, Quartz.kCGTabletProximityEventEnterProximity, 1 if enter else 0)
    Quartz.CGEventSetIntegerValueField(e, Quartz.kCGTabletProximityEventPointerType, 1)  # 1 = pena
    Quartz.CGEventSetIntegerValueField(e, Quartz.kCGTabletProximityEventDeviceID, 1)
    Quartz.CGEventSetIntegerValueField(e, Quartz.kCGTabletProximityEventCapabilityMask, 0x0C47)  # posisi, tekanan, tilt
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)
    _pen["near"] = enter


# ---------- web ----------
async def index(request):
    if request.match_info["code"] != request.app["code"]:
        raise web.HTTPNotFound(text="Alamat salah. Pakai alamat lengkap (dengan kode) dari panel Rapix.")
    return web.FileResponse(STATIC / "index.html")


async def home(request):
    if request.remote in ("127.0.0.1", "::1"):
        raise web.HTTPFound("/panel")
    return web.Response(text="Pakai alamat lengkap (dengan kode) dari panel Rapix.")


async def ws_handler(request):
    app = request.app
    if request.query.get("k") != app["code"]:
        raise web.HTTPForbidden()
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=0)
    await ws.prepare(request)
    loop = asyncio.get_running_loop()
    peer = request.remote

    # pesan pertama dari tablet: ukuran layarnya
    info = {}
    try:
        first = await ws.receive(timeout=5)
        if first.type == WSMsgType.TEXT:
            info = json.loads(first.data)
    except asyncio.TimeoutError:
        pass

    async with app["setup_lock"]:
        if app["mon"] is None:
            try:
                app["mon"] = await setup_display(app, info)
            except Exception as e:
                print("Error:", e)
                await ws.send_json({"t": "error", "msg": str(e)})
                await ws.close()
                return ws

    mon, cond = app["mon"], app["cond"]
    video = video_available(app, info)
    print(f"Tablet terhubung: {peer}" + (" (video H.264)" if video else " (gambar JPEG)"))
    await ws.send_json({"t": "hello", "w": mon["width"], "h": mon["height"], "codec": "h264" if video else "jpeg",
                        "fps": app["cfg"].fps})
    # maks 2 frame dalam perjalanan: tablet menampilkan satu sambil menerima berikutnya
    ready = asyncio.Semaphore(2)

    async def video_sender(client):
        try:
            while not ws.closed:
                await ws.send_bytes(await client.queue.get())
        except (ConnectionResetError, asyncio.CancelledError):
            pass

    if video:
        client = VideoClient()
        app["video"][ws] = client
        if not app.get("stream_task"):
            app["stream_task"] = asyncio.create_task(stream_loop(app))
        request_keyframe(app)
    else:
        app["clients"].add(ws)

    async def sender():
        last = 0
        try:
            while not ws.closed:
                await ready.acquire()
                async with cond:
                    await cond.wait_for(lambda: STATE["seq"] != last)
                    data, last = STATE["frame"], STATE["seq"]
                await ws.send_bytes(data)
        except (ConnectionResetError, asyncio.CancelledError):
            pass

    task = asyncio.create_task(video_sender(client) if video else sender())
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            d = json.loads(msg.data)
            if d.get("t") == "ack":
                if ready._value < 2:
                    ready.release()
            elif d.get("t") == "key":  # decoder tablet tertinggal: minta keyframe baru
                if video:
                    client.need_key = True
                    request_keyframe(app)
            elif d.get("t") != "screen":
                await loop.run_in_executor(app["input_pool"], handle_input, app["mon"], d)
    finally:
        app["clients"].discard(ws)
        app["video"].pop(ws, None)
        if not app["video"] and app.get("stream_proc") and app["stream_proc"].returncode is None:
            app["stream_proc"].terminate()  # tidak ada tablet video lagi: hentikan perekam
        task.cancel()
        print(f"Tablet terputus: {peer}")
    return ws


# ---------- panel (hanya dari komputer ini) ----------
def local_only(handler):
    async def wrapped(request):
        if request.remote not in ("127.0.0.1", "::1"):
            raise web.HTTPForbidden()
        return await handler(request)
    return wrapped


@local_only
async def panel(request):
    return web.FileResponse(STATIC / "panel.html")


@local_only
async def api_status(request):
    app = request.app
    mon = app["mon"]
    return web.json_response({
        **connection_info(app),
        "clients": len(app["clients"]) + len(app["video"]),
        "codec": "h264" if app["video"] else ("jpeg" if app["clients"] else None),
        "display": f"{mon['width']}x{mon['height']}" if mon else None,
        "virtual": bool(app.get("vd_proc")),
        "os": "mac" if IS_MAC else "windows" if IS_WIN else "other",
        "perms": mac_permissions(),
        "perms_at_start": app["perms_at_start"],
        "can_restart": bool(app_bundle()),
        "restarting": app.get("restarting", False),
        "login": login_item() if app_bundle() else None,
        "log": list(LOG.lines)[-15:],
    })


def connection_info(app):
    """Alamat yang dibuka tablet sesuai pilihan koneksi (Wi-Fi atau kabel USB) beserta QR-nya."""
    port, code, mode = app["cfg"].port, app["code"], app["connection"]
    wifi_url = f"http://{local_ip()}:{port}/{code}"
    wired_urls = [f"http://{ip}:{port}/{code}" for ip in wired_addresses()]
    adb = bool(app.get("adb_devices"))
    if mode == "cable":
        target = wired_urls[0] if wired_urls else (app["usb_url"] if adb else None)
    else:
        target = wifi_url
    qrs = app.setdefault("qr_cache", {})
    if target and target not in qrs:
        qrs[target] = qr_svg(target)
    return {"connection": mode, "url": wifi_url, "target_url": target, "qr": qrs.get(target, ""),
            "wired_urls": wired_urls, "adb_connected": adb, "usb_url": app["usb_url"]}


@local_only
async def api_connection(request):
    mode = request.match_info["mode"]
    if mode in ("wifi", "cable"):
        request.app["connection"] = mode
        save_settings(connection=mode)
        print("Koneksi dipilih:", "kabel USB" if mode == "cable" else "Wi-Fi")
    return web.json_response({"connection": request.app["connection"]})


@local_only
async def api_settings(request):
    pane = {"screen": "Privacy_ScreenCapture", "access": "Privacy_Accessibility"}.get(request.match_info["which"])
    if IS_MAC and pane:
        subprocess.Popen(["open", f"x-apple.systempreferences:com.apple.preference.security?{pane}"])
    return web.json_response({"ok": True})


@local_only
async def api_quit(request):
    print("Berhenti atas permintaan dari panel.")
    stop(request.app)
    return web.json_response({"ok": True})


@local_only
async def api_restart(request):
    # izin Screen Recording baru berlaku setelah aplikasi dibuka ulang
    bundle = app_bundle()
    if not bundle:
        return web.json_response({"ok": False})
    restart_app(request.app)
    return web.json_response({"ok": True})


def restart_app(app):
    print("Memulai ulang Rapix…")
    app["restarting"] = True
    subprocess.Popen(["/bin/sh", "-c", f'sleep 1.5; open -n "{app_bundle()}" --args --no-panel --restarted'])
    stop(app)


async def permission_watch(app):
    # izin baru dinyalakan di System Settings: mulai ulang otomatis agar langsung berlaku
    start = app["perms_at_start"]
    if not app_bundle() or app["cfg"].restarted or (start["screen"] is not False and start["access"] is not False):
        return  # hanya sekali: setelah dimuat ulang tidak memuat ulang lagi
    while True:
        await asyncio.sleep(2)
        now = await asyncio.get_running_loop().run_in_executor(None, mac_permissions)
        if now["screen"] and now["access"]:
            print("Izin Screen Recording dan Accessibility sudah aktif.")
            await asyncio.sleep(2.5)  # beri waktu panel menampilkan centang
            restart_app(app)
            return


@local_only
async def api_login(request):
    on = request.match_info["state"] == "on"
    return web.json_response({"login": login_item(on)})


def stop(app):
    proc = app.get("vd_proc")
    if proc and proc.returncode is None:
        proc.terminate()  # layar virtual ikut hilang
    asyncio.get_running_loop().call_later(0.3, os._exit, 0)


def qr_svg(url):
    try:
        import qrcode
        import qrcode.image.svg
        return qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, border=1).to_string(encoding="unicode")
    except Exception:
        return ""


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


# ---------- kabel USB (Android, lewat adb reverse) ----------
async def adb(*args):
    proc = await asyncio.create_subprocess_exec(
        "adb", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await proc.communicate()
    return proc.returncode, out.decode(errors="replace")


async def usb_loop(app):
    # tablet Android yang dicolok (USB debugging aktif) membuka http://localhost:PORT,
    # dan adb meneruskannya lewat kabel ke MacBook
    port = app["cfg"].port
    known, warned = set(), set()
    while True:
        _, out = await adb("devices")
        now = set()
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 2:
                continue
            serial, state = parts[0], parts[1]
            if state == "unauthorized" and serial not in warned:
                warned.add(serial)
                print("Tablet USB terdeteksi: ketuk 'Allow/Izinkan' pada pertanyaan USB debugging di tablet.")
            if state != "device":
                continue
            now.add(serial)
            if serial in known:
                continue
            code, msg = await adb("-s", serial, "reverse", f"tcp:{port}", f"tcp:{port}")
            if code != 0:
                print("Gagal menyiapkan kabel USB:", msg.strip())
                continue
            await adb("-s", serial, "shell", "am", "start", "-a", "android.intent.action.VIEW",
                      "-d", app["usb_url"])
            print(f"Tablet tersambung lewat kabel USB. Di tablet buka: {app['usb_url']}")
        known = now
        app["adb_devices"] = len(now)
        await asyncio.sleep(2)


async def on_startup(app):
    app["cond"] = asyncio.Condition()
    app["setup_lock"] = asyncio.Lock()
    app["capture"] = asyncio.create_task(capture_loop(app))
    if shutil.which("adb"):
        app["usb"] = asyncio.create_task(usb_loop(app))
    app["perm_watch"] = asyncio.create_task(permission_watch(app))


async def on_cleanup(app):
    app["capture"].cancel()
    if "usb" in app:
        app["usb"].cancel()
    sp = app.get("stream_proc")
    if sp and sp.returncode is None:
        sp.terminate()
    proc = app.get("vd_proc")
    if proc and proc.returncode is None:
        proc.terminate()  # layar virtual ikut hilang


def main():
    p = argparse.ArgumentParser(description="Rapix: jadikan tablet layar kedua MacBook")
    p.add_argument("--resolution", help="paksa ukuran layar kedua, mis. 1280x800 (default: ikut tablet)")
    p.add_argument("--hidpi", action="store_true", help="pakai mode Retina untuk --resolution")
    p.add_argument("--mirror", action="store_true", help="cerminkan layar utama, tanpa layar kedua")
    p.add_argument("--monitor", type=int, help="pakai layar yang sudah ada (nomor dari --list)")
    p.add_argument("--list", action="store_true", help="tampilkan daftar layar lalu keluar")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--hz", type=int, default=90, help="refresh rate layar kedua (tablet mendukung 60/90/120/144)")
    p.add_argument("--fps", type=int, help="frame per detik video (bawaan: sama dengan --hz; JPEG maks ~30)")
    p.add_argument("--bitrate", type=int, default=20, help="bitrate video H.264 dalam Mbps")
    p.add_argument("--jpeg", action="store_true", help="matikan video H.264, kirim gambar JPEG per frame")
    p.add_argument("--quality", type=int, default=80, help="kualitas JPEG 30-95")
    p.add_argument("--max-width", type=int, default=2800, help="lebar maksimum gambar yang dikirim")
    p.add_argument("--panel", action=argparse.BooleanOptionalAction, default=FROZEN,
                   help="buka halaman panel di browser komputer ini (default: aktif di versi installer)")
    p.add_argument("--no-install", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--restarted", action="store_true", help=argparse.SUPPRESS)
    cfg = p.parse_args()
    cfg.fps = cfg.fps or cfg.hz
    panel_url = f"http://localhost:{cfg.port}/panel"

    if not cfg.no_install and install_to_applications(cfg.port):
        return
    if app_bundle():
        remove_old_versions()
    if login_item():
        login_item(True)  # perbarui lokasi aplikasi jika sudah dipindah

    if cfg.list:
        for i, m in enumerate(monitors(), 1):
            print(f"  [{i}] {m['width']}x{m['height']} di ({m['left']},{m['top']})" + ("  <- layar utama" if i == 1 else ""))
        return
    if port_in_use(cfg.port):  # Rapix sudah berjalan: cukup tampilkan panelnya
        print(f"Rapix sudah berjalan di port {cfg.port}.")
        if cfg.panel:
            webbrowser.open(panel_url)
        return
    if IS_MAC and not HELPER.exists() and not cfg.mirror:
        print("Peringatan: program 'virtualdisplay' belum di-compile, jadi hanya mode cermin.")
    if IS_WIN and cfg.monitor is None:
        print("Windows: tablet menampilkan salinan layar utama (atau pakai --monitor untuk layar lain).")

    code = access_code()
    url = f"http://{local_ip()}:{cfg.port}/{code}"
    app = web.Application()
    app["code"] = code
    app["perms_at_start"] = mac_permissions(prompt=True)
    app["cfg"] = cfg
    app["mon"] = None
    app["clients"] = set()
    app["video"] = {}
    app["input_pool"] = ThreadPoolExecutor(max_workers=1)
    app["connection"] = load_settings().get("connection", "wifi")
    app["usb_url"] = f"http://localhost:{cfg.port}/{code}" if shutil.which("adb") else None
    app.router.add_get("/", home)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/panel", panel)
    app.router.add_get("/api/status", api_status)
    app.router.add_post("/api/settings/{which}", api_settings)
    app.router.add_post("/api/quit", api_quit)
    app.router.add_post("/api/restart", api_restart)
    app.router.add_post("/api/connection/{mode}", api_connection)
    app.router.add_post("/api/login/{state}", api_login)
    app.router.add_get("/{code}", index)  # paling akhir: menangkap alamat berkode
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    print(f"\nBuka alamat ini di browser tablet:\n\n    {url}\n")
    if not FROZEN:
        print_qr(url)
    if app["usb_url"]:
        print(f"Atau colok tablet Android dengan kabel USB (USB debugging aktif): {app['usb_url']}\n")
    print(f"Panel kontrol: {panel_url}")
    print("Layar kedua akan muncul saat tablet tersambung. Ctrl+C untuk berhenti.\n")

    async def open_panel(_app):
        if cfg.panel:
            asyncio.get_running_loop().call_later(0.5, webbrowser.open, panel_url)
    app.on_startup.append(open_panel)
    if app_bundle():
        # sebagai aplikasi Mac: server jalan di thread terpisah, thread utama melayani macOS
        import threading
        threading.Thread(target=serve, args=(app, cfg.port), daemon=True).start()
        run_menu_bar(panel_url)
    else:
        web.run_app(app, host="0.0.0.0", port=cfg.port, print=None)


def serve(app, port):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    loop.run_until_complete(web.TCPSite(runner, "0.0.0.0", port).start())
    loop.run_forever()


def run_menu_bar(panel_url):
    """Ikon Rapix di menu bar. Juga membuat macOS mengenali Rapix sebagai aplikasi yang hidup,
    sehingga membuka Rapix lagi dari Launchpad cukup menampilkan panelnya."""
    from AppKit import (NSApplication, NSApplicationActivationPolicyAccessory, NSImage, NSMenu,
                        NSMenuItem, NSStatusBar, NSVariableStatusItemLength)
    from Foundation import NSObject

    class Delegate(NSObject):
        def applicationShouldHandleReopen_hasVisibleWindows_(self, _app, _flag):
            webbrowser.open(panel_url)
            return False

        def openPanel_(self, _sender):
            webbrowser.open(panel_url)

        def quit_(self, _sender):
            print("Berhenti dari menu bar.")
            os._exit(0)  # layar virtual ikut hilang karena programnya berhenti saat Rapix tertutup

    nsapp = NSApplication.sharedApplication()
    nsapp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    delegate = Delegate.alloc().init()
    nsapp.setDelegate_(delegate)

    item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
    icon = NSImage.imageWithSystemSymbolName_accessibilityDescription_("rectangle.on.rectangle", "Rapix")
    if icon:
        item.button().setImage_(icon)
    else:
        item.button().setTitle_("Rapix")
    menu = NSMenu.alloc().init()
    for title, action in (("Buka panel Rapix", "openPanel:"), (None, None), ("Berhenti", "quit:")):
        if title is None:
            menu.addItem_(NSMenuItem.separatorItem())
            continue
        mi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, "")
        mi.setTarget_(delegate)
        menu.addItem_(mi)
    item.setMenu_(menu)
    run_menu_bar.keep = (delegate, item, menu)  # cegah dibersihkan garbage collector
    nsapp.run()


if __name__ == "__main__":
    main()
