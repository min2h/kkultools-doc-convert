# kkultools-doc-convert

HTTP server that converts between HWP/HWPX, Office, ODF, PDF, Markdown and images. It is the conversion backend of [kkultools.com](https://kkultools.com).

## Supported conversions

| Input | Output |
|---|---|
| `docx doc odt rtf txt md html hwp hwpx` | `pdf docx hwpx hwp odt doc rtf txt html md png` |
| `xlsx xls ods csv cell` | `pdf xlsx xls ods csv html png` |
| `pptx ppt odp` | `pdf pptx ppt odp png` |
| `pdf` | `docx xlsx hwpx txt png jpg` |
| `png jpg` | `pdf` |

Multi-page images and multi-sheet CSV are returned as a zip. Conversions to HWP/HWPX and from PDF to DOCX/HWPX are less reliable than the rest. Password-protected files, HWP 3.0 and older, and OCR are not supported.

Built on LibreOffice with [H2Orestart](https://github.com/ebandal/H2Orestart), [rhwp](https://github.com/edwardkim/rhwp), [pypandoc-hwpx](https://pypi.org/project/pypandoc-hwpx/), pandoc, [pdf2docx](https://github.com/ArtifexSoftware/pdf2docx), [pdfplumber](https://github.com/jsvine/pdfplumber), poppler and img2pdf.

## Running with Docker

```sh
docker build -t doc-convert .
docker run --rm -p 8080:8080 -e CONVERT_SECRET=change-me doc-convert
curl -u kkul:change-me --data-binary @report.hwp \
  'http://localhost:8080/convert?from=hwp&to=docx' -o report.docx
```

`deploy.sh` builds and deploys to Google Cloud Run (`PROJECT_ID=<id> ./deploy.sh all`).

## API

All conversion requests use HTTP Basic auth (`Authorization: Basic ...`, user `CONVERT_USER`, password `CONVERT_SECRET`).

`POST /convert?from=<ext>&to=<ext>`
: Request body is the raw file. Responds with the converted file; the `x-kkul-ext` header gives the actual extension (`zip` for multi-file results).

`POST /forms/libreoffice/convert`
: Gotenberg-compatible multipart form with one `files` field; the extension of its filename selects the input format. Always returns PDF.

`GET /health`
: Returns `ok`. No auth.

Errors are JSON (`{"ok": false, "error": "..."}`): 400 bad request, 401 unauthorized, 411/413 missing length or too large, 415 unsupported pair, 422 conversion failed, 503 busy, 504 timeout.

## Configuration

| Variable | Default | |
|---|---|---|
| `CONVERT_SECRET` | | Basic auth password (required). `GOTENBERG_API_BASIC_AUTH_PASSWORD` is also accepted. |
| `CONVERT_USER` | `kkul` | Basic auth user |
| `CONVERT_MAX_BYTES` | 25 MiB | Max upload size |
| `CONVERT_MAX_OUT` | 80 MiB | Max output size |
| `CONVERT_TIMEOUT` | `100` | Seconds per conversion |
| `MAX_PDF_EDIT_PAGES` | `60` | Page limit for PDF to DOCX/HWPX |
| `LO_RESTART_AFTER` | `20` | Restart LibreOffice after this many conversions |
| `RHWP_MEM_BYTES` | 1.2 GB | Address-space limit for rhwp |
| `PORT` | `8080` | Listen port |

The server handles one conversion at a time. Uploaded files are kept in a per-request temporary directory that is removed after the response; file names and contents are not logged. Macros and external links in documents are disabled.

## License

AGPL-3.0, see [LICENSE](LICENSE). The AGPL applies because PyMuPDF (used by pdf2docx) is AGPL. Bundled components keep their own licenses: LibreOffice (MPL-2.0), H2Orestart (GPL-3.0), rhwp (MIT), pandoc (GPL-2.0+), pdfplumber (MIT), fonts (OFL/GPL).
