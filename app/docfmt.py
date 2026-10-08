"""꿀툴 문서 변환 엔진 (any → any). 서버(app.py)와 시험 도구가 같이 써요.

convert(src, src_ext, dst_ext, work) -> 결과 파일 경로
  - src: 입력 파일 경로, work: 이 요청 전용 임시 폴더(호출한 쪽이 지워요)
  - 결과가 여러 개(쪽마다 PNG 등)면 zip 하나로 묶어요.
도구: LibreOffice(+H2Orestart, HWP·HWPX 읽기), rhwp(HWP↔HWPX), pypandoc-hwpx(→HWPX),
      pandoc(MD), pdf2docx(PDF→DOCX), pdfplumber(PDF→XLSX), poppler(PDF→PNG·TXT), img2pdf.
"""
import os, re, shutil, subprocess, zipfile, glob

SOFFICE = os.environ.get('SOFFICE', 'soffice')
UNO_PORT = os.environ.get('UNO_PORT')  # 있으면 unoserver(상주 LibreOffice)로 보내요
TIMEOUT = int(os.environ.get('CONVERT_TIMEOUT', '100'))
MAX_PNG_PAGES = 30
MAX_PDF_EDIT_PAGES = int(os.environ.get('MAX_PDF_EDIT_PAGES', '60'))  # PDF→DOCX/HWPX는 쪽당 0.1~2초

WRITER = {'docx', 'doc', 'odt', 'rtf', 'txt', 'html', 'md', 'hwp', 'hwpx'}
CALC = {'xlsx', 'xls', 'ods', 'csv', 'cell'}
IMPRESS = {'pptx', 'ppt', 'odp'}
IMAGES = {'png', 'jpg'}
PDF = {'pdf'}

# 입력 계열 → 나갈 수 있는 형식 (화면·Worker와 같은 표를 써요: src/data/doc-formats.js)
TARGETS = {
    'writer': ['pdf', 'docx', 'hwpx', 'hwp', 'odt', 'doc', 'rtf', 'txt', 'html', 'md', 'png'],
    'calc': ['pdf', 'xlsx', 'xls', 'ods', 'csv', 'html', 'png'],
    'impress': ['pdf', 'pptx', 'ppt', 'odp', 'png'],
    'pdf': ['docx', 'xlsx', 'hwpx', 'txt', 'png', 'jpg'],
    'image': ['pdf'],
}

LO_WRITER_OUT = {
    'pdf': 'pdf:writer_pdf_Export', 'docx': 'docx:MS Word 2007 XML', 'doc': 'doc:MS Word 97',
    'odt': 'odt:writer8', 'rtf': 'rtf:Rich Text Format', 'txt': 'txt:Text (encoded):UTF8',
    'html': 'html:XHTML Writer File:UTF8',
}
LO_CALC_OUT = {
    'pdf': 'pdf:calc_pdf_Export', 'xlsx': 'xlsx:Calc MS Excel 2007 XML', 'xls': 'xls:MS Excel 97',
    'ods': 'ods:calc8', 'csv': 'csv:Text - txt - csv (StarCalc):44,34,76,1,,0,false,true,false,false,false,-1',
    'html': 'html:HTML (StarCalc)',
}
LO_IMPRESS_OUT = {
    'pdf': 'pdf:impress_pdf_Export', 'pptx': 'pptx:Impress MS PowerPoint 2007 XML', 'ppt': 'ppt:MS PowerPoint 97',
    'odp': 'odp:impress8',
}


class ConvertError(Exception):
    pass


def family(ext):
    if ext in WRITER: return 'writer'
    if ext in CALC: return 'calc'
    if ext in IMPRESS: return 'impress'
    if ext in PDF: return 'pdf'
    if ext in IMAGES: return 'image'
    return None


def supported(src_ext, dst_ext):
    f = family(src_ext)
    return bool(f) and dst_ext in TARGETS[f] and dst_ext != src_ext


RHWP_MEM = int(os.environ.get('RHWP_MEM_BYTES', str(1200 * 1024 * 1024)))  # rhwp 메모리 상한 (큰 문서에서 수 GB까지 써요)
PROFILE_SEED = os.environ.get('LO_PROFILE_SEED', '/opt/kkul/lo-profile')  # 이미지에 미리 만들어 둔 LibreOffice 프로필


def rhwp(*args):
    """rhwp를 메모리 상한(prlimit --as) 안에서 실행해요. 넘으면 컨테이너 대신 rhwp만 죽어요."""
    return run(['prlimit', f'--as={RHWP_MEM}', '--', 'rhwp', *args])


