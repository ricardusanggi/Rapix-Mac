"""Ubah ukuran / format banyak gambar sekaligus, dikerjakan bersama MacBook dan tablet.

Saat Rapix berjalan dan tablet tersambung, beban dibagi menurut ukuran file: tablet 1/4, Mac 3/4
(atur dengan --porsi-tablet). Tablet perlu kabel USB dan aplikasi Rapix terbuka di tablet.
Jika Rapix tidak berjalan, tablet tidak ada, atau tablet gagal pada satu file, Mac yang mengerjakan.

Contoh:
    .venv/bin/python tools/bagi_gambar.py ~/Pictures/Liburan ~/Desktop/kecil --lebar 1920 --format webp
"""
import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

PORT = 8766
EXT_IN = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".bmp", ".gif", ".tif", ".tiff"}
EXT_OUT = {"jpeg": ".jpg", "webp": ".webp", "png": ".png"}


def adb():
    return shutil.which("adb") or next((p for p in ("/opt/homebrew/bin/adb", "/usr/local/bin/adb") if Path(p).exists()), None)


def rapix_running():
    try:
        urllib.request.urlopen("http://127.0.0.1:8765/api/status", timeout=2).close()
        return True
    except OSError:
        return False


def connect_tablet():
    """Sambungkan port worker tablet ke Mac lewat USB; kembalikan jumlah inti tablet, atau 0."""
    tool = adb()
    if not tool:
        return 0
    if subprocess.run([tool, "forward", f"tcp:{PORT}", f"tcp:{PORT}"], capture_output=True).returncode != 0:
        return 0
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/info", timeout=3) as r:
            return int(json.load(r)["cores"])
    except (OSError, ValueError, KeyError):
        return 0


def convert_on_mac(src, dst, width, fmt, quality):
    """Dijalankan di proses terpisah supaya semua inti Mac terpakai."""
    from PIL import Image, ImageOps
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        if width and im.width > width:
            im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
        if fmt == "jpeg" and im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        dst.parent.mkdir(parents=True, exist_ok=True)
        im.save(dst, "JPEG" if fmt == "jpeg" else fmt.upper(), quality=quality)


