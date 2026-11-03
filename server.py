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
import zlib
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


def stored_secret(filename, length):
    """Kode acak yang disimpan, jadi tetap sama setiap Rapix dibuka; hanya bisa dibaca pemilik akun."""
    f = log_path().with_name(filename)
    try:
        code = f.read_text().strip()
        if code.isalnum() and len(code) >= length:
            return code
    except OSError:
        pass
    import secrets
    code = "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(length))
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(code)
        f.chmod(0o600)
    except OSError:
        pass
    return code


def access_code():
    # kode acak di alamat tablet supaya orang lain di Wi-Fi yang sama tidak bisa mengendalikan komputer;
    # disimpan agar alamat (dan ikon Home Screen di tablet) tetap sama setiap kali dibuka
    return stored_secret("Rapix-kode.txt", 5)


def pairing_token():
    # kunci panjang untuk aplikasi tablet lewat Wi-Fi; hanya diberikan lewat kabel USB (/api/pair),
    # jadi tidak bisa ditebak dari jaringan seperti kode pendek
    return stored_secret("Rapix-pairing.txt", 32)


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
    # izin sudah terbukti berlaku: cukup dicek ulang sesekali (panel menanyakan status tiap 2 detik)
    ttl = 60 if _blocked_cache["v"] is False else 5
    if time.time() - _blocked_cache["t"] < ttl:
        return _blocked_cache["v"]
    import objc
    result = None
    # autorelease pool: gambar yang ditangkap di thread latar langsung dibebaskan, tidak menumpuk di memori
    with objc.autorelease_pool():
        opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
        windows = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []
        checked = 0
        for w in windows:
            b = w.get("kCGWindowBounds") or {}
            if (w.get("kCGWindowLayer") != 0 or w.get("kCGWindowOwnerPID") == os.getpid()
                    or b.get("Width", 0) < 200 or b.get("Height", 0) < 150 or not w.get("kCGWindowAlpha")):
                continue
            # cukup potongan kecil di tengah jendela, bukan seluruh jendela (bisa belasan MB per jendela Retina)
            pw, ph = min(300, b["Width"]), min(200, b["Height"])
            rect = Quartz.CGRectMake(b["X"] + (b["Width"] - pw) / 2, b["Y"] + (b["Height"] - ph) / 2, pw, ph)
            img = Quartz.CGWindowListCreateImage(
                rect, Quartz.kCGWindowListOptionIncludingWindow, w["kCGWindowNumber"],
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


def via_cable(peer):
    """Tablet tersambung kabel: lewat adb (terlihat sebagai localhost) atau USB tethering (satu jaringan
    dengan alamat kabel Mac). Selain itu dianggap Wi-Fi."""
    if peer in ("127.0.0.1", "::1"):
        return True
    prefix = peer.rsplit(".", 1)[0] + "."
    return any(ip.startswith(prefix) for ip in wired_addresses())


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


# ---------- layar virtual: satu per perangkat ----------
class Screen:
    """Satu layar untuk satu perangkat (tablet/HP): layar virtualnya, perekam videonya, dan klien yang
    menampilkannya. Perangkat pertama jadi layar ke-2, perangkat berikutnya layar ke-3, dan seterusnya."""

    def __init__(self, key, number):
        self.key, self.number = key, number
        self.mon = None           # posisi & ukuran layar di macOS (poin)
        self.display_id = None
        self.vd_proc = None       # proses virtualdisplay; layar hilang saat proses ini berhenti
        self.video = {}           # ws -> VideoClient (H.264)
        self.clients = set()      # ws klien JPEG
        self.pending = 0          # koneksi yang sedang disiapkan (belum masuk video/clients)
        self.wifi_clients = set() # bagian dari clients yang lewat Wi-Fi
        self.stream_proc = self.stream_task = self.stream_mode = None
        self.cond = asyncio.Condition()
        self.frame, self.seq = None, 0
        self.sct = {}             # cache grab_frame: frame terakhir, supaya layar diam tidak dikirim ulang
        self.empty_since = None
        self.video_failed = False # H.264 tidak bisa untuk layar ini: tabletnya memakai JPEG

    @property
    def name(self):
        return "Rapix" if self.number == 1 else f"Rapix {self.number}"

    @property
    def label(self):  # layar bawaan MacBook = layar ke-1
        return f"Layar ke-{self.number + 1}"

    @property
    def connected(self):
        # "pending": perangkat yang sedang disiapkan layarnya ikut dihitung, supaya layarnya tidak diambil
        # perangkat lain atau dilepas sebelum selesai dibuat
        return len(self.video) + len(self.clients) + self.pending

    @property
    def uses_wifi(self):
        return bool(self.wifi_clients) or any(c.wifi for c in self.video.values())


def device_key(app, info, peer):
    """Perangkat dikenali dari ID yang dikirim aplikasi/browser tablet (jadi pindah kabel <-> Wi-Fi tetap
    layar yang sama); tanpa ID, dari alamat IP-nya. Mode cermin/--monitor: semua perangkat berbagi satu layar."""
    cfg = app["cfg"]
    if cfg.monitor is not None or cfg.mirror or not (IS_MAC and HELPER.exists()):
        return "bersama"
    return str(info.get("device") or "")[:64] or peer


def get_screen(app, key):
    """Layar milik perangkat ini; dibuat (dengan nomor berikutnya) bila belum ada."""
    screens = app["screens"]
    if key not in screens:
        # yang dihitung hanya layar yang perangkatnya sedang tersambung: nomor terkecil yang belum dipakai
        # perangkat tersambung. Nomor lama perangkat ini diutamakan bila kosong (posisinya di Arrange ikut diingat).
        remembered = load_settings().get("screens", {})
        busy = {s.number for s in screens.values() if s.connected}
        number = remembered.get(key)
        if not number or number in busy:
            number = next(n for n in range(1, 100) if n not in busy)
        for old in [s for s in screens.values() if s.number == number and not s.connected]:
            close_screen(app, old)  # layar perangkat yang sudah pergi memberi nomornya ke perangkat baru
        screens[key] = Screen(key, number)
        if remembered.get(key) != number:
            kept = {k: n for k, n in remembered.items() if k != key}
            kept[key] = number
            save_settings(screens=dict(list(kept.items())[-8:]))  # cukup 8 perangkat terakhir
    return screens[key]


async def create_virtual_display(app, scr, w, h, hidpi):
    before = {mon_key(m) for m in monitors()}
    # nomor seri = nomor layar, supaya macOS mengingat posisinya di Arrange. Jika macOS menolak (layar lama
    # dengan nomor seri sama belum benar-benar hilang), coba lagi sebentar kemudian dengan nomor seri lain.
    for attempt, serial in enumerate((scr.number, scr.number, 100 + scr.number)):
        if attempt:
            await asyncio.sleep(1)
        proc = await asyncio.create_subprocess_exec(
            str(HELPER), str(w), str(h), "1" if hidpi else "0", str(app["cfg"].hz), str(serial), scr.name,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), 10)
        except asyncio.TimeoutError:
            line = b""
        if line.startswith(b"READY"):
            break
        err = (await proc.stderr.read()).decode().strip()
    else:
        raise RuntimeError(err or "Layar virtual gagal dibuat")
    scr.vd_proc = proc
    scr.display_id = int(line.split()[1])

    for _ in range(50):  # tunggu macOS memasang layar baru (maks 10 detik)
        await asyncio.sleep(0.2)
        new = [m for m in monitors() if mon_key(m) not in before]
        if new:
            return new[0]
    raise RuntimeError("Layar virtual dibuat tapi tidak terdeteksi macOS")


