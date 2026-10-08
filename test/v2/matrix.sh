#!/bin/bash
# runs inside container: /s samples, /o outputs, /app engine, /lab scripts
set -u
cd /o
mkdir -p res
# extra samples
if [ ! -f /s/gov-report.pdf ]; then
  mkdir -p /tmp/g && python3 /lab/one.py /s/hwpx-gov-disaster-report.hwpx hwpx pdf /tmp/g/w /tmp/g/full.pdf >/dev/null && pdfseparate -f 1 -l 8 /tmp/g/full.pdf /tmp/g/p%d.pdf && pdfunite /tmp/g/p?.pdf /s/gov-report.pdf
fi
[ -f /s/notes.txt ] || cp /s/notes.md /s/notes.txt
cp -n /s/hancell-made.xlsx /s/hancell.cell
declare -A T
T[writer]="pdf docx hwpx hwp odt doc rtf txt html md png"
T[calc]="pdf xlsx xls ods csv html png"
T[impress]="pdf pptx ppt odp png"
T[pdf]="docx xlsx hwpx txt png jpg"
T[image]="pdf"
fam() { case $1 in docx|doc|odt|rtf|txt|html|md|hwp|hwpx) echo writer;; xlsx|xls|ods|csv|cell) echo calc;; pptx|ppt|odp) echo impress;; pdf) echo pdf;; png|jpg) echo image;; esac; }
INPUTS=${INPUTS:-"report.docx sample.doc sample.odt sample.rtf notes.txt notes.md page.html hwp-basicsReport.hwp hwp-table.hwp hwp-picture.hwp hwp-numbering.hwp hwp-footnote.hwp hwp-equation.hwp hwp-chart.hwp hwp-password.hwp hwpx-project-plan.hwpx hwpx-table.hwpx hwpx-multicolumn.hwpx table.xlsx sample.xlsx sample.xls sample.ods table.csv hancell.cell sample.pptx sample.ppt sample.odp report.pdf report-lo25.pdf table.pdf gov-report.pdf chart.png photo.jpg"}
echo -e "src\tdst\tstatus\tsec\tmaxrss_mb\tout_bytes\tout_name\terr" > res/matrix.tsv
for f in $INPUTS; do
  se=${f##*.}; base=${f%.*}; fm=$(fam $se)
  for de in ${T[$fm]}; do
    [ "$de" = "$se" ] && continue
    w=/tmp/w/$base.$se.$de; rm -rf $w; mkdir -p $w
    out=/o/$base.$se.to.$de
    /usr/bin/time -f "%e %M" -o $w.time timeout 150 python3 /lab/one.py /s/$f $se $de $w $out > $w.log 2> $w.err
    rc=$?; read sec kb < <(tail -1 $w.time)
    name=$(cat $w.log 2>/dev/null | tail -1)
    if [ $rc = 0 ]; then st=ok; sz=$(stat -c %s $out); err=""; else st=FAIL; sz=0; err=$(grep -v '^\s*$' $w.err | tail -1 | tr '\t' ' ' | cut -c1-160); fi
    echo -e "$f\t$de\t$st\t$sec\t$((kb/1024))\t$sz\t$name\t$err" | tee -a res/matrix.tsv
    rm -rf $w
  done
done
cat /sys/fs/cgroup/memory.peak 2>/dev/null | awk '{print "cgroup memory.peak MB:", $1/1048576}' | tee res/peak.txt
