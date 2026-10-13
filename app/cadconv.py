"""CAD (DWG/DXF/DWFx) -> PDF/SVG/PNG.  LibreDWG dwg2dxf (GPLv3) + ezdxf drawing add-on (MIT) + PyMuPDF (AGPL).
2D only (3D solids/ACIS are not drawn), missing xrefs/images are skipped, SHX fonts become TTF outlines.
DWFx is XPS underneath, so its 2D sheets are rendered by MuPDF.  Classic DWF (W2D streams) is not supported.
Pages: model space (if not empty) + every paper-space layout that has entities.
Options: mono (black on white), page=auto|a4|a3 ..., orient=auto|portrait|landscape.
"""
from __future__ import annotations
import os, re, subprocess, sys, tempfile, time

os.environ.setdefault("XDG_CACHE_HOME", tempfile.gettempdir())
import ezdxf
from ezdxf import recover
from ezdxf.fonts import fonts
from ezdxf.addons.drawing import Frontend, RenderContext, layout, config
import pymupdf as fitz

KO_TTF = "NanumGothic.ttf"
HANGUL = re.compile(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7a3]")
# common Korean CAD fonts (SHX big fonts and Windows TTF) -> installed Nanum
KO_FONT_HINT = re.compile(r"^(whg|hg|kor|han|ksc|gulim|batang|dotum|gungsuh|malgun|굴림|돋움|바탕|궁서|맑은|hy|h2|hm)", re.I)
for _shx in ("whgtxt.shx", "whgdtxt.shx", "whtgtxt.shx", "whtmtxt.shx", "hgtxt.shx", "kortxt.shx", "korean.shx"):
    fonts.SHX_FONTS[_shx] = KO_TTF
PAGE_MM = {"a4": (210, 297), "a3": (297, 420), "a2": (420, 594), "a1": (594, 841), "letter": (215.9, 279.4)}


class CadError(Exception):
    """str(e) is for logs; e.code is the user-facing error code (see worker CAD_ERRORS)."""

    def __init__(self, msg, code="cad_failed"):
        super().__init__(msg)
        self.code = code


# DWG header -> release.  R13-R2018+ are read by LibreDWG; R12 and older (and unknown) are rejected.
DWG_RELEASES = {"AC1012": "R13", "AC1014": "R14", "AC1015": "2000", "AC1018": "2004", "AC1021": "2007",
                "AC1024": "2010", "AC1027": "2013", "AC1032": "2018"}


def dwg_release(path: str) -> str:
    with open(path, "rb") as f:
        head = f.read(6).decode("latin-1")
    if head not in DWG_RELEASES:
        raise CadError(f"unsupported DWG version {head!r}", "cad_dwg_version")
    return DWG_RELEASES[head]


DWG_MEM = int(os.environ.get("DWG_MEM_BYTES", str(1200 * 1024 * 1024)))


def dwg_to_dxf(src: str, workdir: str, timeout: int = 60) -> str:
    dwg_release(src)
    out = os.path.join(workdir, "conv.dxf")
    p = subprocess.run(["prlimit", f"--as={DWG_MEM}", "--", "dwg2dxf", "-y", "-o", out, src],
                       capture_output=True, timeout=timeout)
    if not os.path.exists(out) or os.path.getsize(out) < 100:
        raise CadError("dwg2dxf failed: " + p.stderr.decode(errors="replace")[-400:], "cad_read")
    return out


def load(path: str):
    try:
        doc, auditor = recover.readfile(path)
    except Exception as e:  # noqa
        raise CadError(f"cannot read DXF: {e}", "cad_read")
    return doc, auditor


def fix_korean_fonts(doc) -> int:
    """Styles that are Korean by name, have a big font, or are used by Hangul text -> NanumGothic."""
    hangul_styles = set()
    for e in doc.entitydb.values():
        t = e.dxftype()
        if t in ("TEXT", "ATTRIB", "ATTDEF", "MTEXT", "DIMENSION"):
            try:
                s = e.plain_text() if t == "MTEXT" else (e.dxf.get("text", "") or "")
                if HANGUL.search(s):
                    hangul_styles.add((e.dxf.get("style", "Standard") or "Standard").lower())
            except Exception:
                pass
    n = 0
    for st in doc.styles:
        font = (st.dxf.get("font", "") or "").strip()
        big = (st.dxf.get("bigfont", "") or "").strip()
        name = st.dxf.name.lower()
        if KO_FONT_HINT.match(os.path.basename(font)) or (big and KO_FONT_HINT.match(big)) or name in hangul_styles \
                or (big and not font.lower().endswith((".ttf", ".ttc", ".otf"))):
            st.dxf.font = KO_TTF
            st.dxf.discard("bigfont")
            n += 1
    return n