async def setup_display(app, scr, info):
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
    print(f"Membuat {scr.label.lower()} {w}x{h}{' (Retina)' if hidpi else ''}…")
    mon = await create_virtual_display(app, scr, w, h, hidpi)
    print(f"{scr.label} aktif. Atur posisinya di System Settings → Displays → Arrange.")
    return mon


def close_screen(app, scr):
    for proc in (scr.stream_proc, scr.vd_proc):
        if proc and proc.returncode is None:
            proc.terminate()  # layar virtual ikut hilang
    app["screens"].pop(scr.key, None)


async def screen_reaper(app):
    """Layar yang perangkatnya pergi lebih dari 60 detik dilepas, selama masih ada layar Rapix lain
    (layar terakhir tetap ada, seperti sebelumnya). Jeda ini memberi waktu tablet pindah kabel <-> Wi-Fi."""
    while True:
        await asyncio.sleep(10)
        now = time.time()
        for scr in list(app["screens"].values()):
            if scr.connected:
                scr.empty_since = None
            elif scr.empty_since is None:
                scr.empty_since = now
            elif len(app["screens"]) > 1 and now - scr.empty_since > 60:
                print(f"{scr.label} dilepas (perangkat tidak tersambung).")
                close_screen(app, scr)


# ---------- tangkap layar ----------
_sct = {}
# kursor panah (dalam poin layar): tepi putih, isi hitam
CURSOR_EDGE = [(0, 0), (0, 17), (4.5, 13), (7.5, 19.5), (10, 18.5), (7, 12.5), (12.5, 12.5)]
CURSOR_FILL = [(1.4, 3.3), (1.4, 13.6), (4.9, 10.6), (7.9, 17.6), (8.7, 17.3), (5.7, 11.1), (9.2, 11.1)]


