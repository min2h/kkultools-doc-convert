# kkultools-doc-convert

The document conversion server behind **[kkultools.com](https://kkultools.com) Pro "문서 변환"** —
any-to-any conversion between Korean Hangul (HWP/HWPX), Word, Excel, PowerPoint, PDF, ODF and Markdown.
It runs as a single container on Google Cloud Run (scale to zero). Files live only in a per-request temp
folder (in-memory filesystem on Cloud Run) and are deleted as soon as the response is sent; file names and
contents are never logged.

This repository is published under the **GNU AGPL-3.0** because the server links PyMuPDF (AGPL) through
pdf2docx. Only the conversion server lives here — the kkultools.com website and Cloudflare Worker are not part of it.

## What it converts

| Input | Outputs |
|---|---|
| Writer: `docx doc odt rtf txt md html hwp hwpx` | `pdf docx hwpx hwp odt doc rtf txt html md png` |
| Calc: `xlsx xls ods csv cell` (Hancell 2014+) | `pdf xlsx xls ods csv html png` |
| Impress: `pptx ppt odp` | `pdf pptx ppt odp png` |
| PDF | `docx xlsx hwpx txt png jpg` |
| Image: `png jpg` | `pdf` |

Multi-page PNG/JPG and multi-sheet CSV results come back as a ZIP. **Beta** (check the result): Word → HWPX/HWP,
PDF → DOCX/HWPX, Markdown → HWPX.

## How

* **LibreOffice 26.8** (from the `gotenberg/gotenberg:8.37.0-libreoffice-cloudrun` base image) kept resident via
  [unoserver](https://github.com/unoconv/unoserver), restarted every 20 conversions; each conversion runs in a child
  process with a hard timeout.
* **[H2Orestart](https://github.com/ebandal/H2Orestart)** (GPL-3.0) — LibreOffice extension that reads HWP/HWPX.
* **[rhwp](https://github.com/edwardkim/rhwp)** (MIT) — HWP ↔ HWPX with layout intact, HWPX re-serialization,
  and a fallback renderer. Runs under a memory cap (`prlimit --as`).
* **[pypandoc-hwpx](https://pypi.org/project/pypandoc-hwpx/)** (MIT) + pandoc — DOCX/Markdown → HWPX, then validated
  with [python-hwpx](https://pypi.org/project/python-hwpx/) `hwpx-validate-package`.
* **[pdf2docx](https://github.com/ArtifexSoftware/pdf2docx)** / PyMuPDF (AGPL-3.0) — PDF → DOCX.
* **[pdfplumber](https://github.com/jsvine/pdfplumber)** (MIT) + openpyxl — PDF tables → XLSX.
* poppler-utils, img2pdf, Korean fonts (Nanum, Noto CJK, UnFonts) with font aliases for common Hancom/Windows fonts.
* Hardened LibreOffice config: macros disabled, external links/updates blocked (`libreoffice/kkul-security.xcd`).

## API

```
POST /convert?from=hwp&to=docx     body = file bytes, Basic auth  kkul:<shared secret>
  200 → converted bytes, header  x-kkul-ext: <actual extension>  (zip for multi-page images / multi-sheet CSV)
  400 bad request · 401 auth · 411/413 size · 415 unsupported pair · 422 conversion failed · 503 busy · 504 timeout
POST /forms/libreoffice/convert   Gotenberg-compatible multipart (field "files", filename document.<ext>) → PDF
GET  /health
```

The secret comes from the environment (`CONVERT_SECRET`, or `GOTENBERG_API_BASIC_AUTH_PASSWORD` for compatibility).
Limits: `CONVERT_MAX_BYTES` (default 25 MB), `CONVERT_TIMEOUT` (100 s), one conversion at a time per instance.

## Run locally

```bash
docker build -t kkul-doc-convert .
docker run --rm -p 8080:8080 -e CONVERT_SECRET=change-me kkul-doc-convert
curl -u kkul:change-me --data-binary @report.hwp -H 'content-type: application/octet-stream' \
  'http://localhost:8080/convert?from=hwp&to=docx' -o report.docx
```

Deploying to your own Cloud Run project: `PROJECT_ID=<id> ./deploy.sh all` (creates the secret, a dedicated service
account, builds with Cloud Build and deploys with min 0 / max 2 instances, concurrency 1, 2 GiB).

---

## 한국어

꿀툴(kkultools.com) Pro **문서 변환**에 쓰는 변환 서버예요. 한글(HWP·HWPX)·워드·엑셀·파워포인트·PDF·ODF·마크다운을
서로 바꿔요. Google Cloud Run(도쿄)에서 컨테이너 하나로 돌고, 쓰지 않을 때는 0대로 줄어요.

* 파일은 요청마다 만든 임시 폴더(Cloud Run 메모리 파일 시스템)에만 있고, 응답을 보낸 즉시 지워요. 파일 이름과 내용은 기록하지 않아요.
* 문서 안의 매크로는 실행하지 않고, 외부 링크·자동 업데이트는 막아 둬요.
* PDF → 워드에 쓰는 PyMuPDF가 AGPL이라서 이 서버 소스를 **AGPL-3.0**으로 공개해요. 꿀툴 웹사이트·Worker 코드는 여기 없어요.
* **베타**(결과를 꼭 확인해 주세요): 워드 → 한글(HWPX·HWP), PDF → 워드·HWPX, 마크다운 → HWPX.
* 한계: 암호 걸린 문서, HWP 3.0 이하, 스캔한 PDF의 글자 인식(OCR)은 지원하지 않아요. 한컴 전용 글꼴은 비슷한 무료 글꼴로 바뀌어 줄바꿈이 조금 달라질 수 있어요.

## License

GNU Affero General Public License v3.0 — see [LICENSE](LICENSE). Third-party components keep their own licenses
(LibreOffice MPL-2.0, H2Orestart GPL-3.0, rhwp MIT, PyMuPDF AGPL-3.0, pdfplumber MIT, pandoc GPL-2.0+,
Gotenberg base image MIT, fonts SIL OFL / GPL).
