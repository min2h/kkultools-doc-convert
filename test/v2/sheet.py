from PIL import Image, ImageDraw, ImageFont
import os
os.chdir(os.environ.get('SHEET_DIR', '.'))  # 렌더 PNG가 있는 폴더
FB=os.environ.get('FONT_BOLD', '/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf')
FR=os.environ.get('FONT_REGULAR', '/usr/share/fonts/truetype/nanum/NanumGothic.ttf')
fb=ImageFont.truetype(FB,26); fl=ImageFont.truetype(FB,17); fn=ImageFont.truetype(FR,16)
H=430
rows=[
 ('HWPX → PDF / DOCX / HWP  (프로젝트 계획서)', 'H2Orestart loses background shapes and prints 누름틀 guide text; rhwp HWPX→HWP opens in H2Orestart', [('a1-plan-src-rhwp','원본 HWPX (rhwp render)'),('a2-plan-pdf-h2o','→ PDF (H2Orestart)'),('a3-plan-docx','→ DOCX (viewed in LO)'),('a4-plan-hwp-h2o','→ HWP via rhwp (viewed in H2O)')]),
 ('HWP → PDF / DOCX / HWPX  (과기정통부 보도자료 noori.hwp)', 'Text, tables, logo, OPEN 마크 kept; fonts substituted (함초롬 → 나눔)', [('b1-basics-src-rhwp','원본 HWP (rhwp render)'),('b2-basics-pdf-h2o','→ PDF (H2Orestart)'),('b3-basics-docx','→ DOCX (viewed in LO)'),('b4-basics-hwpx-rhwp','→ HWPX via rhwp (viewed in H2O)')]),
 ('DOCX → HWPX / HWP  (pypandoc-hwpx + rhwp)', 'Headings, table w/ merged cells, image, lists kept; lost: header/footer, colors, fonts', [('c1-report-docx-src','원본 DOCX'),('c2-report-hwpx-rhwp','→ HWPX (rhwp render)'),('c3-report-hwpx-h2o','→ HWPX (H2Orestart render)'),('c4-report-hwp-h2o','→ HWP (H2Orestart render)')]),
 ('PDF → DOCX  (pdf2docx)', 'Good on born-digital PDFs; title spacing glitches; scanned PDFs = images only', [('d1-report-pdf-src','원본 PDF'),('d2-report-pdf-docx','→ DOCX (viewed in LO)'),('d3-gov-pdf-src','원본 PDF (정부 보고서 표지)'),('d4-gov-pdf-docx','→ DOCX (viewed in LO)')]),
 ('XLSX / 한셀 → PDF', 'LibreOffice Calc; formulas computed, number formats kept', [('e1-table-xlsx-src','원본 XLSX (LO view)'),('e2-table-xlsx-pdf','→ PDF'),('e3-hancell-pdf','한셀이 만든 xlsx(.cell) → PDF')]),
 ('PDF → XLSX  (pdfplumber tables)', 'Ruled tables → one sheet per table, numbers as numbers; titles outside tables dropped', [('f1-govtables-pdf-src','원본 PDF (표 16×5)'),('f2-govtables-xlsx','→ XLSX sheet "p3-표1"'),('f3-table-pdf-src','원본 PDF'),('f4-table-pdf-xlsx','→ XLSX')]),
]
imgs=[]
for title,note,cells in rows:
    ims=[]
    for f,l in cells:
        im=Image.open(f+'.png').convert('RGB'); s=H/im.height; ims.append((im.resize((int(im.width*s),H)),l))
    imgs.append((title,note,ims))
W=max(sum(i.width+24 for i,_ in ims)+24 for _,_,ims in imgs)
RH=H+110
c=Image.new('RGB',(W,RH*len(rows)+70),'#f2f4f6'); d=ImageDraw.Draw(c)
d.text((24,18),'꿀툴 문서 변환 확대 — 시험 결과 (2026-10-08, LibreOffice 26.8 + H2Orestart 0.7.14 + rhwp 0.8.7 + pypandoc-hwpx 0.1.1 + pdf2docx 0.5.13)',font=fl,fill='#333')
y=60
for title,note,ims in imgs:
    d.text((24,y),title,font=fb,fill='#191f28'); d.text((24,y+34),note,font=fn,fill='#4e5968')
    x=24
    for im,l in ims:
        c.paste(im,(x,y+86)); d.rectangle([x-1,y+85,x+im.width,y+86+H],outline='#b0b8c1'); d.text((x,y+62),l,font=fl,fill='#3182f6'); x+=im.width+24
    y+=RH
c.save('docfmt-samples-sheet.png',optimize=True)
print(c.size)
