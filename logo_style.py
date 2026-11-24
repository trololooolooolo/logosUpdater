#!/usr/bin/env python3
"""Graphic styles for station logos ("🖌 Logo style" in the UI), Pillow only.

A style is a dict of parameters (see DEFAULTS) applied in a fixed order:
background removal -> plate removal -> monochrome -> trim/fit -> bevel ->
shadow -> tile. The output keeps the source canvas (160x120 RGBA on Sync 3).

Styled files live in paths.styled_dir/<ECC>/<NAME>.PNG, next to _meta.json
({"files": {key: {base, preset, params, time}}, "presets": {...}}). They are a
third version of a file next to the image and logos_sync5/: the per-file pick
"styled" (logo_choices.json) makes the build put them into the image.
"""
import io
import json
import os
import time

from PIL import Image, ImageChops, ImageDraw, ImageFilter

DEFAULTS = {
    'remove_bg': True,    # key out the background colour (taken from the border), connected to the edge
    'tolerance': 40,      # max channel difference still counted as background (0-255)
    'bg_pick': 'auto',    # background colour to cut: 'auto' (from the border) | 'white' | 'black' | 'custom'
    'bg_custom': '#ffffff',  # the colour for bg_pick 'custom'
    'bg_scope': 'holes',  # with original colours: 'edge' = only the area connected to the canvas edge |
                          # 'holes' = + letter holes (o, a, d...) | 'all' = that colour everywhere; white/black always drop holes
    'plate': 'auto',      # solid plate behind the text: 'auto' removed, 'outline' removed but its contour kept, 'off'
    'mode': 'white',      # 'white' | 'black' | 'keep' (original colours)
    'opacity': 100,       # logo opacity, %
    'margin': 6,          # free border after trimming, px (0 = no trim/fit)
    'upscale': 150,       # max enlargement when fitting, %
    'bevel': 0,           # emboss depth, px (0 = off)
    'shadow': 0,          # drop shadow, px (0 = off)
    'shape': 'none',      # tile behind the logo: 'none' | 'rect' | 'rounded' | 'square' | 'circle'
    'radius': 14,         # corner radius for 'rounded'/'square', px
    'fill': '#1e1e1e',    # tile colour
    'fade': 0,            # fade-out to transparency towards the edge of `fade_shape`, px (0 = off)
    'fade_shape': 'circle',  # 'circle' | 'square' | 'rect' — the area that stays, centred on the canvas
    'ai_style': 'flat',   # look the AI redraw asks for (logo_ai.STYLES); ignored by the local pipeline
    'ai_extra': '',       # free text appended to the AI prompt (max 300 chars)
}

AI_STYLES = ('flat', 'outline', 'neon', 'color', 'metal', 'emboss')

PRESETS = {
    'white':      {},
    'white_3d':   {'bevel': 2, 'shadow': 3},
    'white_tile': {'shape': 'rounded', 'margin': 12, 'fill': '#1e1e1e'},
    'clean':      {'mode': 'keep', 'plate': 'off'},
    'white_glow': {'shape': 'rect', 'fill': '#000000', 'margin': 12, 'fade': 30, 'fade_shape': 'circle'},
    'white_outline': {'plate': 'outline'},
}

# RDS country (ECC + first hex digit of the PI code) -> ISO code, for "all logos of a country"
COUNTRIES = {
    'E0': {'1': 'DE', '2': 'DZ', '3': 'AD', '4': 'IL', '5': 'IT', '6': 'BE', '7': 'RU', '8': 'PS', '9': 'AL',
           'A': 'AT', 'B': 'HU', 'C': 'MT', 'D': 'DE', 'F': 'EG'},
    'E1': {'1': 'GR', '2': 'CY', '3': 'SM', '4': 'CH', '5': 'JO', '6': 'FI', '7': 'LU', '8': 'BG', '9': 'DK',
           'A': 'GI', 'B': 'IQ', 'C': 'GB', 'D': 'LY', 'E': 'RO', 'F': 'FR'},
    'E2': {'1': 'MA', '2': 'CZ', '3': 'PL', '4': 'VA', '5': 'SK', '6': 'SY', '7': 'TN', '9': 'LI', 'A': 'IS',
           'B': 'MC', 'C': 'LT', 'D': 'RS', 'E': 'ES', 'F': 'NO'},
    'E3': {'1': 'ME', '2': 'IE', '3': 'TR', '4': 'MK', '8': 'NL', '9': 'LV', 'A': 'LB', 'C': 'HR', 'E': 'SE', 'F': 'BY'},
    'F0': {'1': 'AU', '2': 'AU', '3': 'AU', '4': 'AU', '5': 'AU', '6': 'AU', '7': 'AU', '8': 'AU'},
    'E4': {'1': 'MD', '2': 'EE', '6': 'UA', '7': 'XK', '8': 'PT', '9': 'SI', 'A': 'AM', 'C': 'GE', 'F': 'BA'},
}