def sniff_hwp(src, src_ext):
    """확장자와 실제 내용이 다른 한글 파일(.hwp 이름의 HWPX zip 등)을 바로잡아요."""
    if src_ext not in ('hwp', 'hwpx'):
        return src_ext
    with open(src, 'rb') as f:
        head = f.read(8)
    if head.startswith(b'PK\x03\x04'):
        return 'hwpx'
    if head.startswith(b'\xd0\xcf\x11\xe0'):
        return 'hwp'
    return src_ext


def run(cmd, timeout=TIMEOUT, **kw):
    r = subprocess.run(cmd, capture_output=True, timeout=timeout, **kw)
    if r.returncode != 0:
        raise ConvertError(f'{os.path.basename(cmd[0])} 실패 ({r.returncode}): {r.stderr.decode(errors="replace")[-400:]}')
    return r


def lo(src, fmt, work, infilter=None):
    """LibreOffice로 변환. fmt 예: 'docx:MS Word 2007 XML'. 결과 경로를 돌려줘요."""
    out_dir = os.path.join(work, 'lo-out'); os.makedirs(out_dir, exist_ok=True)
    ext = fmt.split(':', 1)[0]
    if UNO_PORT:
        dst = os.path.join(out_dir, os.path.splitext(os.path.basename(src))[0] + '.' + ext)
        filt = fmt.split(':', 2)
        cmd = ['unoconvert', '--port', UNO_PORT, '--host-location', 'local', '--dont-update-index']
        if infilter: cmd += ['--input-filter', infilter.split(':', 1)[0]]
        if len(filt) > 1: cmd += ['--output-filter', filt[1]]
        if len(filt) > 2: cmd += ['--filter-option', filt[2]]
        try:
            run(cmd + [src, dst])
            outs = [p for p in glob.glob(os.path.join(out_dir, '*')) if p.lower().endswith('.' + ext)]
            if outs:
                return dst if os.path.exists(dst) else sorted(outs)[0]
        except (ConvertError, subprocess.TimeoutExpired):
            pass
        # 상주 LibreOffice가 이 문서에서 죽었을 수 있어요(H2Orestart 오류 등) → 새 soffice 한 번으로 다시 시도
        shutil.rmtree(out_dir, ignore_errors=True); os.makedirs(out_dir)
    prof = os.path.join(work, 'lo-profile')
    if os.path.isdir(PROFILE_SEED) and not os.path.exists(prof):
        shutil.copytree(PROFILE_SEED, prof, symlinks=True)
    cmd = [SOFFICE, f'-env:UserInstallation=file://{prof}', '--headless', '--norestore', '--nolockcheck']
    if infilter: cmd += [f'--infilter={infilter}']
    run(cmd + ['--convert-to', fmt, '--outdir', out_dir, src])
    outs = [p for p in glob.glob(os.path.join(out_dir, '*')) if p.lower().endswith('.' + ext)]
    if not outs:
        raise ConvertError('LibreOffice 결과 없음')
    return outs[0]


def _named(src, ext, work, name='in'):
    """도구들이 확장자로 형식을 알아보므로 'in.<ext>' 이름으로 복사해요."""
    p = os.path.join(work, f'{name}.{ext}')
    if os.path.abspath(src) != os.path.abspath(p):
        shutil.copyfile(src, p)
    return p


def fix_hwpx(path):
    """첫 항목이 압축 안 된 mimetype이 아니면 순서만 고쳐 다시 묶어요 (H2Orestart가 못 읽는 문제)."""
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        if infos and infos[0].filename == 'mimetype' and infos[0].compress_type == zipfile.ZIP_STORED:
            return path
        data = {i.filename: z.read(i) for i in infos}
    if 'mimetype' not in data:
        return path
    fixed = path + '.fixed.hwpx'
    with zipfile.ZipFile(fixed, 'w') as z:
        z.writestr(zipfile.ZipInfo('mimetype'), data.pop('mimetype'), compress_type=zipfile.ZIP_STORED)
        for k, v in data.items():
            z.writestr(k, v, compress_type=zipfile.ZIP_DEFLATED)
    return fixed


