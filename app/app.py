"""HTTP front end for the conversion engine.

POST /convert?from=<ext>&to=<ext>   raw file body, Basic auth
POST /forms/libreoffice/convert     Gotenberg-compatible multipart, always returns PDF
GET  /health
"""

import base64
import hmac
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import docfmt  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8080"))
USER = os.environ.get("CONVERT_USER", "kkul")
SECRET = os.environ.get("CONVERT_SECRET") or os.environ.get("GOTENBERG_API_BASIC_AUTH_PASSWORD", "")
MAX_BYTES = int(os.environ.get("CONVERT_MAX_BYTES", str(25 * 1024 * 1024)))
MAX_OUT = int(os.environ.get("CONVERT_MAX_OUT", str(80 * 1024 * 1024)))
TIMEOUT = int(os.environ.get("CONVERT_TIMEOUT", "100"))
UNO_PORT = os.environ.get("UNO_PORT", "2003")
RESTART_AFTER = int(os.environ.get("LO_RESTART_AFTER", "20"))

CTYPES = {
    "pdf": "application/pdf",
    "svg": "image/svg+xml",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "doc": "application/msword",
    "xls": "application/vnd.ms-excel",
    "ppt": "application/vnd.ms-powerpoint",
    "odt": "application/vnd.oasis.opendocument.text",
    "ods": "application/vnd.oasis.opendocument.spreadsheet",
    "odp": "application/vnd.oasis.opendocument.presentation",
    "rtf": "application/rtf",
    "hwp": "application/x-hwp",
    "hwpx": "application/hwp+zip",
    "txt": "text/plain; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "html": "text/html; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
    "png": "image/png",
    "jpg": "image/jpeg",
    "zip": "application/zip",
}

busy = threading.Lock()