def country_name(code):
    """ISO code for "E2-3" (ECC-first PI digit), or ''."""
    ecc, _, d = code.partition('-')
    return COUNTRIES.get(ecc, {}).get(d, '')


_CHOICES = {'bg_pick': ('auto', 'white', 'black', 'custom'), 'bg_scope': ('edge', 'holes', 'all'),
            'plate': ('auto', 'outline', 'off'), 'mode': ('white', 'black', 'keep'),
            'shape': ('none', 'rect', 'rounded', 'square', 'circle'), 'fade_shape': ('circle', 'square', 'rect'),
            'ai_style': AI_STYLES}
_RANGES = {'tolerance': (0, 255), 'opacity': (5, 100), 'margin': (0, 40), 'upscale': (100, 400),
           'bevel': (0, 8), 'shadow': (0, 10), 'radius': (0, 60), 'fade': (0, 60)}


def normalize(params):
    """DEFAULTS overlaid with the valid entries of `params` (clamped)."""
    p = dict(DEFAULTS)
    for k, v in (params or {}).items():
        if k == 'remove_bg':
            p[k] = bool(v)
        elif k in _CHOICES and v in _CHOICES[k]:
            p[k] = v
        elif k in _RANGES:
            try:
                lo, hi = _RANGES[k]
                p[k] = max(lo, min(hi, int(v)))
            except (TypeError, ValueError):
                pass
        elif k == 'ai_extra' and isinstance(v, str):
            p[k] = ' '.join(v.split())[:300]
        elif k in ('fill', 'bg_custom') and isinstance(v, str) and len(v) == 7 and v[0] == '#':
            try:
                int(v[1:], 16); p[k] = v.lower()
            except ValueError:
                pass
    return p


