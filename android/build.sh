#!/bin/bash
# Membuat aplikasi tablet Rapix: android/build/Rapix.apk
# Hanya memakai Android SDK (aapt2, d8, apksigner); tanpa Gradle, tanpa unduhan.
#
# Pemakaian:  ./android/build.sh           (build saja)
#             ./android/build.sh pasang    (build lalu pasang ke tablet lewat adb)
set -euo pipefail
cd "$(dirname "$0")"

SDK="${ANDROID_HOME:-$HOME/Library/Android/sdk}"
BT="$SDK/build-tools/$(ls "$SDK/build-tools" | sort -V | tail -1)"
JAR="$SDK/platforms/$(ls "$SDK/platforms" | sort -V | tail -1)/android.jar"
# d8 dan apksigner terbaru butuh Java 17+; JDK bawaan Android Studio sudah cukup
JBR="/Applications/Android Studio.app/Contents/jbr/Contents/Home"
[ -x "$JBR/bin/java" ] && export JAVA_HOME="$JBR" PATH="$JBR/bin:$PATH"

OUT=build
rm -rf "$OUT" && mkdir -p "$OUT/res/mipmap-xxxhdpi" "$OUT/classes" "$OUT/gen"

echo "==> Ikon"
ICON=../installer/build/icon.png
[ -f "$ICON" ] || python3 ../installer/make_icon.py
sips -z 192 192 "$ICON" --out "$OUT/res/mipmap-xxxhdpi/ic_launcher.png" >/dev/null

echo "==> Resource"
"$BT/aapt2" compile --dir "$OUT/res" -o "$OUT/res.zip"
"$BT/aapt2" link -o "$OUT/base.apk" -I "$JAR" --manifest AndroidManifest.xml \
  --java "$OUT/gen" "$OUT/res.zip"

echo "==> Compile Java"
javac --release 11 -nowarn -encoding UTF-8 -classpath "$JAR" -d "$OUT/classes" \
  $(find src "$OUT/gen" -name '*.java')

echo "==> Dex"
"$BT/d8" --release --min-api 26 --lib "$JAR" --output "$OUT" $(find "$OUT/classes" -name '*.class')

echo "==> Paket & tanda tangan"
cp "$OUT/base.apk" "$OUT/unsigned.apk"
(cd "$OUT" && zip -q unsigned.apk classes.dex)
"$BT/zipalign" -f 4 "$OUT/unsigned.apk" "$OUT/aligned.apk"
# kunci debug Android Studio; buat sekali jika belum ada
KS="$HOME/.android/debug.keystore"
[ -f "$KS" ] || keytool -genkeypair -keystore "$KS" -storepass android -keypass android -alias androiddebugkey \
  -dname "CN=Android Debug,O=Android,C=US" -keyalg RSA -validity 10000 >/dev/null
"$BT/apksigner" sign --ks "$KS" --ks-pass pass:android --key-pass pass:android \
  --out "$OUT/Rapix.apk" "$OUT/aligned.apk"

echo "Selesai: android/$OUT/Rapix.apk"

if [ "${1:-}" = pasang ]; then
  adb install -r "$OUT/Rapix.apk"
  adb shell am start -n id.rap.rapix/.MainActivity
fi