def grab_frame(mon, max_width, quality, cache):
    if "s" not in _sct:
        _sct["s"] = mss.mss()
    shot = _sct["s"].grab(mon)
    cx, cy = pyautogui.position()

    # layar tidak berubah -> tidak perlu encode dan kirim ulang. Dibandingkan lewat checksum langsung dari
    # buffer mss (tanpa salinan): menyimpan & membandingkan salinan frame Retina (~16 MB) per layar boros memori
    raw = shot.raw
    crc = zlib.crc32(raw)
    if crc == cache.get("crc") and (cx, cy) == cache.get("cur"):
        return None
    cache["crc"], cache["cur"] = crc, (cx, cy)

    img = Image.frombuffer("RGB", shot.size, raw, "raw", "BGRX", 0, 1)

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
    cfg = app["cfg"]
    loop = asyncio.get_running_loop()
    pool = ThreadPoolExecutor(max_workers=1)
    interval = 1.0 / min(cfg.fps, 30)  # JPEG Retina per frame tidak sanggup lebih dari ~30 fps
    while True:
        t0 = time.perf_counter()
        for scr in list(app["screens"].values()):
            if not scr.mon:
                continue
            if scr.video:
                refresh_monitor(scr)  # tablet video tetap butuh posisi layar terbaru untuk sentuhan
            if not scr.clients:
                continue
            try:
                refresh_monitor(scr)
                data = await loop.run_in_executor(pool, grab_frame, scr.mon, cfg.max_width, cfg.quality, scr.sct)
                if data is not None:
                    async with scr.cond:
                        scr.frame = data
                        scr.seq += 1
                        scr.cond.notify_all()
            except Exception as e:
                print("Gagal menangkap layar (cek izin Screen Recording):", e)
                await asyncio.sleep(2)
        await asyncio.sleep(max(0.0, interval - (time.perf_counter() - t0)))


# ---------- video H.264 ----------
class VideoClient:
    """Antrean frame video untuk satu tablet. Frame delta tidak boleh dibuang satu-satu (gambar rusak),
    jadi kalau tablet tertinggal antreannya dikosongkan lalu menunggu keyframe berikutnya."""

    def __init__(self, wifi):
        # ~0,5 detik: lebih dari itu tablet tertinggal jauh; lebih baik loncat ke keyframe baru daripada
        # menumpuk video lama di memori (terasa saat banyak layar atau Wi-Fi lambat)
        self.queue = asyncio.Queue(maxsize=30)
        self.need_key = True
        self.wifi = wifi  # lewat Wi-Fi (bukan kabel USB)


def wanted_mode(app, scr, extra_wifi=False):
    """(fps, bitrate). Lewat kabel tablet ikut terisi daya: refresh tinggi dan kualitas penuh.
    Ada tablet lewat Wi-Fi: 60 fps dan bitrate lebih rendah, karena radio Wi-Fi termasuk pemakan baterai terbesar."""
    cfg = app["cfg"]
    if extra_wifi or any(c.wifi for c in scr.video.values()):
        return cfg.wifi_hz, cfg.wifi_bitrate
    return cfg.hz, cfg.bitrate


def set_refresh(scr, hz):
    """Ganti refresh rate layar virtual (mode 60 Hz dan refresh tinggi sudah disiapkan virtualdisplay)."""
    import Quartz
    if not scr.display_id or not scr.mon:
        return
    cur = Quartz.CGDisplayCopyDisplayMode(scr.display_id)
    if not cur or abs(Quartz.CGDisplayModeGetRefreshRate(cur) - hz) < 1:
        return
    opts = {Quartz.kCGDisplayShowDuplicateLowResolutionModes: True}
    for m in Quartz.CGDisplayCopyAllDisplayModes(scr.display_id, opts) or []:
        same_size = (Quartz.CGDisplayModeGetWidth(m) == scr.mon["width"]
                     and Quartz.CGDisplayModeGetPixelWidth(m) == Quartz.CGDisplayModeGetPixelWidth(cur))
        if same_size and abs(Quartz.CGDisplayModeGetRefreshRate(m) - hz) < 1:
            err, cfg = Quartz.CGBeginDisplayConfiguration(None)
            Quartz.CGConfigureDisplayWithDisplayMode(cfg, scr.display_id, m, None)
            Quartz.CGCompleteDisplayConfiguration(cfg, Quartz.kCGConfigurePermanently)
            print(f"{scr.label} {hz} Hz.")
            return


def restart_stream_if_needed(app, scr):
    proc = scr.stream_proc
    if proc and proc.returncode is None and scr.video and scr.stream_mode != wanted_mode(app, scr):
        proc.terminate()  # stream_loop menjalankan ulang dengan fps/bitrate baru


def video_available(app, scr, info):
    return (IS_MAC and STREAMER.exists() and scr.display_id and info.get("h264")
            and not app["cfg"].jpeg and not scr.video_failed)