def _page_for(lay, opts, is_model):
    """Paper size in mm.  Lineweights are absolute mm on this paper, so the page must be a real
    sheet: the old auto page was 1 drawing unit = 1 mm, which made metre/inch drawings a few cm wide
    and every 0.25-0.7 mm line a blob.  Model space -> A3 (or the chosen size), layouts -> their own
    plot paper when it looks sane, else A3."""
    pg = opts.get("page", "auto")
    orient = opts.get("orient", "auto")
    m = layout.Margins.all(8)
    if pg in PAGE_MM:
        w, h = PAGE_MM[pg]
        return layout.Page(w, h, layout.Units.mm, margins=m), orient
    if not is_model:
        try:
            p = layout.Page.from_dxf_layout(lay)
            if 50 <= p.width_in_mm <= 2500 and 50 <= p.height_in_mm <= 2500:
                return p, orient
        except Exception:
            pass
    w, h = PAGE_MM["a3"]
    return layout.Page(w, h, layout.Units.mm, margins=m), orient


def _orient(page, orient, box):
    w, h = page.width, page.height
    if not (w and h):
        return page
    if orient == "auto" and box is not None and box.has_data:
        land = box.size.x >= box.size.y
    elif orient in ("portrait", "landscape"):
        land = orient == "landscape"
    else:
        return page
    if land != (w >= h):
        w, h = h, w
    return layout.Page(w, h, page.units, margins=page.margins, max_width=page.max_width, max_height=page.max_height)


_CLS = re.compile(r"\.(C[0-9A-Za-z]+)\s*\{([^}]*)\}")


def inline_svg_classes(svg: str) -> str:
    """MuPDF's SVG reader ignores <style> CSS classes (closed paths come out filled black),
    so turn ezdxf's .Cn classes into presentation attributes."""
    attrs = {}
    for name, body in _CLS.findall(svg):
        parts = []
        for decl in body.split(";"):
            if ":" not in decl:
                continue
            k, v = (x.strip() for x in decl.split(":", 1))
            if k == "stroke-width" and v == "none":
                continue
            parts.append(f'{k}="{v}"')
        attrs[name] = " ".join(parts)
    return re.sub(r'class="(C[0-9A-Za-z]+)"', lambda m: attrs.get(m.group(1), m.group(0)), svg)


_HEX = re.compile(r"#([0-9a-fA-F]{6})\b")


def _legible(m):
    """Screen colours like yellow (2), cyan (4) or light grey (8/9) vanish on white paper.
    Darken anything brighter than ~60% luminance, keeping the hue (what viewers do on a white background)."""
    r, g, b = (int(m.group(1)[i:i + 2], 16) for i in (0, 2, 4))
    lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255
    if lum <= 0.6 or (r, g, b) == (255, 255, 255):
        return m.group(0)
    k = 0.45 / lum
    return "#%02x%02x%02x" % tuple(min(255, int(c * k)) for c in (r, g, b))


def legible_colors(svg: str) -> str:
    return re.sub(r"<style>[^<]*</style>", lambda m: _HEX.sub(_legible, m.group(0)), svg)


def _saved_view_box(doc):
    """Model-space window of the last saved view (*Active VPORT) - what AutoCAD shows on open / in the thumbnail."""
    from ezdxf.math import BoundingBox2d, Vec2
    try:
        v = doc.viewports.get("*Active")[0]
        c, h, ar = Vec2(v.dxf.center), float(v.dxf.height), float(v.dxf.aspect_ratio or 1)
        if h <= 0:
            return None
        w = h * ar
        return BoundingBox2d([c - Vec2(w / 2, h / 2), c + Vec2(w / 2, h / 2)])
    except Exception:
        return None


