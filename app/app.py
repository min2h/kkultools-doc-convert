"""꿀툴 문서 변환 서버 (Cloud Run). Gotenberg 대신 쓰는 작은 HTTP 서버예요.

  POST /convert?from=hwp&to=docx   (본문 = 파일 바이트, Basic 인증 kkul:<공유 비밀값>)
     200 → 결과 바이트 · x-kkul-ext: 실제 확장자(쪽 그림 여러 장이면 zip)
     400 잘못된 요청 · 401 인증 · 411/413 크기 · 415 지원 안 하는 변환 · 422 변환 실패 · 503 바쁨 · 504 시간 초과
  POST /forms/libreoffice/convert   (v1 Gotenberg 호환: multipart 'files' 한 개 → PDF)
     배포 중에도 예전 Worker가 그대로 동작하도록 남겨 둬요. 파일 이름의 확장자(document.<ext>)로 형식을 알아봐요.
  GET /health

파일은 요청마다 만든 임시 폴더(Cloud Run은 메모리 파일 시스템)에만 두고 응답 뒤 바로 지워요.
변환은 자식 프로세스(python -m docfmt_cli)에서 돌려 시간 초과·메모리 폭주 때 통째로 끊어요.
LibreOffice는 unoserver로 띄워 두고(요청마다 새로 켜는 2초를 아껴요) N건마다 새로 띄워요.
"""
import base64, hmac, os, shutil, signal, subprocess, sys, tempfile, threading, time, json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import docfmt  # noqa: E402

PORT = int(os.environ.get('PORT', '8080'))
USER = os.environ.get('CONVERT_USER', 'kkul')
SECRET = os.environ.get('CONVERT_SECRET') or os.environ.get('GOTENBERG_API_BASIC_AUTH_PASSWORD', '')
MAX_BYTES = int(os.environ.get('CONVERT_MAX_BYTES', str(25 * 1024 * 1024)))
MAX_OUT = int(os.environ.get('CONVERT_MAX_OUT', str(80 * 1024 * 1024)))
TIMEOUT = int(os.environ.get('CONVERT_TIMEOUT', '100'))
UNO_PORT = os.environ.get('UNO_PORT', '2003')
RESTART_AFTER = int(os.environ.get('LO_RESTART_AFTER', '20'))
CTYPES = {
    'pdf': 'application/pdf', 'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    'doc': 'application/msword', 'xls': 'application/vnd.ms-excel', 'ppt': 'application/vnd.ms-powerpoint',
    'odt': 'application/vnd.oasis.opendocument.text', 'ods': 'application/vnd.oasis.opendocument.spreadsheet',
    'odp': 'application/vnd.oasis.opendocument.presentation', 'rtf': 'application/rtf',
    'hwp': 'application/x-hwp', 'hwpx': 'application/hwp+zip', 'txt': 'text/plain; charset=utf-8',
    'md': 'text/markdown; charset=utf-8', 'html': 'text/html; charset=utf-8', 'csv': 'text/csv; charset=utf-8',
    'png': 'image/png', 'jpg': 'image/jpeg', 'zip': 'application/zip',
}

busy = threading.Lock()


class Uno:
    """상주 LibreOffice (unoserver). 죽었거나 N건을 넘기면 새로 띄워요."""
    def __init__(self):
        self.p = None; self.count = 0; self.prof = tempfile.mkdtemp(prefix='lo-prof-')
        if os.path.isdir(docfmt.PROFILE_SEED):  # 이미지에 미리 만든 프로필(확장 등록·설정 캐시)로 첫 시작을 줄여요
            shutil.copytree(docfmt.PROFILE_SEED, self.prof, symlinks=True, dirs_exist_ok=True)
        self.tmp = tempfile.mkdtemp(prefix='lo-tmp-')
        self.lock = threading.Lock()  # LibreOffice 임시 파일은 여기에만 (요청마다 비워요)

    def clean_tmp(self):
        """LibreOffice가 남긴 임시 파일만 지워요 (실행 중인 LibreOffice의 폴더는 남겨 둬요)."""
        for root, _dirs, files in os.walk(self.tmp):
            for n in files:
                try: os.unlink(os.path.join(root, n))
                except OSError: pass

    def ensure(self):
        with self.lock:
            self._ensure()

    def _ensure(self):
        if self.p and self.p.poll() is None and self.count < RESTART_AFTER:
            return
        self.stop(); shutil.rmtree(self.tmp, ignore_errors=True); os.makedirs(self.tmp, exist_ok=True)
        self.p = subprocess.Popen([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'unosrv.py'), '--interface', '127.0.0.1', '--port', UNO_PORT,
                                   '--user-installation', self.prof],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                                  env=dict(os.environ, TMPDIR=self.tmp))
        self.count = 0
        import socket
        for _ in range(300):  # 최대 30초: unoserver(XML-RPC)가 포트를 열면 준비된 거예요
            if self.p.poll() is not None:
                return
            try:
                socket.create_connection(('127.0.0.1', int(UNO_PORT)), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.1)

    def stop(self):
        if self.p and self.p.poll() is None:
            try: os.killpg(self.p.pid, signal.SIGKILL)
            except ProcessLookupError: pass
        self.p = None


uno = Uno()


def authorized(h):
    a = h.headers.get('authorization', '')
    if not SECRET or not a.startswith('Basic '):
        return False
    try:
        u, _, p = base64.b64decode(a[6:]).decode().partition(':')
    except Exception:
        return False
    return hmac.compare_digest(u, USER) & hmac.compare_digest(p, SECRET)