def request_keyframe(scr):
    proc = scr.stream_proc
    if proc and proc.returncode is None:
        try:
            proc.stdin.write(b"k\n")
        except (BrokenPipeError, ConnectionResetError):
            pass


async def stream_loop(app, scr):
    """Menjalankan screenstream untuk satu layar selama ada tablet video, dan meneruskan frame ke tablet itu."""
    fails, last_fail = 0, 0.0
    while scr.video:
        scr.stream_mode = fps, bitrate = wanted_mode(app, scr)
        if IS_MAC:
            set_refresh(scr, fps)
        proc = await asyncio.create_subprocess_exec(
            str(STREAMER), str(scr.display_id), str(fps), str(bitrate),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        scr.stream_proc = proc
        for c in scr.video.values():
            c.need_key = True
        try:
            while True:
                head = await proc.stdout.readexactly(4)
                packet = await proc.stdout.readexactly(int.from_bytes(head, "big"))
                key = packet[0] == 1
                for c in list(scr.video.values()):
                    if c.need_key and not key:
                        continue
                    try:
                        c.queue.put_nowait(packet)
                        c.need_key = False
                    except asyncio.QueueFull:
                        while not c.queue.empty():
                            c.queue.get_nowait()
                        c.need_key = True
                        request_keyframe(scr)
        except asyncio.IncompleteReadError:
            pass
        finally:
            if proc.returncode is None:
                proc.terminate()
        code = await proc.wait()
        if code == 3:  # resolusi layar berubah: jalankan ulang dengan ukuran baru
            continue
        if scr.key not in app["screens"] or not scr.vd_proc or scr.vd_proc.returncode is not None:
            break  # layar ini sudah dilepas: perekamnya memang berhenti
        err = (await proc.stderr.read()).decode().strip().splitlines()
        if scr.video and code not in (0, -15):
            fails = fails + 1 if time.time() - last_fail < 30 else 1
            last_fail = time.time()
            reason = (err[-1] if err else "") or f"kode {code}"
            if fails < 3:  # gangguan sesaat (mis. layar sedang diatur macOS): coba lagi
                print(f"Video {scr.label.lower()} berhenti ({reason}); mencoba lagi…")
                await asyncio.sleep(1)
                continue
            # tetap gagal: hanya layar ini yang pindah ke JPEG, layar lain tetap video
            print(f"Video H.264 {scr.label.lower()} gagal ({reason}); pindah ke mode gambar JPEG.")
            scr.video_failed = True
            for ws in list(scr.video):
                await ws.close()  # tablet menyambung ulang dan otomatis memakai JPEG
            break
    scr.stream_proc = None
    scr.stream_task = None


def refresh_monitor(scr):
    # macOS bisa memindahkan layar virtual setelah dibuat (posisi tersimpan, atau diatur di Arrange);
    # baca ulang posisinya dari ID layar agar gambar dan sentuhan selalu ke tempat yang benar
    if not scr.display_id:
        return
    import Quartz
    r = Quartz.CGDisplayBounds(scr.display_id)
    if r.size.width:
        scr.mon = {"left": int(r.origin.x), "top": int(r.origin.y),
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
# kode akses pendek mudah ditebak berulang-ulang dari Wi-Fi yang sama: setelah beberapa kali salah,
# alamat itu diblokir sebentar
FAILS = {}  # ip -> [jumlah salah, waktu mulai]
MAX_FAILS, FAIL_WINDOW, BLOCK_SECONDS = 8, 60, 300
REFUSED = {}  # ip -> waktu terakhir dicatat ditolak karena batas layar Wi-Fi


def blocked(ip):
    n, since = FAILS.get(ip, (0, 0))
    return n >= MAX_FAILS and time.time() - since < BLOCK_SECONDS


def code_ok(request, given, allow_token=False):
    """Kode benar (atau kunci pairing aplikasi tablet); tebakan salah dihitung per alamat IP."""
    import hmac
    app, ip = request.app, request.remote
    if blocked(ip):
        raise web.HTTPTooManyRequests(text="Terlalu banyak kode salah. Coba lagi dalam beberapa menit.")
    ok = hmac.compare_digest(given, app["code"]) or (allow_token and hmac.compare_digest(given, app["token"]))
    if ok:
        FAILS.pop(ip, None)
    elif given.isalnum():  # abaikan permintaan otomatis browser seperti /favicon.ico
        n, since = FAILS.get(ip, (0, time.time()))
        FAILS[ip] = (1, time.time()) if time.time() - since > FAIL_WINDOW else (n + 1, since)
        if FAILS[ip][0] == MAX_FAILS:
            print(f"Alamat {ip} diblokir {BLOCK_SECONDS // 60} menit karena kode salah berulang.")
    return ok


async def index(request):
    if not code_ok(request, request.match_info["code"]):
        raise web.HTTPNotFound(text="Alamat salah. Pakai alamat lengkap (dengan kode) dari panel Rapix.")
    return web.FileResponse(STATIC / "index.html")


async def home(request):
    if request.remote in ("127.0.0.1", "::1"):
        raise web.HTTPFound("/panel")
    return web.Response(text="Pakai alamat lengkap (dengan kode) dari panel Rapix.")


async def ws_handler(request):
    app = request.app
    if not code_ok(request, request.query.get("k", ""), allow_token=True):
        raise web.HTTPForbidden()
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=0)
    try:
        await ws.prepare(request)
    except ConnectionResetError:
        return ws  # perangkat memutus di tengah jabat tangan (mis. layar HP terkunci): abaikan
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

    wifi = not via_cable(peer)
    key = device_key(app, info, peer)
    # aturan: lewat kabel boleh banyak layar tambahan; lewat Wi-Fi hanya satu (di luar layar milik perangkat ini).
    # Dicek sebelum layar dibuat, jadi perangkat yang ditolak tidak mendapat (dan tidak menyimpan) nomor layar.
    if wifi and any(s.key != key and s.uses_wifi for s in app["screens"].values()):
        if time.time() - REFUSED.get(peer, 0) > 60:  # perangkat terus mencoba ulang: cukup dicatat sekali per menit
            print(f"Tablet {peer} ditolak: lewat Wi-Fi hanya 1 layar tambahan.")
            REFUSED[peer] = time.time()
        await ws.send_json({"t": "error", "msg": "Lewat Wi-Fi hanya 1 layar tambahan, dan sedang dipakai perangkat lain. "
                                                 "Colok kabel USB untuk layar berikutnya."})
        await ws.close()
        return ws
    scr = get_screen(app, key)
    scr.pending += 1  # sejak sekarang layar ini milik perangkat ini, walau belum selesai disiapkan
    error = None
    try:
        async with app["setup_lock"]:  # satu per satu: layar baru dikenali dengan membandingkan daftar layar
            if scr.mon is None:
                try:
                    scr.mon = await setup_display(app, scr, info)
                except Exception as e:
                    error = str(e)
        if not error:
            video = video_available(app, scr, info)
            who = f"{peer} → {scr.label.lower()}" if len(app["screens"]) > 1 else peer
            print(f"Tablet terhubung: {who}" + (" (video H.264)" if video else " (gambar JPEG)"))
            # fps yang akan dipakai setelah tablet ini bergabung; tablet meminta refresh layar yang sama
            fps = wanted_mode(app, scr, extra_wifi=wifi)[0] if video else app["cfg"].fps
            await ws.send_json({"t": "hello", "w": scr.mon["width"], "h": scr.mon["height"],
                                "codec": "h264" if video else "jpeg", "fps": fps, "screen": scr.label})
    finally:
        scr.pending -= 1  # tanpa await sampai klien terdaftar di bawah, jadi tidak ada celah
    if error:
        print("Error:", error)
        if not scr.connected:
            close_screen(app, scr)
        await ws.send_json({"t": "error", "msg": error})
        await ws.close()
        return ws
    # maks 2 frame dalam perjalanan: tablet menampilkan satu sambil menerima berikutnya
    ready = asyncio.Semaphore(2)

    async def video_sender(client):
        try:
            while not ws.closed:
                await ws.send_bytes(await client.queue.get())
        except (ConnectionResetError, asyncio.CancelledError):
            pass

    if video:
        client = VideoClient(wifi=wifi)
        scr.video[ws] = client
        if not scr.stream_task:
            scr.stream_task = asyncio.create_task(stream_loop(app, scr))
        restart_stream_if_needed(app, scr)
        request_keyframe(scr)
    else:
        scr.clients.add(ws)
        if wifi:
            scr.wifi_clients.add(ws)

    async def sender():
        last = 0
        try:
            while not ws.closed:
                await ready.acquire()
                async with scr.cond:
                    await scr.cond.wait_for(lambda: scr.seq != last)
                    data, last = scr.frame, scr.seq
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
                    request_keyframe(scr)
            elif d.get("t") != "screen":
                await loop.run_in_executor(app["input_pool"], handle_input, scr.mon, d)
    finally:
        scr.clients.discard(ws)
        scr.wifi_clients.discard(ws)
        scr.video.pop(ws, None)
        if not scr.video and scr.stream_proc and scr.stream_proc.returncode is None:
            scr.stream_proc.terminate()  # tidak ada tablet video lagi di layar ini: hentikan perekamnya
        restart_stream_if_needed(app, scr)  # mis. tablet Wi-Fi pindah ke kabel: kembali ke bitrate penuh
        task.cancel()
        print(f"Tablet terputus: {who}")
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
async def api_pair(request):
    """Aplikasi tablet yang tersambung kabel USB (dianggap lokal) mengambil kunci dan alamat Wi-Fi Mac."""
    app = request.app
    return web.json_response({"token": app["token"], "port": app["cfg"].port,
                              "hosts": [h for h in (local_ip(), socket.gethostname()) if h and h != "127.0.0.1"]})


@local_only
async def api_status(request):
    app = request.app
    screens = sorted(app["screens"].values(), key=lambda s: s.number)
    first = next((s for s in screens if s.mon), None)

    def links(s):
        return ["wifi" if c.wifi else "usb" for c in s.video.values()] + ["browser"] * len(s.clients)

    def stream(s):
        running = s.stream_proc and s.stream_proc.returncode is None
        return {"fps": s.stream_mode[0], "mbps": s.stream_mode[1]} if running else None

    return web.json_response({
        **connection_info(app),
        "clients": sum(s.connected for s in screens),
        "codec": "h264" if any(s.video for s in screens) else ("jpeg" if any(s.clients for s in screens) else None),
        "display": f"{first.mon['width']}x{first.mon['height']}" if first else None,
        "virtual": any(s.vd_proc for s in screens),
        # satu baris per perangkat: layar ke berapa, lewat apa, dan kualitas videonya
        "screens": [{"label": s.label, "name": s.name, "connected": s.connected, "links": links(s),
                     "display": f"{s.mon['width']}x{s.mon['height']}" if s.mon else None, "stream": stream(s)}
                    for s in screens],
        "os": "mac" if IS_MAC else "windows" if IS_WIN else "other",
        "perms": mac_permissions(),
        "perms_at_start": app["perms_at_start"],
        "can_restart": bool(app_bundle()),
        "restarting": app.get("restarting", False),
        "login": login_item() if app_bundle() else None,
        "log": list(LOG.lines)[-15:],
        "links": [l for s in screens for l in links(s)],
        "stream": next((stream(s) for s in screens if stream(s)), None),
        "version": app_version(),
    })


def app_version():
    bundle = app_bundle()
    if not bundle:
        return None
    import plistlib
    try:
        with open(bundle / "Contents" / "Info.plist", "rb") as f:
            return plistlib.load(f).get("CFBundleShortVersionString")
    except OSError:
        return None


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
async def api_show(request):
    """Rapix dibuka lagi (mis. dari Launchpad) saat sudah berjalan: tampilkan jendela panelnya."""
    show_panel(f"http://localhost:{request.app['cfg'].port}/panel")
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
    for scr in list(app["screens"].values()):
        close_screen(app, scr)  # semua layar virtual ikut hilang
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
    # dan adb meneruskannya lewat kabel ke MacBook.
    # Tidak mengecek berulang-ulang: "adb track-devices" memberi kabar sendiri saat kabel dicolok/dicabut.
    known, warned = set(), set()
    while True:
        proc = await asyncio.create_subprocess_exec(
            "adb", "track-devices", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            while True:  # setiap kabar: 4 digit heksa panjang, lalu daftar "serial<TAB>status"
                size = int(await proc.stdout.readexactly(4), 16)
                listing = (await proc.stdout.readexactly(size)).decode(errors="replace")
                known = await usb_devices_changed(app, listing, known, warned)
        except (asyncio.IncompleteReadError, ValueError):
            pass  # server adb dimulai ulang: sambungkan lagi
        finally:
            if proc.returncode is None:
                proc.terminate()
        await asyncio.sleep(1)


async def usb_devices_changed(app, listing, known, warned):
    port = app["cfg"].port
    now = set()
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        if state == "unauthorized" and serial not in warned:
            warned.add(serial)
            print("Tablet USB terdeteksi: ketuk 'Allow/Izinkan' pada pertanyaan USB debugging di tablet.")
        if state != "device" or ":" in serial:  # "IP:port" = adb lewat Wi-Fi, bukan kabel
            continue
        now.add(serial)
        if serial in known:
            continue
        code, msg = await adb("-s", serial, "reverse", f"tcp:{port}", f"tcp:{port}")
        if code != 0:
            print("Gagal menyiapkan kabel USB:", msg.strip())
            continue
        # aplikasi Rapix terpasang: buka aplikasinya (video lebih lancar); jika tidak, buka browser
        _, installed = await adb("-s", serial, "shell", "pm", "path", "id.rap.rapix")
        if installed.strip():
            await adb("-s", serial, "shell", "am", "start", "-n", "id.rap.rapix/.MainActivity")
            print("Tablet tersambung lewat kabel USB. Aplikasi Rapix dibuka di tablet.")
        else:
            await adb("-s", serial, "shell", "am", "start", "-a", "android.intent.action.VIEW",
                      "-d", app["usb_url"])
            print(f"Tablet tersambung lewat kabel USB. Di tablet buka: {app['usb_url']}")
    app["adb_devices"] = len(now)
    return now


async def on_startup(app):
    app["setup_lock"] = asyncio.Lock()
    app["capture"] = asyncio.create_task(capture_loop(app))
    app["reaper"] = asyncio.create_task(screen_reaper(app))
    if shutil.which("adb"):
        app["usb"] = asyncio.create_task(usb_loop(app))
    app["perm_watch"] = asyncio.create_task(permission_watch(app))


async def on_cleanup(app):
    app["capture"].cancel()
    if "usb" in app:
        app["usb"].cancel()
    for scr in list(app["screens"].values()):
        close_screen(app, scr)  # semua layar virtual ikut hilang


def main():
    p = argparse.ArgumentParser(description="Rapix: jadikan tablet layar kedua MacBook")
    p.add_argument("--resolution", help="paksa ukuran layar kedua, mis. 1280x800 (default: ikut tablet)")
    p.add_argument("--hidpi", action="store_true", help="pakai mode Retina untuk --resolution")
    p.add_argument("--mirror", action="store_true", help="cerminkan layar utama, tanpa layar kedua")
    p.add_argument("--monitor", type=int, help="pakai layar yang sudah ada (nomor dari --list)")
    p.add_argument("--list", action="store_true", help="tampilkan daftar layar lalu keluar")
    p.add_argument("--port", type=int, default=8765)
    # lewat kabel tablet terisi daya, jadi boleh refresh tinggi; lewat Wi-Fi 60 Hz agar hemat baterai
    p.add_argument("--hz", type=int, default=90, help="refresh rate layar kedua lewat kabel USB (bawaan 90)")
    p.add_argument("--wifi-hz", type=int, default=60, help="refresh rate layar kedua lewat Wi-Fi (bawaan 60)")
    p.add_argument("--fps", type=int, help="frame per detik mode JPEG (praktis maks ~30)")
    p.add_argument("--bitrate", type=int, default=20, help="bitrate video H.264 lewat kabel USB, dalam Mbps")
    p.add_argument("--wifi-bitrate", type=int, default=12, help="bitrate video H.264 lewat Wi-Fi, dalam Mbps")
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
            import urllib.request
            try:  # minta Rapix yang berjalan membuka jendela panelnya sendiri
                urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{cfg.port}/api/show", method="POST"),
                                       timeout=3)
            except OSError:
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
    app["token"] = pairing_token()
    app["perms_at_start"] = mac_permissions(prompt=True)
    app["cfg"] = cfg
    app["screens"] = {}  # ID perangkat -> Screen (layar ke-2, ke-3, ...)
    app["input_pool"] = ThreadPoolExecutor(max_workers=1)
    app["connection"] = load_settings().get("connection", "wifi")
    app["usb_url"] = f"http://localhost:{cfg.port}/{code}" if shutil.which("adb") else None
    app.router.add_get("/", home)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/panel", panel)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/pair", api_pair)
    app.router.add_post("/api/settings/{which}", api_settings)
    app.router.add_post("/api/quit", api_quit)
    app.router.add_post("/api/show", api_show)
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
            asyncio.get_running_loop().call_later(0.5, show_panel, panel_url)
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


