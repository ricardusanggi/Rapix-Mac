# Rapix

**Setiap piksel sampai ke tablet.** Rapix menjadikan tablet (Android, iPad, atau tablet apa pun yang punya browser) sebagai **layar kedua MacBook**. Saat tablet tersambung, macOS mendapat monitor baru seukuran layar tablet, jadi jendela bisa diseret ke sana seperti monitor sungguhan.

Tablet tidak perlu memasang aplikasi apa pun, cukup browser.

Dibuat oleh **RAP**.

## Fitur

- **Layar extended sungguhan**, bukan sekadar cermin: Rapix membuat monitor virtual di macOS, dan posisinya bisa diatur di System Settings → Displays → Arrange.
- **Sentuhan jadi mouse**:
  - ketuk = klik, ketuk dua kali = klik ganda
  - geser = seret, tahan = klik kanan
  - dua jari = gulir
  - stylus menekan langsung
- **Tersambung lewat Wi-Fi**, atau kabel USB (USB tethering, atau `adb` untuk Android).
- **Kode akses** di alamat tablet, supaya orang lain di Wi-Fi yang sama tidak bisa mengendalikan Mac Anda.
- **Panel kontrol** di browser Mac berisi QR, status, panduan izin, dan pilihan buka otomatis saat Mac menyala.
- **Ikon di menu bar** untuk membuka panel atau menghentikan Rapix.
- **Pemasangan sekali klik**: Rapix memindahkan dirinya ke Applications.

## Pasang

1. Unduh `Rapix-…-mac-arm64.dmg` dari halaman [Releases](../../releases).
2. Buka DMG, lalu klik dua kali **Rapix**. Rapix memasang dirinya ke Applications.
3. Jika macOS menolak membukanya, buka **System Settings → Privacy & Security**, klik **Open Anyway**, lalu buka lagi. Ini terjadi karena Rapix tidak ditandatangani dengan akun Apple Developer.
4. Panel Rapix terbuka di browser. Ikuti **Persiapan pertama**: izinkan **Screen Recording** dan **Accessibility** untuk Rapix. Rapix memuat ulang sendiri setelah izin aktif.
5. Di tablet, scan QR di panel, lalu ketuk **Mulai**.

**Kebutuhan:**
- Mac dengan chip Apple (M1 atau lebih baru) dan macOS 11 atau lebih baru.
- Tablet di Wi-Fi yang sama, atau tersambung lewat kabel USB.

## Batasan

- Gambar dikirim sebagai JPEG, jadi ada jeda sekitar 50–150 ms. Cocok untuk dokumen, chat, dan referensi, tetapi kurang ideal untuk video atau game. Wi-Fi 5 GHz atau kabel memberi hasil terbaik.
- DMG rilis hanya untuk Apple Silicon. Untuk Mac Intel, build sendiri di Mac Intel.

## Build dari kode sumber

```bash
./installer/create_signing_cert.sh   # sekali saja: sertifikat tetap agar izin macOS tidak hilang setiap update
./installer/build_mac.sh             # hasil: installer/dist/Rapix-<versi>-mac-<arsitektur>.dmg
```

Rapix juga bisa dijalankan langsung dari Terminal, tanpa build: `./Jalankan.command`.

Rincian build ada di [installer/README.md](installer/README.md). Panduan lengkap untuk pengguna ada di [PANDUAN.md](PANDUAN.md).

## Struktur

| File | Isi |
|---|---|
| `server.py` | Server Rapix: tangkap layar, kirim ke tablet, terima sentuhan, panel, menu bar |
| `virtualdisplay.m` | Pembuat monitor virtual memakai CoreGraphics |
| `static/index.html` | Halaman di tablet |
| `static/panel.html` | Panel kontrol di Mac |
| `installer/` | Skrip build DMG, ikon, dan sertifikat |

## Lisensi

[MIT](LICENSE) © 2026 RAP
