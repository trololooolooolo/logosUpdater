#!/usr/bin/env bash
# Weryfikacja: czy sterownik qnx6 w kernelu Linux POPRAWNIE czyta obraz
# zbudowany przez nasz pure-Python writer (qnx6fs.py).
#
# Montuje obraz RO i porównuje WSZYSTKIE pliki (md5) z wyekstrahowanym oryginałem.
# Uruchom:   sudo bash verify_mount.sh [obraz.img] [katalog_referencyjny]

set -e
IMG="${1:-/tmp/rt_new.img}"
REF="${2:-/tmp/qnx_rt}"
MNT=/tmp/qnx6_verify_mnt

echo "== Obraz:   $IMG"
echo "== Refer.:  $REF (wyekstrahowany oryginał)"
[ -f "$IMG" ] || { echo "BRAK $IMG — uruchom najpierw round-trip w Pythonie"; exit 1; }
[ -d "$REF" ] || { echo "BRAK $REF"; exit 1; }

mkdir -p "$MNT"
mountpoint -q "$MNT" && sudo umount "$MNT"

echo "== Montuję jako qnx6 (read-only)..."
sudo mount -t qnx6 -o loop,ro "$IMG" "$MNT"
echo "== ZAMONTOWANO. Zawartość /:"
ls -la "$MNT" | head -20

echo "== Liczba plików na zamontowanym obrazie:"
MNT_FILES=$(find "$MNT" -type f | wc -l)
REF_FILES=$(find "$REF" -type f | wc -l)
echo "   obraz=$MNT_FILES   referencja=$REF_FILES"

echo "== Porównanie md5 wszystkich plików (to chwilę trwa)..."
DIFF=0
while IFS= read -r f; do
    rel="${f#$MNT/}"
    if [ -f "$REF/$rel" ]; then
        a=$(md5sum "$f" | cut -d' ' -f1)
        b=$(md5sum "$REF/$rel" | cut -d' ' -f1)
        [ "$a" != "$b" ] && { echo "   RÓŻNICA: $rel"; DIFF=$((DIFF+1)); }
    else
        echo "   BRAK W REF: $rel"; DIFF=$((DIFF+1))
    fi
done < <(find "$MNT" -type f)

echo "== Odmontowuję..."
sudo umount "$MNT"

echo
if [ "$MNT_FILES" = "$REF_FILES" ] && [ "$DIFF" = "0" ]; then
    echo "✓✓✓ SUKCES: kernel Linux czyta obraz IDENTYCZNIE jak oryginał ($MNT_FILES plików, 0 różnic)."
    echo "    Format QNX6 zbudowany przez nasz writer jest poprawny."
else
    echo "✗ UWAGA: $DIFF różnic, pliki obraz=$MNT_FILES vs ref=$REF_FILES — analizujemy."
fi