class Uno:
    """Resident LibreOffice via unoserver, restarted every RESTART_AFTER jobs."""

    def __init__(self):
        self.p = None
        self.count = 0
        self.prof = tempfile.mkdtemp(prefix="lo-prof-")
        if os.path.isdir(docfmt.PROFILE_SEED):
            shutil.copytree(docfmt.PROFILE_SEED, self.prof, symlinks=True, dirs_exist_ok=True)
        self.tmp = tempfile.mkdtemp(prefix="lo-tmp-")
        self.lock = threading.Lock()

    def clean_tmp(self):
        # Remove files only; the running instance still owns the directories.
        for root, _dirs, files in os.walk(self.tmp):
            for n in files:
                try:
                    os.unlink(os.path.join(root, n))
                except OSError:
                    pass

    def ensure(self):
        with self.lock:
            self._ensure()

    def _ensure(self):
        if self.p and self.p.poll() is None and self.count < RESTART_AFTER:
            return
        self.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.makedirs(self.tmp, exist_ok=True)
        self.p = subprocess.Popen(
            [
                sys.executable,
                os.path.join(HERE, "unosrv.py"),
                "--interface",
                "127.0.0.1",
                "--port",
                UNO_PORT,
                "--user-installation",
                self.prof,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=dict(os.environ, TMPDIR=self.tmp),
        )
        self.count = 0
        for _ in range(300):  # up to 30 s for the XML-RPC port to open
            if self.p.poll() is not None:
                return
            try:
                socket.create_connection(("127.0.0.1", int(UNO_PORT)), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.1)

    def stop(self):
        if self.p and self.p.poll() is None:
            try:
                os.killpg(self.p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.p = None


uno = Uno()


def authorized(h):
    a = h.headers.get("authorization", "")
    if not SECRET or not a.startswith("Basic "):
        return False
    try:
        u, _, p = base64.b64decode(a[6:]).decode().partition(":")
    except Exception:
        return False
    return hmac.compare_digest(u, USER) & hmac.compare_digest(p, SECRET)


def parse_multipart_file(body, ctype):
    """Return (filename, bytes) of the first file part, or (None, None)."""
    m = re.search(r'boundary=(?:"([^"]+)"|([^;\s]+))', ctype or "")
    if not m:
        return None, None
    b = b"--" + (m.group(1) or m.group(2)).encode()
    for part in body.split(b)[1:]:
        if part.startswith(b"--"):
            break
        head, sep, data = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        h = head.decode("utf-8", "replace")
        fn = re.search(r'filename\*?=(?:"([^"]*)"|([^;\r\n]*))', h)
        if not fn:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]
        return (fn.group(1) or fn.group(2) or "").strip(), data
    return None, None


class H(BaseHTTPRequestHandler):
    server_version = "kkul-doc"
    sys_version = ""

    def log_message(self, fmt, *args):
        # File names must never reach the logs; do_POST emits its own summary line.
        pass

    def _drain(self):
        # Replying without reading the body makes the Cloud Run front end return 502.
        if self.command != "POST" or getattr(self, "_body_read", False):
            return
        self._body_read = True
        n = self.headers.get("content-length") or ""
        if not n.isdigit() or int(n) > MAX_BYTES + (1 << 20):
            self.close_connection = True
            return
        left = int(n)
        try:
            self.connection.settimeout(30)
            while left:
                c = self.rfile.read(min(left, 1 << 20))
                if not c:
                    break
                left -= len(c)
        except OSError:
            self.close_connection = True

    def _json(self, status, code):
        self._drain()
        b = json.dumps({"ok": False, "error": code}).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("content-length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self._json(404, "not_found")

    def do_POST(self):
        t0 = time.time()
        u = urlparse(self.path)
        compat = u.path == "/forms/libreoffice/convert"
        if u.path != "/convert" and not compat:
            return self._json(404, "not_found")
        if not authorized(self):
            return self._json(401, "unauthorized")
        q = parse_qs(u.query)
        src_ext = q.get("from", [""])[0].lower()
        dst_ext = "pdf" if compat else q.get("to", [""])[0].lower()
        if not compat and not docfmt.supported(src_ext, dst_ext):
            return self._json(415, "unsupported")
        n = self.headers.get("content-length")
        if n is None or not n.isdigit():
            return self._json(411, "length_required")
        n = int(n)
        if n == 0:
            return self._json(400, "empty")
        if n > MAX_BYTES + (64 * 1024 if compat else 0):
            return self._json(413, "too_large")
        if not busy.acquire(blocking=False):
            return self._json(503, "busy")
        work = tempfile.mkdtemp(prefix="kc-")
        status, reason = 500, ""
        try:
            buf = bytearray()
            left = n
            while left:
                chunk = self.rfile.read(min(left, 1 << 20))
                if not chunk:
                    raise ConnectionError("short body")
                buf += chunk
                left -= len(chunk)
            self._body_read = True
            data = bytes(buf)
            del buf
            if compat:
                name, data = parse_multipart_file(data, self.headers.get("content-type"))
                src_ext = (os.path.splitext(name or "")[1].lstrip(".") or "").lower()
                if not data or not docfmt.supported(src_ext, "pdf"):
                    status, reason = 400, "compat_bad_form"
                    return self._json(400, "bad_form")
            src = os.path.join(work, f"in.{src_ext}")
            with open(src, "wb") as f:
                f.write(data)
            del data
            is_cad = docfmt.family(src_ext) == "cad"
            if not is_cad:  # CAD does not use LibreOffice
                uno.ensure()
                uno.count += 1
            out_path = os.path.join(work, "result.bin")
            env = dict(os.environ, UNO_PORT=UNO_PORT, HOME=work, TMPDIR=work)
            if is_cad:
                import cadconv

                env["CAD_OPTS"] = json.dumps(cadconv.parse_opts({k: v[0] for k, v in q.items()}))
                env["XDG_CACHE_HOME"] = work
            cmd = [
                sys.executable,
                os.path.join(os.path.dirname(__file__), "docfmt_cli.py"),
                src,
                src_ext,
                dst_ext,
                os.path.join(work, "w"),
                out_path,
            ]
            try:
                r = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=TIMEOUT,
                    env=env,
                    start_new_session=True,
                )
            except subprocess.TimeoutExpired:
                if not is_cad:
                    uno.count = RESTART_AFTER  # LibreOffice may be wedged
                status, reason = 504, "timeout"
                return self._json(504, "timeout")
            if r.returncode != 0 or not os.path.exists(out_path):
                uno.count = RESTART_AFTER if r.returncode < 0 and not is_cad else uno.count
                err = r.stderr.decode(errors="replace").strip().splitlines() or ["?"]
                status, reason = 422, err[-1][:200]
                return self._json(422, "convert_failed")
            ext = (r.stdout.decode().strip().splitlines() or [dst_ext])[-1]
            size = os.path.getsize(out_path)
            if size > MAX_OUT:
                status, reason = 413, "output_too_large"
                return self._json(413, "output_too_large")
            self.send_response(200)
            self.send_header("content-type", CTYPES.get(ext, "application/octet-stream"))
            self.send_header("content-length", str(size))
            if not compat:
                self.send_header("x-kkul-ext", ext)
            self.end_headers()
            with open(out_path, "rb") as f:
                shutil.copyfileobj(f, self.wfile, 1 << 20)
            status = 200
        except (ConnectionError, BrokenPipeError):
            status, reason = 400, "client_disconnected"
        finally:
            shutil.rmtree(work, ignore_errors=True)
            uno.clean_tmp()
            busy.release()
            log = {
                "ev": "convert",
                "api": "v1" if compat else "v2",
                "from": src_ext,
                "to": dst_ext,
                "mb": round(n / 1048576, 1),
                "status": status,
                "ms": int((time.time() - t0) * 1000),
                "reason": reason,
            }
            print(json.dumps(log), flush=True)


if __name__ == "__main__":
    threading.Thread(target=uno.ensure, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
