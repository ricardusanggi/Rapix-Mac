#!/bin/bash
# Membuat sertifikat tanda tangan tetap "Rapix Self-Signed" di Keychain login (sekali saja).
# Dengan sertifikat ini macOS mengenali setiap versi baru Rapix sebagai aplikasi yang sama,
# sehingga izin Screen Recording dan Accessibility tidak hilang setelah pembaruan.
# Sertifikat hanya berlaku di Mac ini; tidak membuat Rapix dipercaya di Mac lain.
set -euo pipefail

NAME="Rapix Self-Signed"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"

if security find-certificate -c "$NAME" "$KEYCHAIN" >/dev/null 2>&1; then
  echo "Sertifikat \"$NAME\" sudah ada."; exit 0
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

cat > "$TMP/cert.cnf" <<EOF
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = $NAME
O = RAP
[ext]
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature
extendedKeyUsage = critical, codeSigning
EOF

/usr/bin/openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -config "$TMP/cert.cnf" \
  -keyout "$TMP/key.pem" -out "$TMP/cert.pem" 2>/dev/null
PASS="$(/usr/bin/openssl rand -hex 16)"
/usr/bin/openssl pkcs12 -export -inkey "$TMP/key.pem" -in "$TMP/cert.pem" \
  -name "$NAME" -passout "pass:$PASS" -out "$TMP/cert.p12"

# -T: codesign boleh memakai kunci ini tanpa bertanya setiap build
security import "$TMP/cert.p12" -k "$KEYCHAIN" -P "$PASS" -T /usr/bin/codesign >/dev/null
echo "Sertifikat \"$NAME\" dibuat di Keychain login."