def to_utf8_text(path, work, ext):
    """CSV·TXT·MD: UTF-8(BOM)·CP949(엑셀 한국어 기본)를 알아서 UTF-8로 바꿔요."""
    raw = open(path, 'rb').read()
    for enc in ('utf-8-sig', 'cp949', 'utf-16'):
        try:
            txt = raw.decode(enc); break
        except UnicodeDecodeError:
            continue
    else:
        txt = raw.decode('utf-8', errors='replace')
    p = os.path.join(work, f'in-utf8.{ext}')
    open(p, 'w', encoding='utf-8').write(txt)
    return p


def pages_to_images(pdf, dst_ext, work):
    out = os.path.join(work, 'pages'); os.makedirs(out, exist_ok=True)
    dev = ['-png'] if dst_ext == 'png' else ['-jpeg', '-jpegopt', 'quality=88']
    run(['pdftoppm', *dev, '-r', '150', '-l', str(MAX_PNG_PAGES), pdf, os.path.join(out, 'page')])
    files = sorted(glob.glob(os.path.join(out, 'page*')))
    if not files:
        raise ConvertError('쪽 그림 없음')
    if len(files) == 1:
        return files[0]
    z = os.path.join(work, 'pages.zip')
    with zipfile.ZipFile(z, 'w', zipfile.ZIP_STORED) as zf:
        for i, f in enumerate(files, 1):
            zf.write(f, f'{i:03d}.{dst_ext}')
    return z


def _pypandoc_hwpx(src, out):
    """pypandoc-hwpx(MIT): Pandoc AST → OWPML. 문서 제목(Title 스타일)이 메타데이터로 빠지는 문제만 고쳐서 써요."""
    from pypandoc_hwpx.PandocToHwpx import PandocToHwpx
    if not getattr(PandocToHwpx, '_kkul_title_fix', False):
        orig = PandocToHwpx.convert
        def convert_with_title(self, page_setup_xml=None):
            if self.title and self.ast is not None:
                self.ast.setdefault('blocks', []).insert(0, {'t': 'Header', 'c': [1, ['', [], []], [{'t': 'Str', 'c': self.title}]]})
            return orig(self, page_setup_xml)
        PandocToHwpx.convert = convert_with_title
        PandocToHwpx._kkul_title_fix = True
    import pypandoc_hwpx, contextlib, io
    ref = os.path.join(os.path.dirname(pypandoc_hwpx.__file__), 'blank.hwpx')
    with contextlib.redirect_stdout(io.StringIO()):
        PandocToHwpx.convert_to_hwpx(src, out, ref)


def finish_hwpx(raw, work):
    """rhwp로 한 번 다시 저장해 한컴 호환 형태(네임스페이스·빈 subList 등)로 맞추고, 패키지 검사를 통과해야 내보내요."""
    out = os.path.join(work, 'out.hwpx')
    rhwp('export-hwpx', raw, out)
    r = subprocess.run(['hwpx-validate-package', out], capture_output=True, timeout=60)
    if r.returncode != 0:
        raise ConvertError('HWPX 검사 실패: ' + r.stdout.decode(errors='replace')[-300:])
    return out


def docx_to_hwpx(docx, work):
    raw = os.path.join(work, 'pandoc.hwpx')
    _pypandoc_hwpx(_named(docx, 'docx', work, 'hwpx-src'), raw)
    return finish_hwpx(raw, work)


def hwp_family_lo(src, src_ext, fmt, work):
    """HWP·HWPX → LibreOffice(H2Orestart). 실패하면 rhwp로 HWPX를 다시 만들어 한 번 더 시도해요
    (H2Orestart가 HWP 바이너리의 각주 등에서 실패해도 HWPX 경로로는 열리는 경우가 많아요)."""
    try:
        return lo(_prep(src, src_ext, work), fmt, work)
    except (ConvertError, subprocess.TimeoutExpired) as first:
        alt = os.path.join(work, 'alt.hwpx')
        try:
            rhwp('export-hwpx', _named(src, src_ext, work), alt)
        except Exception:
            raise first
        shutil.rmtree(os.path.join(work, 'lo-out'), ignore_errors=True)
        try:
            return lo(alt, fmt, work)
        except ConvertError:
            if fmt.startswith('pdf:'):  # 마지막으로 rhwp 자체 렌더러 (메모리 상한 안에서)
                out = os.path.join(work, 'out.pdf')
                rhwp('export-pdf', _named(src, src_ext, work), '-o', out)
                return out
            raise


def pdf_to_docx(pdf, work):
    from pdf2docx import Converter
    out = os.path.join(work, 'out.docx')
    cv = Converter(pdf)
    try:
        cv.convert(out, multi_processing=False)
    finally:
        cv.close()
    return out


