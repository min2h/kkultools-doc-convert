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


def sniff(path: str) -> str:
    """Real format by content: 'dwg', 'dxf' (ASCII or binary) or '' - users rename files."""
    with open(path, "rb") as f:
        head = f.read(64)
    if head[:4] == b"AC10" or head[:4] == b"AC20" or head[:2] == b"MC":
        return "dwg"
    if head.startswith(b"AutoCAD Binary DXF"):
        return "dxf"
    txt = head.lstrip(b"\xef\xbb\xbf").lstrip()
    if txt[:1] == b"0" and b"SECTION" in head or txt[:1] == b"9" or b"SECTION" in head[:32]:
        return "dxf"
    return ""


def load(path: str):
    try:
        with open(path, "rb") as f:
            binary = f.read(22).startswith(b"AutoCAD Binary DXF")
        if binary:  # recover mode reads ASCII only
            doc = ezdxf.readfile(path)
            auditor = doc.audit()
        else:
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


def _robust_box(lay, limit=200_000):
    """Bounds without far-away strays: when <=2% of the entities lie far outside the box of the rest
    (header $EXTMIN/$EXTMAX are never trusted - they are 0 or stale in many files), return that box."""
    from ezdxf import bbox as bb
    from ezdxf.math import BoundingBox2d, Vec2
    boxes = []
    for i, e in enumerate(lay):
        if i >= limit:
            return None
        try:
            b = bb.extents([e], fast=True)
        except Exception:
            continue
        if b.has_data:
            boxes.append(b)
    n = len(boxes)
    if n < 5:
        return None
    cx = sorted(b.center.x for b in boxes); cy = sorted(b.center.y for b in boxes)
    drop = max(2, int(n * 0.02))
    k = min(drop, n // 3)
    lo, hi = Vec2(cx[k], cy[k]), Vec2(cx[-1 - k], cy[-1 - k])
    span = max(hi.x - lo.x, hi.y - lo.y, 1e-9)
    pad = span * 10
    keep = [b for b in boxes if lo.x - pad <= b.center.x <= hi.x + pad and lo.y - pad <= b.center.y <= hi.y + pad]
    if len(keep) == n or n - len(keep) > drop:
        return None
    full = bb.extents(lay, fast=True)
    core = BoundingBox2d([p for b in keep for p in (Vec2(b.extmin), Vec2(b.extmax))])
    if max(full.size.x, full.size.y) < 20 * max(core.size.x, core.size.y):
        return None
    m = max(core.size.x, core.size.y) * 0.02
    return BoundingBox2d([core.extmin - Vec2(m, m), core.extmax + Vec2(m, m)])


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
    if is_model and rbox is None and view == "auto":
        rbox = _robust_box(lay)
        if rbox is not None:
            _render_one.outliers = True
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


FACE_TYPES = {"3DFACE", "POLYLINE", "MESH", "POLYMESH", "POLYFACE"}


def _is_face(e) -> bool:
    t = e.dxftype()
    if t == "3DFACE":
        return True
    if t == "POLYLINE":
        try:
            return e.is_poly_face_mesh or e.is_polygon_mesh
        except Exception:
            return False
    return False


def surface_model(msp, paper_2d=False) -> bool:
    """A 3D surface model (3DFACE / polyface / polygon mesh, typical for SketchUp/3ds/old R12 exports):
    the plan (top) view stacks every floor onto the roof outline - a meaningless web of triangle edges.
    True when face entities dominate model space and the model is clearly three-dimensional."""
    from ezdxf import bbox as bb
    if paper_2d:
        return False
    n = faces = 0
    for e in msp:
        n += 1
        faces += _is_face(e)
    if not n or faces / n < 0.6:
        return False
    try:
        ext = bb.extents(msp, fast=True)
    except Exception:
        return False
    if not ext.has_data:
        return False
    sx, sy, sz = ext.size.x, ext.size.y, ext.size.z
    return sz >= 0.25 * max(sx, sy, 1e-9)


def _faces_3d(msp, limit=400_000):
    """World-space polygons (faces) and polylines (edges) of a 3D model, INSERTs and meshes exploded."""
    from ezdxf.disassemble import recursive_decompose
    faces, lines = [], []
    for e in recursive_decompose(msp):
        if len(faces) + len(lines) >= limit:
            break
        t = e.dxftype()
        try:
            if t == "POLYLINE" and (e.is_poly_face_mesh or e.is_polygon_mesh):
                for f in e.virtual_entities():
                    v = [tuple(p) for p in f.wcs_vertices()]
                    faces.append(v[:3] if v[3] == v[2] else v)
            elif t in ("3DFACE", "SOLID", "TRACE"):
                v = [tuple(p) for p in e.wcs_vertices()]
                if len(v) == 4 and v[3] == v[2]:
                    v = v[:3]
                faces.append(v)
            elif t == "LINE":
                lines.append([tuple(e.dxf.start), tuple(e.dxf.end)])
            elif t in ("POLYLINE", "LWPOLYLINE"):
                pts = [tuple(p) for p in e.points_in_wcs()] if t == "LWPOLYLINE" else [tuple(v.dxf.location) for v in e.vertices]
                if len(pts) > 1:
                    lines.append(pts)
        except Exception:
            continue
    return faces, lines


def render_3d(doc, opts):
    """Hidden-line isometric view (from the south-east, Z up) of a 3D surface model, one A3 page.
    Painter's algorithm: faces sorted far-to-near, filled white with dark edges."""
    import math
    import numpy as np
    faces, lines = _faces_3d(doc.modelspace())
    if not faces:
        raise CadError("3D model without faces", "cad_3d")
    az, el = math.radians(-45), math.radians(35.264)
    rz = np.array([[math.cos(az), -math.sin(az), 0], [math.sin(az), math.cos(az), 0], [0, 0, 1]])
    up = np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]])
    rx = np.array([[1, 0, 0], [0, math.cos(el), -math.sin(el)], [0, math.sin(el), math.cos(el)]])
    R = (rx @ up @ rz).T
    P = [np.asarray(f, dtype=float) @ R for f in faces]
    L = [np.asarray(l, dtype=float) @ R for l in lines]
    allp = np.vstack(P + L)
    mn, mx = allp[:, :2].min(0), allp[:, :2].max(0)
    span = np.maximum(mx - mn, 1e-9)
    w, h = PAGE_MM.get(opts.get("page", "auto"), PAGE_MM["a3"])
    w, h = w * 72 / 25.4, h * 72 / 25.4
    orient = opts.get("orient", "auto")
    land = span[0] > span[1] if orient == "auto" else orient == "landscape"
    if land != (w > h):
        w, h = h, w
    m = 8 * 72 / 25.4
    s = min((w - 2 * m) / span[0], (h - 2 * m) / span[1])
    ox, oy = (w - span[0] * s) / 2, (h - span[1] * s) / 2
    lw = max(0.15, min(0.5, 1500 / max(len(P), 1)))
    out = fitz.open()
    pg = out.new_page(width=w, height=h)
    sh = pg.new_shape()

    def pts(a):
        return [fitz.Point(ox + (x - mn[0]) * s, h - oy - (y - mn[1]) * s) for x, y in a[:, :2]]

    items = [(f[:, 2].mean(), 0, f) for f in P] + [(l[:, 2].max(), 1, l) for l in L]
    items.sort(key=lambda t: t[0])
    color = (0, 0, 0)
    for _, kind, a in items:
        q = pts(a)
        if kind == 0:
            sh.draw_polyline(q + [q[0]])
            sh.finish(color=color, fill=(1, 1, 1), width=lw, closePath=True, lineJoin=1)
        else:
            sh.draw_polyline(q)
            sh.finish(color=color, width=lw, closePath=False)
    sh.commit()
    out.set_metadata({"producer": "kkultools CAD (ezdxf 3D view)"})
    return out.tobytes(deflate=True, garbage=3)