def convert_on_tablet(src, dst, width, fmt, quality):
    url = f"http://127.0.0.1:{PORT}/gambar?lebar={width}&format={fmt}&kualitas={quality}"
    req = urllib.request.Request(url, data=src.read_bytes(), method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(data)


def main():
    p = argparse.ArgumentParser(description="Ubah ukuran/format gambar bersama MacBook + tablet")
    p.add_argument("masuk", type=Path, help="folder (atau satu file) gambar asal")
    p.add_argument("keluar", type=Path, help="folder hasil")
    p.add_argument("--lebar", type=int, default=0, help="lebar maksimum dalam piksel (0 = ukuran asli)")
    p.add_argument("--format", choices=EXT_OUT, default="jpeg")
    p.add_argument("--kualitas", type=int, default=80)
    # 1/4: terukur paling cepat untuk M4 + Kirin T90A (tablet ~3x lebih lambat, jadi keduanya selesai bersamaan)
    p.add_argument("--porsi-tablet", type=float, default=1 / 4,
                   help="bagian beban untuk tablet, menurut ukuran file (bawaan 1/4; sisanya Mac)")
    p.add_argument("--tanpa-tablet", action="store_true", help="kerjakan di Mac saja (untuk perbandingan)")
    p.add_argument("--tanpa-mac", action="store_true", help="kerjakan di tablet saja (untuk perbandingan)")
    a = p.parse_args()

    base = a.masuk if a.masuk.is_dir() else a.masuk.parent
    files = [f for f in (sorted(a.masuk.rglob("*")) if a.masuk.is_dir() else [a.masuk])
             if f.suffix.lower() in EXT_IN and not f.name.startswith(".")]
    if not files:
        sys.exit("Tidak ada gambar di " + str(a.masuk))

    tablet_cores = 0 if a.tanpa_tablet or not rapix_running() else connect_tablet()
    mac_cores = 0 if a.tanpa_mac else (os.cpu_count() or 4)
    if not tablet_cores and not mac_cores:
        sys.exit("Tablet tidak terjangkau. Jalankan Rapix, colok kabel USB, dan buka aplikasi Rapix di tablet.")
    share = 1.0 if not mac_cores else (min(max(a.porsi_tablet, 0.0), 1.0) if tablet_cores else 0.0)

    # bagi menurut ukuran file: setiap file diberikan ke perangkat yang paling jauh di bawah porsinya,
    # jadi file besar dan kecil tersebar rata di keduanya
    tablet_jobs, mac_jobs = queue.Queue(), queue.Queue()
    sizes = {"tablet": 0, "mac": 0}
    for f in sorted(files, key=lambda f: f.stat().st_size, reverse=True):
        total = sizes["tablet"] + sizes["mac"] + f.stat().st_size
        to_tablet = share * total - sizes["tablet"] > (1 - share) * total - sizes["mac"]
        (tablet_jobs if to_tablet else mac_jobs).put(f)
        sizes["tablet" if to_tablet else "mac"] += f.stat().st_size
    total = sizes["tablet"] + sizes["mac"]
    print(f"{len(files)} gambar. Mac: {mac_cores} proses, {sizes['mac'] / total:.0%} beban" +
          (f"; tablet: {tablet_cores} proses, {sizes['tablet'] / total:.0%} beban" if tablet_cores
           else "; tablet: tidak dipakai" + ("" if a.tanpa_tablet else " (Rapix/tablet tidak terdeteksi)")))

    done = {"mac": 0, "tablet": 0, "gagal": 0}
    lock = threading.Lock()
    retry_on_mac = queue.Queue()  # file yang gagal di tablet
    target = lambda f: a.keluar / f.relative_to(base).with_suffix(EXT_OUT[a.format])

    def finish(who):
        with lock:
            done[who] += 1
            n = done["mac"] + done["tablet"] + done["gagal"]
            print(f"\r  {n}/{len(files)}  (Mac {done['mac']}, tablet {done['tablet']})", end="", flush=True)

    def mac_loop(pool):
        while True:
            try:
                f = retry_on_mac.get_nowait()
            except queue.Empty:
                try:
                    f = mac_jobs.get_nowait()
                except queue.Empty:
                    return
            try:
                pool.submit(convert_on_mac, f, target(f), a.lebar, a.format, a.kualitas).result()
                finish("mac")
            except Exception as e:
                print(f"\n  gagal {f.name}: {e}")
                finish("gagal")

    def tablet_loop():
        while True:
            try:
                f = tablet_jobs.get_nowait()
            except queue.Empty:
                return
            try:
                convert_on_tablet(f, target(f), a.lebar, a.format, a.kualitas)
                finish("tablet")
            except Exception:
                if mac_cores:
                    retry_on_mac.put(f)  # Mac yang mengerjakan
                else:
                    finish("gagal")

    t0 = time.perf_counter()
    with ProcessPoolExecutor(max_workers=mac_cores or 1) as pool:
        threads = [threading.Thread(target=mac_loop, args=(pool,)) for _ in range(mac_cores)]
        threads += [threading.Thread(target=tablet_loop) for _ in range(tablet_cores)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if mac_cores:  # sisa file yang gagal di tablet setelah antrean utama habis
            mac_loop(pool)
    dt = time.perf_counter() - t0
    print(f"\nSelesai dalam {dt:.1f} detik ({len(files) / dt:.1f} gambar/detik). "
          f"Mac {done['mac']}, tablet {done['tablet']}" + (f", gagal {done['gagal']}" if done["gagal"] else ""))
    print("Hasil di", a.keluar)


if __name__ == "__main__":
    main()