def pdf_to_xlsx(pdf, work):
    """PDF 속 표를 시트로. 표가 없으면 쪽마다 글 줄을 칸(공백 2개 이상)으로 나눠 넣어요."""
    import pdfplumber
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter
    wb = Workbook(); wb.remove(wb.active)
    n_tables = 0
    with pdfplumber.open(pdf) as doc:
        for pi, page in enumerate(doc.pages[:200], 1):
            tables = page.extract_tables()
            for ti, t in enumerate(tables, 1):
                rows = [[(c or '').strip() for c in r] for r in t if any((c or '').strip() for c in r)]
                if not rows: continue
                n_tables += 1
                ws = wb.create_sheet(f'p{pi}-표{ti}'[:31])
                for r in rows:
                    ws.append([_num(c) for c in r])
                for ci in range(1, max(len(r) for r in rows) + 1):
                    w = max(len(str(r[ci - 1])) if ci <= len(r) else 0 for r in rows)
                    ws.column_dimensions[get_column_letter(ci)].width = min(50, max(8, w * 1.6 + 2))
        if n_tables == 0:
            ws = wb.create_sheet('텍스트')
            for pi, page in enumerate(doc.pages[:200], 1):
                for line in (page.extract_text() or '').splitlines():
                    ws.append([_num(c) for c in re.split(r'\s{2,}', line.strip())])
    out = os.path.join(work, 'out.xlsx'); wb.save(out)
    return out


_NUM = re.compile(r'^-?[\d,]+(\.\d+)?$')
def _num(s):
    s2 = s.replace('\n', ' ')
    if _NUM.match(s2) and (',' not in s2 or re.match(r'^-?\d{1,3}(,\d{3})+(\.\d+)?$', s2)):
        try:
            return float(s2.replace(',', '')) if '.' in s2 else int(s2.replace(',', ''))
        except ValueError:
            pass
    return s2


