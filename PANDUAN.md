# Rapix — panduan singkat

*Dibuat oleh RAP.*

Rapix menjadikan tablet (iPad atau Android) sebagai **monitor kedua** MacBook lewat Wi-Fi. Saat tablet tersambung, MacBook otomatis mendapat monitor baru seukuran layar tablet, jadi jendela bisa diseret ke tablet. Tablet cukup memakai browser, tanpa instal aplikasi, dan tidak perlu BetterDisplay.

## Pasang dari installer (paling mudah)

1. Buka `Rapix-…-mac.dmg`, lalu klik dua kali **Rapix**. Aplikasi memasang dirinya sendiri ke
   **Applications**. Jika macOS menolak membukanya, buka **System Settings → Privacy & Security**,
   klik **Open Anyway**, lalu klik dua kali lagi.
2. Browser menampilkan **panel Rapix**. Ikuti **Persiapan pertama**: nyalakan izin Screen Recording
   dan Accessibility untuk Rapix, lalu klik **Mulai ulang**.
3. Di tablet, scan QR di panel, lalu ketuk **Mulai**.
   Di panel, pilih **Wi-Fi** atau **Kabel USB**. Untuk kabel, colok tablet lalu nyalakan **USB tethering** di tablet;
   alamat dan QR kabel muncul otomatis.

Rapix otomatis berjalan setiap Mac menyala (bisa dimatikan di panel), jadi selanjutnya cukup buka
alamat di tablet. Untuk membuka panel lagi atau menghentikan Rapix, klik ikon Rapix di menu bar (pojok kanan atas layar). Alamat tablet memuat kode akses (mis. `http://192.168.1.5:8765/k7m2p`)
supaya orang lain di Wi-Fi yang sama tidak bisa mengendalikan komputer Anda.

## 1. Instal dan jalankan (dari kode sumber)

Unduh `LayarKedua.zip`, buka Terminal, lalu tempel:

```
cd ~/Downloads; [ -d LayarKedua ] || unzip -oq LayarKedua.zip; cd LayarKedua && xattr -dr com.apple.quarantine . ; chmod +x Jalankan.command; ./Jalankan.command
```

Kalau diminta memasang Command Line Tools, klik **Install**, lalu tempel perintah tadi sekali lagi. Selanjutnya cukup klik dua kali `Jalankan.command`.

## 2. Izin macOS (sekali saja)

Di **System Settings → Privacy & Security**, aktifkan **Terminal** di:
- **Screen & System Audio Recording**
- **Accessibility**

Tutup Terminal, lalu jalankan ulang. Jika muncul pertanyaan firewall untuk Python, pilih **Allow**.

## 3. Sambungkan tablet

1. Sambungkan tablet ke Wi-Fi yang sama dengan MacBook.
2. Putar tablet ke posisi yang diinginkan (mendatar atau tegak).
3. Buka alamat yang tampil di Terminal (atau scan QR), lalu ketuk **Mulai**.
4. Layar MacBook akan berkedip sebentar karena monitor baru terpasang.
5. Atur letak tablet di **System Settings → Displays → Arrange**. Pilih **Extend**, bukan Mirror.

Begitu aplikasi ditutup (Ctrl+C atau tutup Terminal), monitor tablet ikut hilang dan semua jendela kembali ke MacBook.

## 3b. Sambungkan lewat kabel USB (tablet Android)

Lewat kabel, gambar lebih lancar dan tidak butuh Wi-Fi.

1. Pasang `adb` sekali saja: `brew install android-platform-tools`
2. Di tablet, aktifkan **Developer options**: Settings → About tablet → ketuk **Build number** 7 kali.
3. Di **Developer options**, aktifkan **USB debugging**.
4. Jalankan `Jalankan.command`, lalu colok tablet ke MacBook dengan kabel data.
5. Di tablet, ketuk **Allow/Izinkan** pada pertanyaan USB debugging. Centang "Always allow" agar tidak ditanya lagi.
6. Browser tablet terbuka otomatis ke `http://localhost:8765`. Kalau tidak, buka alamat itu sendiri, lalu ketuk **Mulai**.

iPad belum bisa memakai mode kabel. Untuk iPad, gunakan Wi-Fi atau hotspot.

## 4. Cara memakai di tablet

- Ketuk = klik, ketuk dua kali = klik ganda
- Sentuh lalu geser = seret
- Tahan ±0,6 detik = klik kanan
- Geser dua jari = gulir
- Apple Pencil / stylus langsung menekan dan menyeret

Tips: di iPad, buka alamatnya di Safari → Share → **Add to Home Screen**, lalu buka dari ikon itu supaya tampil penuh. Matikan auto-lock tablet selama dipakai.

## 5. Pilihan tambahan

| Pilihan | Fungsi |
|---|---|
| `--resolution 1366x1024` | Paksa ukuran layar kedua (default mengikuti tablet) |
| `--hidpi` | Tampilan Retina untuk `--resolution` |
| `--mirror` | Hanya mencerminkan layar MacBook |
| `--fps 20` | Turunkan jika Wi-Fi lambat |
| `--quality 60` | Kualitas gambar 30–95 |
| `--max-width 1600` | Turunkan jika terasa patah-patah |

Contoh: `./Jalankan.command --fps 20 --quality 60`

## 6. Batasan

Gambar dikirim lewat Wi-Fi, jadi ada jeda sekitar 50–150 ms. Cocok untuk dokumen, chat, referensi, dan Spotify, tetapi kurang ideal untuk video atau game. Router 5 GHz memberikan hasil terbaik. Butuh macOS 11 (Big Sur) atau yang lebih baru.
