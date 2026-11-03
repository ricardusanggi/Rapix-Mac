#!/bin/bash
# Membuat installer macOS: installer/dist/Rapix-<versi>-mac-<arsitektur>.dmg
#
# Pemakaian:  ./installer/build_mac.sh
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="1.1.0"
ARCH="$(uname -m)"
BUILD="installer/build"
DIST="installer/dist"
APP="$DIST/Rapix.app"
DMG="$DIST/Rapix-$VERSION-mac-$ARCH.dmg"

if ! xcode-select -p >/dev/null 2>&1; then
  echo "Butuh Command Line Tools: jalankan 'xcode-select --install' lalu ulangi."; exit 1
fi

echo "==> Menyiapkan Python build"
mkdir -p "$BUILD"
[ -d "$BUILD/venv" ] || python3 -m venv "$BUILD/venv"
PY="$BUILD/venv/bin/python"
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q -r requirements.txt -r installer/requirements-build.txt

echo "==> Compile pembuat layar virtual"
clang -fobjc-arc -mmacosx-version-min=11.0 -framework Foundation -framework CoreGraphics \
  -o virtualdisplay virtualdisplay.m

echo "==> Compile perekam video H.264"
clang -fobjc-arc -mmacosx-version-min=12.3 -framework Foundation -framework CoreGraphics -framework CoreMedia \
  -framework CoreVideo -framework ScreenCaptureKit -framework VideoToolbox \
  -o screenstream screenstream.m

echo "==> Membuat ikon"
"$PY" installer/make_icon.py

echo "==> Membuat Rapix.app"
"$PY" -m PyInstaller --noconfirm --clean --log-level WARN \
  --distpath "$DIST" --workpath "$BUILD/work" installer/Rapix.spec
rm -rf "$DIST/Rapix"  # folder antara; yang dipakai hanya .app

echo "==> Tanda tangan"
# sertifikat tetap (buat sekali dengan create_signing_cert.sh) agar izin macOS tidak hilang setiap pembaruan
CERT="$(security find-certificate -c "Rapix Self-Signed" -Z 2>/dev/null | awk '/SHA-1/ {print $3}' | head -1)"
if [ -n "$CERT" ]; then
  codesign --force --deep --sign "$CERT" "$APP"
  echo "Ditandatangani dengan sertifikat Rapix Self-Signed"
else
  codesign --force --deep --sign - "$APP"  # ad-hoc: izin macOS perlu diberikan ulang setiap pembaruan
  echo "Ditandatangani ad-hoc (jalankan installer/create_signing_cert.sh agar izin tidak hilang)"
fi

echo "==> Membuat DMG"
STAGE="$BUILD/dmg"
rm -rf "$STAGE" "$DMG"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
cat > "$STAGE/Cara Pasang.txt" <<'TXT'
CARA PASANG RAPIX

1. Klik dua kali "Rapix".
   Aplikasi otomatis memasang dirinya ke folder Applications.
2. Jika macOS menolak membukanya: buka System Settings > Privacy & Security,
   gulir ke bawah, klik "Open Anyway", lalu klik dua kali "Rapix" lagi.
3. Browser akan menampilkan panel Rapix. Ikuti langkah "Persiapan pertama"
   (izin Screen Recording dan Accessibility, lalu Mulai ulang).
4. Di tablet, scan QR pada panel, lalu ketuk "Mulai".

Setelah terpasang, Rapix otomatis berjalan setiap Mac menyala
(bisa dimatikan di panel). DMG ini boleh di-eject dan dihapus.
TXT
cp PANDUAN.md "$STAGE/Panduan lengkap.md"
hdiutil create -quiet -volname "Rapix" -srcfolder "$STAGE" -ov -format UDZO "$DMG"

echo
echo "Selesai: $DMG"
