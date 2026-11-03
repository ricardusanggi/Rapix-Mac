# Membuat installer Rapix (macOS)

Hasil: `installer/dist/Rapix-1.2.0-mac-arm64.dmg`

## Cara build

Butuh Command Line Tools (`xcode-select --install`) dan Python 3.10+. Lalu jalankan:

```bash
./installer/build_mac.sh
```

Skrip ini menyiapkan Python build, meng-compile pembuat layar virtual, membuat ikon, membuat
`Rapix.app`, lalu membungkusnya menjadi DMG. Folder `installer/build/` adalah folder kerja sementara
dan boleh dihapus kapan saja.

## Sertifikat tanda tangan (sekali saja)

```bash
./installer/create_signing_cert.sh
```

Membuat sertifikat "Rapix Self-Signed" di Keychain login. `build_mac.sh` otomatis memakainya, sehingga
macOS mengenali setiap versi baru Rapix sebagai aplikasi yang sama dan izin Screen Recording serta
Accessibility tidak perlu diberikan ulang setelah pembaruan. Tanpa sertifikat ini, aplikasi
ditandatangani *ad-hoc*.

## Catatan

- DMG hanya berjalan di Mac dengan chip yang sama dengan Mac pembuatnya (Apple Silicon → `arm64`,
  Intel → `x86_64`). Untuk Mac Intel, jalankan skrip yang sama di Mac Intel.
- Aplikasi ditandatangani *ad-hoc* (tanpa akun Apple Developer). Di Mac lain, macOS akan memblokirnya
  saat pertama dibuka; pengguna perlu membuka **System Settings → Privacy & Security** lalu klik
  **Open Anyway**.

## Mengganti versi

Ubah `1.2.0` di dua tempat: `build_mac.sh` (`VERSION`) dan `Rapix.spec` (`VERSION`).
Untuk aplikasi tablet, ubah juga `versionName` di `android/AndroidManifest.xml` dan naikkan `versionCode` satu angka.