def warnings_for(doc) -> list:
    """Things that render incompletely - shown to the user next to the result."""
    w = []
    try:
        xrefs = {b.name for b in doc.blocks if b.block_record.is_xref}
    except Exception:
        xrefs = set()
    if xrefs and any(e.dxftype() == "INSERT" and e.dxf.name in xrefs for e in doc.entitydb.values()):
        w.append("cad_xref")
    types = {e.dxftype() for e in doc.entitydb.values()}
    if types & {"IMAGE", "PDFUNDERLAY", "DWFUNDERLAY", "DGNUNDERLAY", "OLE2FRAME", "OLEFRAME"}:
        w.append("cad_image")
    if types & {"3DSOLID", "BODY", "REGION", "SURFACE", "EXTRUDEDSURFACE", "LOFTEDSURFACE", "REVOLVEDSURFACE",
                "SWEPTSURFACE", "PLANESURFACE", "NURBSURFACE"}:
        w.append("cad_solid_part")
    if types & {"ACAD_PROXY_ENTITY"}:
        w.append("cad_proxy")
    return w


def sanity_check(pdf: bytes):
    """Refuse a result that would mislead: a blank page, or a drawing squeezed into a speck because
    stray far-away objects set the extents.  Checks each page at 36 dpi."""
    with fitz.open("pdf", pdf) as d:
        blank = speck = 0
        for pg in d:
            pix = pg.get_pixmap(dpi=36, colorspace=fitz.csGRAY, alpha=False)
            buf, W, H = pix.samples, pix.width, pix.height
            xs, ys, ink = [], [], 0
            for y in range(H):
                row = buf[y * pix.stride:y * pix.stride + W]
                for x in range(W):
                    if row[x] < 200:
                        ink += 1
                        xs.append(x)
                        ys.append(y)
            if ink < 3:
                blank += 1
                continue
            # bounds of 98% of the ink (one far-off stray object must not count)
            xs.sort(); ys.sort()
            k = int(len(xs) * 0.01)
            bw = (xs[-1 - k] - xs[k] + 1) / W
            bh = (ys[-1 - k] - ys[k] + 1) / H
            if max(bw, bh) < 0.12:
                speck += 1
        n = d.page_count
    if blank == n:
        raise CadError("rendered page is blank", "cad_blank")
    if blank + speck == n:
        raise CadError("drawing renders as a speck (far-away stray objects?)", "cad_speck")


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
    if surface_model(msp, paper_2d) and opts.get("view3d", "auto") != "plan":
        render.errors = []
        render.view3d = True
        pdf = render_3d(doc, opts)
        if fmt == "pdf":
            return pdf
        with fitz.open("pdf", pdf) as d:
            if fmt == "png":
                return [d[0].get_pixmap(dpi=opts.get("dpi", 150), alpha=False).tobytes("png")]
            return [d[0].get_svg_image(text_as_path=True).encode()]
    render.view3d = False
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
    render.outliers = getattr(_render_one, "outliers", False)
    _render_one.outliers = False
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
        real = sniff(src)
        if real and real != ext.lstrip("."):
            info["renamed_from"] = real  # a DWG saved as .dxf (or the reverse)
            ext = "." + real
        elif not real:
            raise CadError("not a DXF/DWG file", "cad_read")
        if ext == ".dwg":
            path = dwg_to_dxf(src, td)
            info["t_dwg2dxf"] = round(time.time() - t0, 2)
        t1 = time.time()
        doc, aud = load(path)
        info["dxfversion"] = doc.dxfversion
        info["acad_release"] = doc.acad_release
        info["audit_errors"] = len(aud.errors)
        info["ko_styles"] = fix_korean_fonts(doc)
        info["warnings"] = warnings_for(doc)
        info["t_load"] = round(time.time() - t1, 2)
        t2 = time.time()
        fmt = os.path.splitext(dst)[1].lstrip(".").lower()
        res = render(doc, opts, fmt)
        info["t_render"] = round(time.time() - t2, 2)
        info["layout_errors"] = getattr(render, "errors", [])
        info["view3d"] = getattr(render, "view3d", False)
        if info["view3d"]:
            info["warnings"].append("cad_view3d")
        if getattr(render, "outliers", False):
            info["warnings"].append("cad_outliers")
        if isinstance(res, bytes):
            sanity_check(res)
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