def parse_multipart_file(body, ctype):
    """multipart/form-data 본문에서 첫 파일 파트(이름, 바이트)를 꺼내요. v1 호환용이라 파일 한 개만 봐요."""
    import re
    m = re.search(r'boundary=(?:"([^"]+)"|([^;\s]+))', ctype or '')
    if not m:
        return None, None
    b = b'--' + (m.group(1) or m.group(2)).encode()
    for part in body.split(b)[1:]:
        if part.startswith(b'--'):
            break
        head, sep, data = part.partition(b'\r\n\r\n')
        if not sep:
            continue
        h = head.decode('utf-8', 'replace')
        fn = re.search(r'filename\*?=(?:"([^"]*)"|([^;\r\n]*))', h)
        if not fn:
            continue
        if data.endswith(b'\r\n'):
            data = data[:-2]
        return (fn.group(1) or fn.group(2) or '').strip(), data
    return None, None


class H(BaseHTTPRequestHandler):
    server_version = 'kkul-doc'
    sys_version = ''

    def log_message(self, fmt, *args):  # 파일 이름·내용은 남기지 않아요 (경로·상태·시간만)
        pass

    def _json(self, status, code):
        b = json.dumps({'ok': False, 'error': code}).encode()
        self.send_response(status); self.send_header('content-type', 'application/json'); self.send_header('content-length', str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        if self.path == '/health':
            b = b'ok'; self.send_response(200); self.send_header('content-length', '2'); self.end_headers(); self.wfile.write(b)
        else:
            self._json(404, 'not_found')

    def do_POST(self):
        t0 = time.time()
        u = urlparse(self.path)
        compat = u.path == '/forms/libreoffice/convert'
        if u.path != '/convert' and not compat:
            return self._json(404, 'not_found')
        if not authorized(self):
            return self._json(401, 'unauthorized')
        q = parse_qs(u.query)
        src_ext = (q.get('from', [''])[0]).lower(); dst_ext = 'pdf' if compat else (q.get('to', [''])[0]).lower()
        if not compat and not docfmt.supported(src_ext, dst_ext):
            return self._json(415, 'unsupported')
        n = self.headers.get('content-length')
        if n is None or not n.isdigit():
            return self._json(411, 'length_required')
        n = int(n)
        if n == 0: return self._json(400, 'empty')
        if n > MAX_BYTES + (64 * 1024 if compat else 0): return self._json(413, 'too_large')
        if not busy.acquire(blocking=False):
            return self._json(503, 'busy')
        work = tempfile.mkdtemp(prefix='kc-')
        status, reason = 500, ''
        try:
            buf = bytearray(); left = n
            while left:
                chunk = self.rfile.read(min(left, 1 << 20))
                if not chunk: raise ConnectionError('short body')
                buf += chunk; left -= len(chunk)
            data = bytes(buf); del buf
            if compat:
                name, data = parse_multipart_file(data, self.headers.get('content-type'))
                src_ext = (os.path.splitext(name or '')[1].lstrip('.') or '').lower()
                if not data or not docfmt.supported(src_ext, 'pdf'):
                    status, reason = 400, 'compat_bad_form'
                    return self._json(400, 'bad_form')
            src = os.path.join(work, f'in.{src_ext}')
            with open(src, 'wb') as f:
                f.write(data)
            del data
            uno.ensure(); uno.count += 1
            out_path = os.path.join(work, 'result.bin')
            env = dict(os.environ, UNO_PORT=UNO_PORT, HOME=work, TMPDIR=work)
            try:
                r = subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), 'docfmt_cli.py'), src, src_ext, dst_ext, os.path.join(work, 'w'), out_path],
                                   capture_output=True, timeout=TIMEOUT, env=env, start_new_session=True)
            except subprocess.TimeoutExpired:
                uno.count = RESTART_AFTER  # LibreOffice가 붙잡혀 있을 수 있어 다음 요청 전에 새로 띄워요
                status, reason = 504, 'timeout'
                return self._json(504, 'timeout')
            if r.returncode != 0 or not os.path.exists(out_path):
                uno.count = RESTART_AFTER if r.returncode < 0 else uno.count
                status, reason = 422, (r.stderr.decode(errors='replace').strip().splitlines() or ['?'])[-1][:200]
                return self._json(422, 'convert_failed')
            ext = (r.stdout.decode().strip().splitlines() or [dst_ext])[-1]
            size = os.path.getsize(out_path)
            if size > MAX_OUT:
                status, reason = 413, 'output_too_large'
                return self._json(413, 'output_too_large')
            self.send_response(200)
            self.send_header('content-type', CTYPES.get(ext, 'application/octet-stream'))
            self.send_header('content-length', str(size))
            if not compat:
                self.send_header('x-kkul-ext', ext)
            self.end_headers()
            with open(out_path, 'rb') as f:
                shutil.copyfileobj(f, self.wfile, 1 << 20)
            status = 200
        except (ConnectionError, BrokenPipeError):
            status, reason = 400, 'client_disconnected'
        finally:
            shutil.rmtree(work, ignore_errors=True)
            uno.clean_tmp()
            busy.release()
            print(json.dumps({'ev': 'convert', 'api': 'v1' if compat else 'v2', 'from': src_ext, 'to': dst_ext, 'mb': round(n / 1048576, 1), 'status': status, 'ms': int((time.time() - t0) * 1000), 'reason': reason}), flush=True)


if __name__ == '__main__':
    threading.Thread(target=uno.ensure, daemon=True).start()  # 첫 요청 전에 LibreOffice를 미리 띄워요
    ThreadingHTTPServer(('0.0.0.0', PORT), H).serve_forever()
