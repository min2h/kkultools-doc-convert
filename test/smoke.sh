#!/usr/bin/env bash
# 변환 서버 시험: 폴더의 모든 문서를 보내 PDF로 받아요.
#   URL=http://127.0.0.1:3030 AUTH_USER=kkul AUTH_PASS=비밀 ./smoke.sh <입력폴더> <출력폴더>
# 결과: 파일별 HTTP 코드·걸린 시간·쪽수 (pdfinfo 있으면) — summary.tsv
set -u
IN=${1:?입력 폴더}; OUT=${2:?출력 폴더}; URL=${URL:-http://127.0.0.1:3030}
mkdir -p "$OUT"; : > "$OUT/summary.tsv"
printf 'file\tbytes\thttp\tseconds\tpages\n' >> "$OUT/summary.tsv"
for f in "$IN"/*; do
  [ -f "$f" ] || continue
  b=$(basename "$f"); o="$OUT/${b%.*}.${b##*.}.pdf"
  # Worker와 똑같이: 원래 이름 대신 document.<확장자>, HWPX는 .hwp로 보내요(H2Orestart가 내용으로 판별)
  ext=$(printf '%s' "${b##*.}" | tr 'A-Z' 'a-z'); [ "$ext" = hwpx ] && ext=hwp
  r=$(curl -s -u "${AUTH_USER:-kkul}:${AUTH_PASS:?AUTH_PASS}" -o "$o" -w '%{http_code}\t%{time_total}' \
      -F "files=@$f;filename=document.$ext" "$URL/forms/libreoffice/convert")
  code=${r%%$'\t'*}; pages=-
  if [ "$code" = 200 ] && command -v pdfinfo >/dev/null; then pages=$(pdfinfo "$o" 2>/dev/null | awk '/^Pages:/{print $2}'); else [ "$code" = 200 ] || { mv "$o" "$o.err.txt" 2>/dev/null; }; fi
  printf '%s\t%s\t%s\t%s\n' "$b" "$(stat -c %s "$f")" "$r" "$pages" | tee -a "$OUT/summary.tsv"
done
