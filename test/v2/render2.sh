#!/bin/bash
set -u
D=/m; mkdir -p $D /tmp/r2
r() { local f=$1 how=$2 name=$3 page=${4:-1} w=/tmp/r2/$3; rm -rf $w; mkdir -p $w; local pdf
  case $how in
    lo) cp "$f" $w/in.${f##*.}; soffice -env:UserInstallation=file://$w/prof --headless --convert-to pdf --outdir $w $w/in.${f##*.} >/dev/null 2>&1; pdf=$w/in.pdf;;
    rhwp) cp "$f" $w/in.${f##*.}; rhwp export-pdf $w/in.${f##*.} -o $w/in.pdf >/dev/null 2>&1; pdf=$w/in.pdf;;
    pdf) pdf=$f;;
  esac
  [ -f "$pdf" ] && pdftoppm -png -r 80 -f $page -l $page -singlefile "$pdf" $D/$name && echo "$name ok" || echo "$name FAIL"; }
S=/s; O=/o
r $S/hwpx-project-plan.hwpx rhwp a1-plan-src-rhwp
r $O/hwpx-project-plan.hwpx.to.pdf pdf a2-plan-pdf-h2o
r $O/hwpx-project-plan.hwpx.to.docx lo a3-plan-docx
r $O/hwpx-project-plan.hwpx.to.hwp lo a4-plan-hwp-h2o
r $S/hwp-noori.hwp rhwp b1-basics-src-rhwp
r $O/hwp-noori.hwp.to.pdf pdf b2-basics-pdf-h2o
r $O/hwp-noori.hwp.to.docx lo b3-basics-docx
r $O/hwp-noori.hwp.to.hwpx lo b4-basics-hwpx-rhwp
r $S/report.docx lo c1-report-docx-src
r $O/report.docx.to.hwpx rhwp c2-report-hwpx-rhwp
r $O/report.docx.to.hwpx lo c3-report-hwpx-h2o
r $O/report.docx.to.hwp lo c4-report-hwp-h2o
r $S/report.pdf pdf d1-report-pdf-src
r $O/report.pdf.to.docx lo d2-report-pdf-docx
r $S/gov-report.pdf pdf d3-gov-pdf-src 1
r $O/gov-report.pdf.to.docx lo d4-gov-pdf-docx 1
r $S/table.xlsx lo e1-table-xlsx-src
r $O/table.xlsx.to.pdf pdf e2-table-xlsx-pdf
r $O/hancell.cell.to.pdf pdf e3-hancell-pdf
r $S/gov-tables.pdf pdf f1-govtables-pdf-src 3
r $O/gov-p3sheet.xlsx lo f2-govtables-xlsx
r $S/table.pdf pdf f3-table-pdf-src
r $O/table.pdf.to.xlsx lo f4-table-pdf-xlsx
