"""CAD (DWG/DXF) -> PDF/SVG/PNG.  LibreDWG dwg2dxf (GPLv3) + ezdxf drawing add-on (MIT) + PyMuPDF (AGPL).
Beta: 2D only (3D solids/ACIS are not drawn), missing xrefs/images are skipped, SHX fonts become TTF outlines.
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
    pass


DWG_MEM = int(os.environ.get("DWG_MEM_BYTES", str(1200 * 1024 * 1024)))


def dwg_to_dxf(src: str, workdir: str, timeout: int = 60) -> str:
    out = os.path.join(workdir, "conv.dxf")
    p = subprocess.run(["prlimit", f"--as={DWG_MEM}", "--", "dwg2dxf", "-y", "-o", out, src],
                       capture_output=True, timeout=timeout)
    if not os.path.exists(out) or os.path.getsize(out) < 100:
        raise CadError("dwg2dxf failed: " + p.stderr.decode(errors="replace")[-400:])
    return out


def load(path: str):
    try:
        doc, auditor = recover.readfile(path)
    except Exception as e:  # noqa
        raise CadError(f"cannot read DXF: {e}")
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
    pg = opts.get("page", "auto")
    orient = opts.get("orient", "auto")
    m = layout.Margins.all(5)
    if pg in PAGE_MM:
        w, h = PAGE_MM[pg]
        return layout.Page(w, h, layout.Units.mm, margins=m), orient
    if not is_model:
        try:
            p = layout.Page.from_dxf_layout(lay)
            if p.width > 1 and p.height > 1:
                return p, orient
        except Exception:
            pass
    return layout.Page(0, 0, layout.Units.mm, margins=m, max_width=841, max_height=1189), orient  # auto, cap A0


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
        raise CadError("drawing is empty")
    pages, errors = [], []
    for lay, is_model in layouts:
        try:
            pages.append(_render_one(doc, lay, is_model, cfg, opts, fmt))
        except Exception as e:  # one bad layout must not kill the job
            errors.append(f"{lay.name}: {type(e).__name__}: {e}"[:200])
    render.errors = errors
    if not pages:
        if any("NaN" in e or "empty bounding box" in e for e in errors):
            raise CadError("no 2D content we can draw (3D solids/ACIS or empty viewports only)")
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


def convert(src: str, dst: str, opts=None):
    opts = opts or {}
    ext = os.path.splitext(src)[1].lower()
    info = {}
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
        info = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
    info["t_total"] = round(time.time() - t, 2)
    info["maxrss_mb"] = round(max(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss) / 1024)
    print(json.dumps(info, ensure_ascii=False))