def convert(src, src_ext, dst_ext, work):
    src_ext, dst_ext = src_ext.lower(), dst_ext.lower()
    src_ext = sniff_hwp(src, src_ext)
    if src_ext == dst_ext and src_ext in ('hwp', 'hwpx'):
        out = os.path.join(work, f'out.{dst_ext}'); shutil.copyfile(src, out); return out
    if not supported(src_ext, dst_ext):
        raise ConvertError(f'지원하지 않는 변환: {src_ext} → {dst_ext}')
    fam = family(src_ext)

    # ── 한글 사이 직접 변환 (rhwp: 서식 그대로 옮겨요) ──
    if src_ext == 'hwp' and dst_ext == 'hwpx':
        out = os.path.join(work, 'out.hwpx'); rhwp('export-hwpx', _named(src, 'hwp', work), out); return out
    if src_ext == 'hwpx' and dst_ext == 'hwp':
        out = os.path.join(work, 'out.hwp'); rhwp('convert', _named(src, 'hwpx', work), out); return out

    if fam == 'writer':
        if src_ext == 'md':
            if dst_ext == 'hwpx':
                return run_hwpx_from_md(src, work)
            docx = os.path.join(work, 'md.docx')
            run(['pandoc', '-f', 'gfm', '-t', 'docx', to_utf8_text(src, work, 'md'), '-o', docx])
            return convert(docx, 'docx', dst_ext, work) if dst_ext != 'docx' else docx
        if dst_ext == 'md':
            docx = src if src_ext == 'docx' else (hwp_family_lo(src, src_ext, LO_WRITER_OUT['docx'], work) if src_ext in ('hwp', 'hwpx') else lo(_prep(src, src_ext, work), LO_WRITER_OUT['docx'], work, _infilter(src_ext)))
            out = os.path.join(work, 'out.md')
            run(['pandoc', '-f', 'docx', '-t', 'gfm', '--wrap=none', docx, '-o', out])
            return out
        if dst_ext in ('hwpx', 'hwp'):
            docx = _named(src, 'docx', work) if src_ext == 'docx' else lo(_prep(src, src_ext, work), LO_WRITER_OUT['docx'], work, _infilter(src_ext))
            hwpx = docx_to_hwpx(docx, work)
            if dst_ext == 'hwpx': return hwpx
            out = os.path.join(work, 'out.hwp'); rhwp('convert', hwpx, out); return out
        if src_ext in ('hwp', 'hwpx'):
            if dst_ext in ('odt', 'html'):  # H2Orestart → odt/html 저장이 일부 문서에서 깨져요 → DOCX를 거쳐요
                docx = hwp_family_lo(src, src_ext, LO_WRITER_OUT['docx'], work)
                return lo(_named(docx, 'docx', work, 'mid'), LO_WRITER_OUT[dst_ext], os.path.join(work, 'step2'))
            pdf_or = hwp_family_lo(src, src_ext, LO_WRITER_OUT['pdf' if dst_ext == 'png' else dst_ext], work)
            return pages_to_images(pdf_or, 'png', work) if dst_ext == 'png' else pdf_or
        if dst_ext == 'png':
            return pages_to_images(lo(_prep(src, src_ext, work), LO_WRITER_OUT['pdf'], work, _infilter(src_ext)), 'png', work)
        return lo(_prep(src, src_ext, work), LO_WRITER_OUT[dst_ext], work, _infilter(src_ext))

    if fam == 'calc':
        if dst_ext == 'png':
            return pages_to_images(lo(_prep(src, src_ext, work), LO_CALC_OUT['pdf'], work, _infilter(src_ext)), 'png', work)
        out = lo(_prep(src, src_ext, work), LO_CALC_OUT[dst_ext], work, _infilter(src_ext))
        if dst_ext == 'csv':  # 시트가 여러 개면 in-시트이름.csv 여러 개 → zip
            csvs = sorted(glob.glob(os.path.join(os.path.dirname(out), '*.csv')))
            if len(csvs) > 1:
                z = os.path.join(work, 'sheets.zip')
                with zipfile.ZipFile(z, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for c in csvs:
                        zf.write(c, re.sub(r'^in-', '', os.path.basename(c)))
                return z
        return out

    if fam == 'impress':
        if dst_ext == 'png':
            return pages_to_images(lo(_prep(src, src_ext, work), LO_IMPRESS_OUT['pdf'], work), 'png', work)
        return lo(_prep(src, src_ext, work), LO_IMPRESS_OUT[dst_ext], work)

    if fam == 'pdf':
        pdf = _named(src, 'pdf', work)
        if dst_ext in ('docx', 'hwpx'):
            info = run(['pdfinfo', pdf], timeout=20).stdout.decode(errors='replace')
            m = re.search(r'^Pages:\s+(\d+)', info, re.M)
            if m and int(m.group(1)) > MAX_PDF_EDIT_PAGES:
                raise ConvertError(f'쪽이 너무 많아요 ({m.group(1)}쪽, 최대 {MAX_PDF_EDIT_PAGES}쪽)')
        if dst_ext == 'docx': return pdf_to_docx(pdf, work)
        if dst_ext == 'hwpx': return docx_to_hwpx(pdf_to_docx(pdf, work), work)
        if dst_ext == 'xlsx': return pdf_to_xlsx(pdf, work)
        if dst_ext == 'txt':
            out = os.path.join(work, 'out.txt'); run(['pdftotext', '-layout', '-enc', 'UTF-8', pdf, out]); return out
        if dst_ext in ('png', 'jpg'): return pages_to_images(pdf, dst_ext, work)

    if fam == 'image':
        out = os.path.join(work, 'out.pdf'); run(['img2pdf', '--pagesize', 'A4', '--fit', 'into', '-o', out, _named(src, src_ext, work)]); return out

    raise ConvertError(f'지원하지 않는 변환: {src_ext} → {dst_ext}')


def run_hwpx_from_md(src, work):
    raw = os.path.join(work, 'pandoc.hwpx')
    _pypandoc_hwpx(_named(to_utf8_text(src, work, 'md'), 'md', work, 'doc'), raw)
    return finish_hwpx(raw, work)


def _infilter(ext):
    return {
        'csv': 'Text - txt - csv (StarCalc):44,34,76,1',
        'txt': 'Text (encoded):UTF8',
        'html': 'HTML (StarWriter)',
    }.get(ext)


def _prep(src, ext, work):
    if ext == 'hwpx':
        return fix_hwpx(_named(src, 'hwpx', work))
    if ext in ('csv', 'txt'):
        return to_utf8_text(src, work, ext)
    if ext == 'cell':
        # 한셀 2014 이후 .cell = OOXML(zip). 2010 이하(OLE)는 못 읽어요.
        with open(src, 'rb') as f:
            if f.read(4) != b'PK\x03\x04':
                raise ConvertError('한셀 2010 이하 .cell은 지원하지 않아요')
        return _named(src, 'xlsx', work)
    return _named(src, ext, work)