def _share_inside(lay, box, limit=3000):
    """Share of (up to `limit`) entities whose bbox center lies in `box`."""
    from ezdxf import bbox as bb
    n = inside = 0
    for e in lay:
        if n >= limit:
            break
        try:
            b = bb.extents([e], fast=True)
        except Exception:
            continue
        if not b.has_data:
            continue
        n += 1
        c = b.center
        inside += box.extmin.x <= c.x <= box.extmax.x and box.extmin.y <= c.y <= box.extmax.y
    return inside / n if n else 0.0


def _render_one(doc, lay, is_model, cfg, opts, fmt):
    """Render one layout to SVG (ezdxf SVGBackend: fast, text as outlines), then PDF/PNG via PyMuPDF.
    (ezdxf's PyMuPdfBackend is O(n^2)-ish on big drawings: >150 s for 100k lines.)"""
    from ezdxf import bbox as bb
    from ezdxf.addons.drawing import svg as svgmod
    ctx = RenderContext(doc)
    ctx.set_current_layout(lay)
    be = svgmod.SVGBackend()
    Frontend(ctx, be, config=cfg).draw_layout(lay, finalize=True)
    page, orient = _page_for(lay, opts, is_model)
    if is_model or opts.get("page", "auto") != "auto":
        box = bb.extents(lay, fast=True) if orient == "auto" else None
        page = _orient(page, orient, box)
    rbox = None
    view = opts.get("view", "auto")
    if is_model and view in ("saved", "auto"):
        rbox = _saved_view_box(doc)
        if view == "auto" and rbox is not None:
            ext = bb.extents(lay, fast=True)
            # auto: keep "zoom extents" unless stray far-away objects make the drawing a speck
            if not ext.has_data or ext.size.x * ext.size.y < 25 * rbox.size.x * rbox.size.y \
                    or _share_inside(lay, rbox) < 0.8:
                rbox = None
        if rbox is not None and opts.get("orient", "auto") == "auto" and page.width and page.height:
            page = _orient(page, "landscape" if rbox.size.x >= rbox.size.y else "portrait", None)
    st = layout.Settings(fit_page=True, scale=1, crop_at_margins=rbox is not None)
    svg_s = be.get_string(page, settings=st, render_box=rbox)
    if not opts.get("mono"):
        svg_s = legible_colors(svg_s)
    if fmt == "svg":
        return svg_s.encode()
    svg = inline_svg_classes(svg_s).encode()
    with fitz.open("svg", svg) as d:
        pdf = d.convert_to_pdf()
    if fmt == "png":
        with fitz.open("pdf", pdf) as d:
            return d[0].get_pixmap(dpi=opts.get("dpi", 150), alpha=False).tobytes("png")
    return pdf


def _min_hatch_distance(doc):
    """Skip hatch pattern lines closer than 1/3000 of the drawing (sub-pixel even on A0).
    Dense patterns otherwise explode to millions of lines: 36 MB SVG / 46 MB PDF / 68 s for one Autodesk sample."""
    from ezdxf import bbox as bb
    try:
        e = bb.extents(doc.modelspace(), fast=True)
        if e.has_data:
            return max(e.size.x, e.size.y) / 3000
    except Exception:
        pass
    return 0.0