def _ramp(lo, soft=24):
    """Lookup: 0 up to `lo`, then linear to 255 over `soft` levels."""
    return [0 if v <= lo else 255 if v >= lo + soft else (v - lo) * 255 // soft for v in range(256)]


def _diff(rgb, color):
    """Max channel |difference| of every pixel to `color` (mode L)."""
    r, g, b = ImageChops.difference(rgb, Image.new('RGB', rgb.size, color)).split()
    return ImageChops.lighter(ImageChops.lighter(r, g), b)


def _dominant(rgb, alpha, sample=None, bits=5):
    """(colour, pixel count) of the most common colour (`bits`-bit buckets) among opaque pixels."""
    w, h = rgb.size
    px, al = rgb.load(), alpha.load()
    pts = sample or ((x, y) for y in range(h) for x in range(w))
    buckets, sh = {}, 8 - bits
    for x, y in pts:
        if al[x, y] > 200:
            c = px[x, y]
            k = (c[0] >> sh, c[1] >> sh, c[2] >> sh)
            e = buckets.get(k)
            if e: e[0] += 1; e[1] += c[0]; e[2] += c[1]; e[3] += c[2]
            else: buckets[k] = [1, c[0], c[1], c[2]]
    if not buckets:
        return None, 0
    n, r, g, b = max(buckets.values())
    return (r // n, g // n, b // n), n


def _bbox_count(mask):
    """(bbox, pixel count) of the non-zero pixels of a binary L mask."""
    return mask.getbbox(), mask.histogram()[255]


def _flood(rgb, alpha, seeds, col, tol):
    """Key out the `col`-coloured region connected to `seeds` (soft edge).

    Returns (alpha, counters): counters = alpha with the same colour removed
    where it is enclosed too (letter counters), for the caller to choose.
    """
    d = _diff(rgb, col)
    near = ImageChops.multiply(d.point(lambda v: 255 if v <= tol else 0),
                               alpha.point(lambda v: 255 if v > 128 else 0))
    m = near.copy()
    mp = m.load()
    for x, y in seeds:
        if mp[x, y] == 255:
            ImageDraw.floodfill(m, (x, y), 128)
    soft = d.point(_ramp(tol))
    full = Image.new('L', rgb.size, 255)

    def cut(region):
        # pixels next to the region fade with their distance to its colour
        keep = ImageChops.composite(soft, full, region.filter(ImageFilter.MaxFilter(3)))
        return ImageChops.multiply(ImageChops.multiply(alpha, keep), ImageChops.invert(region))
    conn = m.point(lambda v: 255 if v == 128 else 0)
    return cut(conn), cut(near)


def _remove_bg(rgb, alpha, tol, color=None, scope='edge'):
    """(alpha, counters) without the background colour, or None.

    `color` None = the dominant border colour (skipped when the logo is already on a
    transparent background); `scope` 'edge' removes only the area connected to the
    canvas edge, 'all' that colour everywhere (letter holes and gaps too).
    """
    w, h = rgb.size
    border = [(x, y) for x in range(w) for y in (0, h - 1)] + [(x, y) for y in range(h) for x in (0, w - 1)]
    if color is None:
        al = alpha.load()
        if sum(1 for x, y in border if al[x, y] < 128) > len(border) / 2:
            return None                       # already on a transparent background
        color, _ = _dominant(rgb, alpha, border)
    conn, counters = _flood(rgb, alpha, border, color, tol)
    return (counters, counters) if scope == 'all' else (conn, counters)


def _drop_holes(alpha, counters, frac=0.025):
    """`alpha` without the letter holes: enclosed bits of the removed colour (gone in
    `counters`) that are small (<= `frac` of the canvas) and lie close to the outside
    (a ring a few px around them reaches transparency). A badge's white inside or text
    in that colour on a plate is bigger or farther from the outside and stays."""
    w, h = alpha.size
    k = max(3, round(min(w, h) * 0.04))
    enc = ImageChops.subtract(alpha.point(lambda v: 255 if v > 128 else 0), counters.point(lambda v: 255 if v > 128 else 0))
    outside = alpha.point(lambda v: 255 if v < 64 else 0)
    holes = Image.new('L', alpha.size, 0)
    m = enc.copy()
    mp = m.load()
    for y in range(h):
        for x in range(w):
            if mp[x, y] != 255:
                continue
            ImageDraw.floodfill(m, (x, y), 100)
            comp = m.point(lambda v: 255 if v == 100 else 0)
            ImageDraw.floodfill(m, (x, y), 50)          # done
            if comp.histogram()[255] > frac * w * h:
                continue
            bx = comp.getbbox()
            area = (max(0, bx[0] - k), max(0, bx[1] - k), min(w, bx[2] + k), min(h, bx[3] + k))
            c = comp.crop(area)                         # the ring only around this bit: fast
            ring = ImageChops.subtract(c.filter(ImageFilter.MaxFilter(2 * k + 1)), c)
            rn = ring.histogram()[255]
            if rn and ImageChops.multiply(ring, outside.crop(area)).histogram()[255] >= 0.2 * rn:
                holes = ImageChops.lighter(holes, comp)
    if not holes.getbbox():
        return alpha
    # the counters' soft edge, only around the holes
    return Image.composite(ImageChops.darker(alpha, counters), alpha, holes.filter(ImageFilter.MaxFilter(3)))


def _remove_plate(rgb, alpha, tol):
    """(alpha, counters) without a solid plate behind the text, or None when there is none.

    A plate = the opaque area is a solid shape (>= 75% of its bounding box: a box,
    rounded box or circle; text alone is far sparser), its outline is mostly one
    colour, and something else (>= 2%) is left on it once that colour is removed.
    """
    opaque = alpha.point(lambda v: 255 if v > 200 else 0)
    box, n = _bbox_count(opaque)
    if not box:
        return None
    area = (box[2] - box[0]) * (box[3] - box[1])
    if n < 0.75 * area:
        return None
    ring = ImageChops.subtract(opaque, opaque.filter(ImageFilter.MinFilter(5)))
    rp = ring.load()
    pts = [(x, y) for y in range(box[1], box[3]) for x in range(box[0], box[2]) if rp[x, y]]
    col, cnt = _dominant(rgb, alpha, pts, bits=3)   # coarse: plates often carry a gradient
    if col is None or cnt < 0.4 * len(pts):
        return None
    res = _flood(rgb, alpha, pts, col, tol)
    if res[0].point(lambda v: 255 if v > 200 else 0).histogram()[255] < 0.02 * area:
        return None                           # a solid shape with nothing on it: keep it
    return res + (col,)


def warnings(im):
    """Hints for the preview: 'blob' = a solid shape with almost no inner detail left
    (edge pixels < 15% of the opaque ones; text and outlines score far higher), 'empty'."""
    a = im.getchannel('A').point(lambda v: 255 if v > 128 else 0)
    n = a.histogram()[255]
    if n < 0.01 * im.size[0] * im.size[1]:
        return ['empty']
    edge = ImageChops.subtract(a, a.filter(ImageFilter.MinFilter(3))).histogram()[255]
    return ['blob'] if edge < 0.15 * n else []


def _fit(im, margin, upscale):
    """Trim to content and centre it on the same canvas with `margin` px free."""
    W, H = im.size
    box = im.getchannel('A').point(lambda v: 255 if v > 16 else 0).getbbox()
    if not box:
        return im
    c = im.crop(box)
    w, h = c.size
    s = min((W - 2 * margin) / w, (H - 2 * margin) / h, upscale / 100)
    if abs(s - 1) > 0.01:
        c = c.resize((max(1, round(w * s)), max(1, round(h * s))), Image.LANCZOS)
    out = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    out.paste(c, ((W - c.size[0]) // 2, (H - c.size[1]) // 2))
    return out


def _bevel(im, depth):
    """Emboss lit from the top-left: darken the lower-right slopes, lighten the upper-left."""
    a = im.getchannel('A')
    hmap = a.filter(ImageFilter.GaussianBlur(depth))
    k = max(1, depth // 2)
    shade = ImageChops.subtract(ImageChops.offset(hmap, -k, -k), ImageChops.offset(hmap, k, k), 1, 128)
    shade = shade.point(lambda v: max(0, min(255, 128 + (v - 128) * 2)))
    dark = shade.point(lambda v: 255 - max(0, 128 - v)).convert('RGB')        # down to 50%
    light = shade.point(lambda v: max(0, (v - 128) * 2)).convert('RGB')
    rgb = ImageChops.screen(ImageChops.multiply(im.convert('RGB'), dark), light)
    rgb.putalpha(a)
    return rgb


def _shadow(im, size):
    a = im.getchannel('A')
    off = max(1, size // 2)
    sh = ImageChops.offset(a, off, off).filter(ImageFilter.GaussianBlur(max(1, size / 2)))
    sh = sh.point(lambda v: v * 6 // 10)
    layer = Image.new('RGBA', im.size, (0, 0, 0, 0))
    layer.putalpha(sh)
    return Image.alpha_composite(layer, im)


def _tile(size, shape, radius, fill):
    W, H = size
    t = Image.new('RGBA', size, (0, 0, 0, 0))
    d = ImageDraw.Draw(t)
    rgb = tuple(int(fill[i:i + 2], 16) for i in (1, 3, 5))
    if shape in ('square', 'circle'):
        s = min(W, H) - 2
        box = ((W - s) // 2, (H - s) // 2, (W - s) // 2 + s - 1, (H - s) // 2 + s - 1)
    else:
        box = (1, 1, W - 2, H - 2)
    if shape == 'circle':
        d.ellipse(box, fill=rgb)
    elif shape == 'rect':
        d.rectangle(box, fill=rgb)
    else:
        d.rounded_rectangle(box, radius=radius, fill=rgb)
    return t, box


def apply(im, params, warn=None):
    """Styled copy of `im` (any mode) as RGBA on the same canvas.

    `warn` (a list) receives warnings() of the logo layer (before any tile).
    """
    p = normalize(params)
    im = im.convert('RGBA')
    rgb, alpha = im.convert('RGB'), im.getchannel('A')
    if p['remove_bg']:
        # background, then up to 3 nested plates (white canvas -> dark box -> ...); only
        # connected areas go, so text in the plate's colour inside another plate survives.
        # The innermost colour removed is the text's surroundings: its enclosed bits are
        # letter counters and go too (unless keeping colours, where they are harmless).
        pick = {'white': (255, 255, 255), 'black': (0, 0, 0),
                'custom': tuple(int(p['bg_custom'][i:i + 2], 16) for i in (1, 3, 5))}.get(p['bg_pick'])
        last = _remove_bg(rgb, alpha, p['tolerance'], pick, p['bg_scope'])
        bg_none = last is None                # was already on a transparent background
        if last:
            alpha = last[0]
        contour = None
        for _ in range(3):
            r = _remove_plate(rgb, alpha, p['tolerance'])
            if not r:
                break
            box = alpha.point(lambda v: 255 if v > 128 else 0).getbbox()
            sides = sum((box[0] <= 2, box[1] <= 2, box[2] >= im.size[0] - 2, box[3] >= im.size[1] - 2)) if box else 0
            # plate 'off' still drops the background card: a plate filling the image (3+ sides:
            # the real background inside a thin frame line) or, on a transparent logo, the
            # outermost plate when it is white/light (a card); never a badge or strip under the text
            card = sides >= 3 or (bg_none and last is None and min(r[2]) >= 215)
            if p['plate'] == 'off' and not card:
                break
            # contour candidate: the plate just removed, unless it reaches 2+ canvas sides
            # (then it is a canvas/background, not a badge); the innermost one wins
            if sides < 2:
                contour = alpha
            alpha, last = r[0], r[:2]
        if last and p['mode'] != 'keep':
            alpha = ImageChops.darker(alpha, last[1])
        elif last and p['bg_scope'] == 'holes':
            alpha = _drop_holes(alpha, last[1])
        if contour is not None and p['plate'] == 'outline':
            # that plate's contour, ~2% of the canvas thick, with its soft outer edge
            w = max(2, round(min(im.size) * 0.02))
            alpha = ImageChops.lighter(alpha, ImageChops.subtract(contour, contour.filter(ImageFilter.MinFilter(2 * w + 1))))
    alpha = alpha.point(lambda v: 0 if v < 20 else v)
    if p['mode'] == 'keep':
        out = rgb.copy()
    else:
        out = Image.new('RGB', im.size, (255, 255, 255) if p['mode'] == 'white' else (0, 0, 0))
    out.putalpha(alpha.point(lambda v: v * p['opacity'] // 100))
    if p['shape'] != 'none':
        tile, box = _tile(im.size, p['shape'], p['radius'], p['fill'])
        inner = (box[2] - box[0] + 1, box[3] - box[1] + 1)
        logo = _fit(_crop_to(out, inner), p['margin'], p['upscale'])
    else:
        logo = _fit(out, p['margin'], p['upscale']) if p['margin'] else out
    if warn is not None and p['mode'] != 'keep':
        warn.extend(warnings(logo))
    if p['bevel']:
        logo = _bevel(logo, p['bevel'])
    if p['shadow']:
        logo = _shadow(logo, p['shadow'])
    if p['shape'] == 'none':
        return _fade(logo, p['fade'], p['fade_shape'])
    t = tile.copy()
    t.alpha_composite(logo, (box[0], box[1]))
    if p['shape'] in ('rounded', 'square', 'circle'):
        mask, _ = _tile(im.size, p['shape'], p['radius'], '#ffffff')
        t.putalpha(ImageChops.multiply(t.getchannel('A'), mask.getchannel('A')))
    return _fade(t, p['fade'], p['fade_shape'])


def _fade(im, width, shape):
    """`im` fading to transparent over the last `width` px inside a centred circle /
    square / the whole canvas (rect): concentric shapes, each a step more opaque."""
    if not width:
        return im
    W, H = im.size
    if shape == 'rect':
        box = (0, 0, W - 1, H - 1)
    else:
        s = min(W, H)
        box = ((W - s) // 2, (H - s) // 2, (W - s) // 2 + s - 1, (H - s) // 2 + s - 1)
    width = min(width, (box[2] - box[0]) // 2, (box[3] - box[1]) // 2)
    mask = Image.new('L', im.size, 0)
    d = ImageDraw.Draw(mask)
    draw = d.ellipse if shape == 'circle' else d.rectangle
    for i in range(width + 1):
        b = (box[0] + i, box[1] + i, box[2] - i, box[3] - i)
        if b[2] < b[0] or b[3] < b[1]:
            break
        draw(b, fill=255 * i // width)
    out = im.copy()
    out.putalpha(ImageChops.multiply(im.getchannel('A'), mask))
    return out


def _crop_to(im, size):
    """The content of `im` on a transparent canvas of `size` (for fitting into a tile)."""
    box = im.getchannel('A').getbbox()
    c = Image.new('RGBA', size, (0, 0, 0, 0))
    if box:
        part = im.crop(box)
        part.thumbnail(size, Image.LANCZOS)
        c.paste(part, ((size[0] - part.size[0]) // 2, (size[1] - part.size[1]) // 2))
    return c


def render(src, params, warn=None):
    """PNG bytes of the styled file `src` (`warn` as in apply)."""
    with Image.open(src) as im:
        out = apply(im, params, warn)
    buf = io.BytesIO()
    out.save(buf, 'PNG', optimize=True)
    return buf.getvalue()


class Store:
    """Styled files + metadata under one directory (paths.styled_dir)."""

    def __init__(self, root):
        self.root = root
        self.meta_path = os.path.join(root, '_meta.json')

    def load(self):
        try:
            with open(self.meta_path, encoding='utf-8') as f:
                m = json.load(f)
        except (OSError, ValueError):
            m = {}
        m.setdefault('files', {}); m.setdefault('presets', {})
        return m

    def save(self, m):
        import config
        config.atomic_write(self.meta_path, json.dumps(m, indent=1, ensure_ascii=False, sort_keys=True))

    def path(self, key):
        ecc, fn = key.upper().split('/')
        return os.path.join(self.root, ecc, fn)

    def exists(self, key):
        return os.path.isfile(self.path(key))

    def write(self, key, png, meta, base, preset, params):
        p = self.path(key)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        import config
        config.atomic_write(p, png)
        meta['files'][key.upper()] = {'base': base, 'preset': preset, 'params': normalize(params),
                                      'time': time.strftime('%Y-%m-%d %H:%M:%S')}

    def presets(self):
        out = {k: normalize(v) for k, v in PRESETS.items()}
        out.update({k: normalize(v) for k, v in self.load()['presets'].items()})
        return out


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='Apply a logo style to PNG files (preview/testing).')
    ap.add_argument('files', nargs='+')
    ap.add_argument('--preset', default='white', choices=sorted(PRESETS))
    ap.add_argument('--set', action='append', default=[], metavar='KEY=VALUE', help='override a parameter')
    ap.add_argument('--out', required=True, help='output directory')
    a = ap.parse_args()
    prm = dict(PRESETS[a.preset])
    for kv in a.set:
        k, _, v = kv.partition('=')
        prm[k] = {'true': True, 'false': False}.get(v.lower(), v)
    os.makedirs(a.out, exist_ok=True)
    for fp in a.files:
        with open(os.path.join(a.out, os.path.basename(fp)), 'wb') as f:
            f.write(render(fp, prm))
    print(f"{len(a.files)} files -> {a.out}  {normalize(prm)}")
