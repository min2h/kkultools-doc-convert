#!/usr/bin/env bash
# Post every file in a directory to the Gotenberg-compatible endpoint and save the PDFs.
#   URL=http://127.0.0.1:3030 AUTH_USER=kkul AUTH_PASS=... ./smoke.sh IN_DIR OUT_DIR
# Writes OUT_DIR/summary.tsv (HTTP code, time, page count if pdfinfo is installed).
set -u
IN=${1:?input dir}
OUT=${2:?output dir}
URL=${URL:-http://127.0.0.1:3030}
mkdir -p "$OUT"
: >"$OUT/summary.tsv"
printf 'file\tbytes\thttp\tseconds\tpages\n' >>"$OUT/summary.tsv"
for f in "$IN"/*; do
  [ -f "$f" ] || continue
  b=$(basename "$f")
  o="$OUT/${b%.*}.${b##*.}.pdf"
  # H2Orestart detects HWPX by content, so send it as .hwp.
  ext=$(printf '%s' "${b##*.}" | tr 'A-Z' 'a-z')
  [ "$ext" = hwpx ] && ext=hwp
  r=$(curl -s -u "${AUTH_USER:-kkul}:${AUTH_PASS:?AUTH_PASS}" -o "$o" -w '%{http_code}\t%{time_total}' \
    -F "files=@$f;filename=document.$ext" "$URL/forms/libreoffice/convert")
  code=${r%%$'\t'*}
  pages=-
  if [ "$code" = 200 ] && command -v pdfinfo >/dev/null; then
    pages=$(pdfinfo "$o" 2>/dev/null | awk '/^Pages:/{print $2}')
  elif [ "$code" != 200 ]; then
    mv "$o" "$o.err.txt" 2>/dev/null
  fi
  printf '%s\t%s\t%s\t%s\n' "$b" "$(stat -c %s "$f")" "$r" "$pages" | tee -a "$OUT/summary.tsv"
done