def render(doc, opts=None, fmt="pdf"):
    opts = opts or {}
    cfg = config.Configuration(
        background_policy=config.BackgroundPolicy.WHITE,
        color_policy=config.ColorPolicy.BLACK if opts.get("mono") else config.ColorPolicy.COLOR,
        lineweight_policy=config.LineweightPolicy.ABSOLUTE,
        min_lineweight=1.2,  # 1/300 inch -> 0.1 mm hairline floor on paper
        lineweight_scaling=1.0,
        hatching_timeout=20,
        min_hatch_line_distance=_min_hatch_distance(doc),
    )
    layouts = []
    msp = doc.modelspace()
    if len(msp):
        layouts.append((msp, True))
    if opts.get("only") != "model":
        for name in doc.layout_names_in_taborder():
            if name.lower() == "model":
                continue
            psp = doc.paperspace(name)
            if any(e.dxftype() != "VIEWPORT" for e in psp) or len(psp.viewports()) > 1:
                layouts.append((psp, False))
    if not layouts:
        raise CadError("drawing is empty", "cad_empty")
    solid = {"3DSOLID", "BODY", "REGION", "SURFACE", "EXTRUDEDSURFACE", "LOFTEDSURFACE", "REVOLVEDSURFACE",
             "SWEPTSURFACE", "PLANESURFACE", "NURBSURFACE", "MESH", "LIGHT"}
    types = [e.dxftype() for e in msp]
    paper_2d = any(not m and any(e.dxftype() != "VIEWPORT" for e in lay) for lay, m in layouts)
    if types and sum(t in solid for t in types) / len(types) >= 0.5 and not paper_2d:
        # a 3D model with a few stray lines would come out as an almost blank page
        raise CadError("3D model (ACIS solids) - nothing meaningful to plot in 2D", "cad_3d")
    pages, errors = [], []
    for lay, is_model in layouts:
        try:
            pages.append(_render_one(doc, lay, is_model, cfg, opts, fmt))
        except Exception as e:  # one bad layout must not kill the job
            errors.append(f"{lay.name}: {type(e).__name__}: {e}"[:200])
    render.errors = errors
    if not pages:
        if any("NaN" in e or "empty bounding box" in e for e in errors):
            raise CadError("no 2D content we can draw (3D solids/ACIS or empty viewports only)", "cad_3d")
        raise CadError("nothing rendered: " + "; ".join(errors)[:300])
    if fmt != "pdf":
        return pages
    out = fitz.open()
    for b in pages:
        with fitz.open("pdf", b) as d:
            out.insert_pdf(d)
    out.set_metadata({"producer": "kkultools CAD (LibreDWG + ezdxf)"})
    return out.tobytes(deflate=True, garbage=3)


OPTS = {"mono": ("0", "1"), "page": ("auto", *PAGE_MM), "orient": ("auto", "portrait", "landscape"),
        "view": ("auto", "extents", "saved")}


def parse_opts(q: dict) -> dict:
    """Whitelist query options (mono=1, page=a3, orient=landscape, view=saved)."""
    o = {}
    for k, allowed in OPTS.items():
        v = str(q.get(k, "")).lower()
        if v in allowed:
            o[k] = v == "1" if k == "mono" else v
    return o