PANEL = {"show": None}  # diisi run_menu_bar: menampilkan jendela panel native


def show_panel(panel_url):
    """Tampilkan panel: jendela Rapix sendiri saat berjalan sebagai aplikasi Mac, selain itu browser.
    Aman dipanggil dari thread mana pun (jendela macOS hanya boleh disentuh dari thread utama)."""
    if PANEL["show"]:
        from PyObjCTools import AppHelper
        AppHelper.callAfter(PANEL["show"])
    else:
        webbrowser.open(panel_url)


def run_menu_bar(panel_url):
    """Ikon Rapix di menu bar dan jendela panel (WebKit, tanpa browser). Juga membuat macOS mengenali
    Rapix sebagai aplikasi yang hidup, sehingga membuka Rapix lagi dari Launchpad cukup menampilkan panelnya."""
    from AppKit import (NSAlert, NSAlertFirstButtonReturn, NSApplication, NSApplicationActivationPolicyAccessory,
                        NSApplicationActivationPolicyRegular, NSBackingStoreBuffered, NSImage, NSMakeRect, NSMenu,
                        NSMenuItem, NSStatusBar, NSVariableStatusItemLength, NSWindow,
                        NSWindowStyleMaskClosable, NSWindowStyleMaskMiniaturizable, NSWindowStyleMaskResizable,
                        NSWindowStyleMaskTitled)
    from Foundation import NSURL, NSObject, NSURLRequest
    from WebKit import WKWebView, WKWebViewConfiguration

    nsapp = NSApplication.sharedApplication()

    class Delegate(NSObject):
        window = None

        def applicationShouldHandleReopen_hasVisibleWindows_(self, _app, _flag):
            self.openPanel_(None)
            return False

        def openPanel_(self, _sender):
            if self.window is None:
                self.window = self.makeWindow()
            # selama jendela terbuka Rapix tampil di Dock dan Cmd+Tab seperti aplikasi biasa
            nsapp.setActivationPolicy_(NSApplicationActivationPolicyRegular)
            nsapp.activateIgnoringOtherApps_(True)
            self.window.makeKeyAndOrderFront_(None)

        def makeWindow(self):
            style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                     | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)
            win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, 920, 720), style, NSBackingStoreBuffered, False)
            win.setTitle_("Rapix")
            win.setMinSize_((420, 480))
            win.setReleasedWhenClosed_(False)  # ditutup = disembunyikan; dibuka lagi tanpa memuat ulang
            win.setDelegate_(self)
            if not win.setFrameUsingName_("RapixPanel"):
                win.center()
            win.setFrameAutosaveName_("RapixPanel")  # ukuran & posisi jendela diingat
            web = WKWebView.alloc().initWithFrame_configuration_(win.contentView().bounds(),
                                                                 WKWebViewConfiguration.alloc().init())
            web.setAutoresizingMask_(18)  # lebar & tinggi ikut jendela
            web.setUIDelegate_(self)
            web.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(panel_url)))
            win.contentView().addSubview_(web)
            self.web = web
            return win

        def windowWillClose_(self, _note):
            nsapp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)  # kembali hanya di menu bar

        # dialog JavaScript panel (mis. konfirmasi "Hentikan Rapix") sebagai dialog macOS
        def webView_runJavaScriptConfirmPanelWithMessage_initiatedByFrame_completionHandler_(
                self, _web, message, _frame, handler):
            alert = NSAlert.alloc().init()
            alert.setMessageText_(message)
            alert.addButtonWithTitle_("OK")
            alert.addButtonWithTitle_("Batal")
            handler(alert.runModal() == NSAlertFirstButtonReturn)

        def webView_runJavaScriptAlertPanelWithMessage_initiatedByFrame_completionHandler_(
                self, _web, message, _frame, handler):
            alert = NSAlert.alloc().init()
            alert.setMessageText_(message)
            alert.runModal()
            handler()

        def quit_(self, _sender):
            print("Berhenti dari menu bar.")
            os._exit(0)  # layar virtual ikut hilang karena programnya berhenti saat Rapix tertutup

    nsapp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    delegate = Delegate.alloc().init()
    nsapp.setDelegate_(delegate)
    PANEL["show"] = lambda: delegate.openPanel_(None)

    def menu_item(menu, title, action, key="", target=delegate):
        mi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
        if target is not None:
            mi.setTarget_(target)
        menu.addItem_(mi)

    # menu aplikasi saat jendela aktif: Cmd+W menutup jendela, Cmd+C menyalin (alamat, log)
    main = NSMenu.alloc().init()
    for title, items in (("Rapix", (("Sembunyikan Rapix", "hide:", "h", None), (None,) * 4,
                                    ("Hentikan Rapix", "quit:", "", delegate))),
                         ("Edit", (("Salin", "copy:", "c", None), ("Pilih Semua", "selectAll:", "a", None))),
                         ("Jendela", (("Tutup", "performClose:", "w", None), ("Kecilkan", "performMiniaturize:", "m", None)))):
        top = NSMenuItem.alloc().init()
        sub = NSMenu.alloc().initWithTitle_(title)
        for t, action, key, target in items:
            if t is None:
                sub.addItem_(NSMenuItem.separatorItem())
            else:
                menu_item(sub, t, action, key, target)
        top.setSubmenu_(sub)
        main.addItem_(top)
    nsapp.setMainMenu_(main)

    item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
    # logo Rapix sebagai gambar template: macOS mewarnainya sendiri untuk menu bar terang/gelap
    icon = None
    if (BUNDLE / "menubar.png").exists():
        icon = NSImage.alloc().initWithContentsOfFile_(str(BUNDLE / "menubar.png"))
        icon.setSize_((18, 18))
        icon.setTemplate_(True)
    icon = icon or NSImage.imageWithSystemSymbolName_accessibilityDescription_("rectangle.on.rectangle", "Rapix")
    if icon:
        item.button().setImage_(icon)
    else:
        item.button().setTitle_("Rapix")
    menu = NSMenu.alloc().init()
    menu_item(menu, "Buka panel Rapix", "openPanel:")
    menu.addItem_(NSMenuItem.separatorItem())
    menu_item(menu, "Berhenti", "quit:")
    item.setMenu_(menu)
    run_menu_bar.keep = (delegate, item, menu, main)  # cegah dibersihkan garbage collector
    nsapp.run()


if __name__ == "__main__":
    main()
