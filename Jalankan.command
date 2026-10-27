#!/bin/bash
# Klik dua kali file ini di Finder untuk menjalankan Rapix.
cd "$(dirname "$0")"
export PATH="$PATH:/opt/homebrew/bin:/usr/local/bin"  # supaya adb (kabel USB) ditemukan

if ! xcode-select -p >/dev/null 2>&1; then
  echo "Memasang Command Line Tools dari Apple. Ikuti jendela yang muncul,"
  echo "lalu jalankan file ini lagi setelah selesai."
  xcode-select --install
  read -p "Tekan Enter untuk menutup..."; exit 0
fi

if [ ! -x "virtualdisplay" ]; then
  echo "Menyiapkan pembuat layar kedua..."
  clang -fobjc-arc -framework Foundation -framework CoreGraphics -o virtualdisplay virtualdisplay.m \
    || echo "Gagal compile; aplikasi akan berjalan dalam mode cermin."
fi

if [ ! -d ".venv" ]; then
  echo "Menyiapkan pertama kali (butuh internet, 1-2 menit)..."
  python3 -m venv .venv
  ./.venv/bin/pip install --upgrade pip -q
  ./.venv/bin/pip install -r requirements.txt -q
fi

./.venv/bin/python server.py "$@"
read -p "Tekan Enter untuk menutup..."