def _fallback_font() -> bytes:
    for p in ("/usr/share/fonts/truetype/nanum/NanumGothic.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if os.path.exists(p):
            return open(p, "rb").read()
    return b""


def fix_dwfx_fonts(src: str, dst: str) -> int:
    """Autodesk DWFx writers embed subset fonts whose Unicode cmap is empty while the <Glyphs> use
    UnicodeString only, so every character comes out as a .notdef box.  Swap such fonts for a
    complete one (NanumGothic: Latin + Hangul).  Returns how many fonts were replaced."""
    import io, zipfile
    from fontTools.ttLib import TTFont
    fb, n = _fallback_font(), 0
    with zipfile.ZipFile(src) as zi, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zo:
        for it in zi.infolist():
            data = zi.read(it)
            if fb and it.filename.lower().endswith(".ttf"):
                try:
                    t = TTFont(io.BytesIO(data), lazy=True)
                    if not any(c.isUnicode() and c.cmap for c in t["cmap"].tables):
                        data, n = fb, n + 1
                except Exception:
                    pass
            zo.writestr(it, data)
    return n


def dwfx_to_pdf(src: str) -> tuple[bytes, int]:
    """DWFx = XPS package.  Keep the 2D sheets, crop each to its drawing (DWFx writers often declare a
    Letter FixedPage and draw the plot inside it), drop Autodesk's 'additional content' notice and the
    tiny 3D thumbnails of eModel sections."""
    fixed = src + ".fonts.xps"
    try:
        if fix_dwfx_fonts(src, fixed):
            src = fixed
    except Exception:
        pass
    try:
        xps = fitz.open(src, filetype="xps")
    except Exception as e:
        raise CadError(f"cannot open DWFx: {e}", "cad_read")
    out = fitz.open()
    with xps:
        pdf = fitz.open("pdf", xps.convert_to_pdf())
    for pg in pdf:
        r = pg.rect
        if min(r.width, r.height) < 216:  # 3D eModel textures/thumbnails (< 3 inch); real sheets are >= A5
            continue
        txt = " ".join(pg.get_text().replace("\xa0", " ").split())
        if "Additional Content Available" in txt and len(txt) < 600:
            continue
        box = fitz.Rect()
        for kind, b in pg.get_bboxlog():
            if kind.startswith("ignore"):
                continue
            b = fitz.Rect(b) & r
            if b.is_empty:
                continue
            # skip tiny registration ticks in the page corners, they defeat the crop
            edge = 0.08 * min(r.width, r.height)
            if b.width < 0.02 * r.width and b.height < 0.02 * r.height and (
                    b.x1 < edge or b.x0 > r.width - edge or b.y1 < edge or b.y0 > r.height - edge):
                continue
            box |= b
        if box.is_empty or box.width < 36 or box.height < 36:
            continue
        pad = 0.02 * max(box.width, box.height)
        box = (box + (-pad, -pad, pad, pad)) & r
        k = max(1.0, 1190 / max(box.width, box.height))  # vector: bring small sheets up to A3
        np_ = out.new_page(width=box.width * k, height=box.height * k)
        np_.show_pdf_page(np_.rect, pdf, pg.number, clip=box)
    n = out.page_count
    if not n:
        raise CadError("DWFx has no 2D sheets (3D model only)", "cad_3d")
    out.set_metadata({"producer": "kkultools CAD (DWFx/XPS via MuPDF)"})
    return out.tobytes(deflate=True, garbage=3), n


def convert(src: str, dst: str, opts=None):
    opts = opts or {}
    ext = os.path.splitext(src)[1].lower()
    info = {}
    if ext == ".dwf":
        raise CadError("classic DWF (W2D) is not supported", "cad_dwf")
    if ext == ".dwfx":
        fmt = os.path.splitext(dst)[1].lstrip(".").lower()
        if fmt not in ("pdf", "png"):
            raise CadError("DWFx converts to PDF/PNG only", "cad_failed")
        pdf, n = dwfx_to_pdf(src)
        if fmt == "pdf":
            open(dst, "wb").write(pdf)
        else:
            with fitz.open("pdf", pdf) as d:
                base, e = os.path.splitext(dst)
                for i, p in enumerate(d, 1):
                    p.get_pixmap(dpi=150, alpha=False).save(dst if n == 1 else f"{base}-{i}{e}")
        return {"pages": n}
    with tempfile.TemporaryDirectory() as td:
        t0 = time.time()
        path = src
        if ext == ".dwg":
            path = dwg_to_dxf(src, td)
            info["t_dwg2dxf"] = round(time.time() - t0, 2)
        t1 = time.time()
        doc, aud = load(path)
        info["dxfversion"] = doc.dxfversion
        info["acad_release"] = doc.acad_release
        info["audit_errors"] = len(aud.errors)
        info["ko_styles"] = fix_korean_fonts(doc)
        info["t_load"] = round(time.time() - t1, 2)
        t2 = time.time()
        fmt = os.path.splitext(dst)[1].lstrip(".").lower()
        res = render(doc, opts, fmt)
        info["t_render"] = round(time.time() - t2, 2)
        info["layout_errors"] = getattr(render, "errors", [])
        if isinstance(res, bytes):
            open(dst, "wb").write(res)
            info["pages"] = fitz.open(dst).page_count
        else:
            info["pages"] = len(res)
            base, e = os.path.splitext(dst)
            for i, b in enumerate(res, 1):
                open(dst if len(res) == 1 else f"{base}-{i}{e}", "wb").write(b)
    return info


if __name__ == "__main__":
    import json, resource
    a = sys.argv[1:]
    opts = {"mono": "--mono" in a}
    for x in a:
        if x.startswith("--page="): opts["page"] = x[7:]
        if x.startswith("--orient="): opts["orient"] = x[9:]
        if x.startswith("--view="): opts["view"] = x[7:]
    files = [x for x in a if not x.startswith("--")]
    t = time.time()
    try:
        info = convert(files[0], files[1], opts)
        info["ok"] = True
    except Exception as e:
        info = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300], "code": getattr(e, "code", None)}
    info["t_total"] = round(time.time() - t, 2)
    info["maxrss_mb"] = round(max(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss) / 1024)
    print(json.dumps(info, ensure_ascii=False))
