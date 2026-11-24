#!/usr/bin/env python3
"""AI redraw of station logos for "🖌 Logo style" (stage 2), for files the local
filters cannot turn white (logo_style.warnings() == 'blob').

The model gets the logo and is asked for a look from STYLES (`ai_style`, plus the
user's `ai_extra` text): white-on-black ones are keyed by luminance -> alpha, coloured
ones come on chroma green that is keyed out; the result is scaled onto the source canvas and
run through logo_style.apply() with the chosen parameters, so AI results get
the same margin / emboss / tile as the rest.

Providers (keys: config.yaml `ai.*_api_key` or env GEMINI_API_KEY / KIE_API_KEY):
  gemini — Gemini API, Interactions endpoint (falls back to generateContent)
  kie    — kie.ai: base64 upload -> jobs/createTask -> poll recordInfo -> download
Raw answers are cached per (source bytes, prompt, provider, model), so a new
preview or apply never pays twice.
"""
import base64
import hashlib
import io
import json
import os
import time

import requests
from PIL import Image, ImageChops, ImageStat

import logo_style

PROMPT = (
    "This is a radio station logo. Redraw it as a clean, flat, single-colour logo: every letter, "
    "symbol and shape of the logo in pure white (#FFFFFF) on a solid pure black (#000000) background. "
    "Keep exactly the same text and spelling (including Polish/diacritic letters), the same letter shapes, "
    "proportions and layout. Do not add, remove, translate or restyle anything; no gradients, shadows, "
    "outlines, glow, frames or new background shapes. If the lettering sits on a coloured plate, box or "
    "badge, drop the plate and keep only the lettering and symbols. Holes and gaps inside letters must be "
    "black. Fill the frame with the logo, centred."
)

# plate: 'outline' -> keep the plate's contour instead of dropping it (logo_style param `plate`)
PROMPT_OUTLINE = PROMPT.replace(
    "If the lettering sits on a coloured plate, box or badge, drop the plate and keep only the lettering and symbols.",
    "If the lettering sits on a coloured plate, box, badge or shape, do not fill it: draw only its outer contour as a "
    "thin white outline (about 3% of the logo height) with the lettering and symbols inside it in white.")

# logo_style param `ai_style` -> (look, keying). 'mono': white on black, luminance -> alpha, recoloured by
# `mode`; 'key': own colours on chroma green, the green is keyed out and the colours kept.
_SAME = ("Keep exactly the same text and spelling (including Polish/diacritic letters), the same letter shapes, "
         "proportions and layout; do not add, remove or translate anything and add no frames or extra objects. ")
_PLATE = {
    'auto': "If the lettering sits on a coloured plate, box or badge, drop the plate and keep only the lettering and symbols. ",
    'outline': "If the lettering sits on a plate, box, badge or shape, do not fill it: draw only its outer contour as a "
               "thin outline (about 3% of the logo height) with the lettering and symbols inside it. ",
    'off': "Keep any plate, box or badge behind the lettering as part of the logo. ",
}
_GREEN = ("Put it on a perfectly flat, solid chroma-key green (#00FF00) background with no gradient, texture, "
          "vignette or shadow on the background; do not use that green anywhere in the logo itself. ")
_LOOK = {
    'neon': ('mono', "Redraw it as glowing neon-tube lettering: every letter, symbol and shape as bright white (#FFFFFF) "
                     "neon tubes with a soft white glow, on a solid pure black (#000000) background, no coloured light. "),
    'color': ('key', "Redraw it as a clean, crisp, flat vector-style logo in its original colours: sharp edges, "
                     "no blur, noise or compression artefacts, no gradients or shadows that the original does not have. "),
    'metal': ('key', "Redraw it as a polished silver chrome 3D logo: letters and symbols in silver metal with a subtle "
                     "bevel and soft reflections, neutral grey and white tones only. "),
    'emboss': ('key', "Redraw it as a white embossed 3D logo: letters and symbols in white with a soft bevel and gentle "
                      "grey shading that gives relief, no other colours. "),
}
STYLES = logo_style.AI_STYLES


def kind_of(params):
    """'mono' (white on black, keyed by luminance) or 'key' (colours on green)."""
    return _LOOK.get(logo_style.normalize(params)['ai_style'], ('mono',))[0]


def prompt_for(params):
    """The prompt for logo_style params: `ai_style` look + `plate` handling + `ai_extra`.

    'flat'/'outline' keep the original wording, so earlier cached answers stay valid."""
    p = logo_style.normalize(params)
    st = p['ai_style']
    if st in ('flat', 'outline'):
        text = PROMPT_OUTLINE if st == 'outline' or p['plate'] == 'outline' else PROMPT
    else:
        kind, look = _LOOK[st]
        text = ("This is a radio station logo. " + look + _SAME + _PLATE[p['plate']] +
                (_GREEN if kind == 'key' else "Holes and gaps inside letters must be black. ") +
                "Fill the frame with the logo, centred.")
    if p['ai_extra']:
        text += " Additional instructions: " + p['ai_extra']
    return text


DEFAULTS = {
    'provider': 'gemini',
    'gemini_api_key': '',
    'gemini_model': 'gemini-nano-banana-2.1',
    'kie_api_key': '',
    'kie_model': 'google/nano-banana-edit',
    'price_per_image': {'gemini': 0.034, 'kie': None},   # USD, for the estimate only
    'workers': 3,
    # endpoint overrides (proxy / tests); empty = the official ones below
    'gemini_url': '', 'kie_url': '', 'kie_upload_url': '',
}
GEMINI_URL = 'https://generativelanguage.googleapis.com/v1beta'
KIE_API = 'https://api.kie.ai/api/v1/jobs'
KIE_UPLOAD = 'https://kieai.redpandaai.co/api/file-base64-upload'
TIMEOUT = 120
RESULT_MAX = 20 * 1024 * 1024   # an AI result image is a few hundred kB


def _download_result(url, max_redirects=5):
    """GET an AI result image: public http(s) addresses only - checked again on every redirect hop
    (requests would follow a 302 to 127.0.0.1 or the LAN on its own) - and at most RESULT_MAX bytes."""
    from urllib.parse import urljoin
    import fetcher
    for _ in range(max_redirects + 1):
        if not fetcher.public_http_url(url):
            raise AIError('kie.ai: result URL is not a public address')
        with requests.get(url, timeout=TIMEOUT, stream=True, allow_redirects=False) as img:
            if img.status_code in (301, 302, 303, 307, 308) and img.headers.get('location'):
                url = urljoin(url, img.headers['location'])
                continue
            if img.status_code != 200:
                raise AIError('kie.ai download ' + _err(img))
            data = b''
            for chunk in img.iter_content(65536):
                data += chunk
                if len(data) > RESULT_MAX:
                    raise AIError('kie.ai: result image is too large')
            return data
    raise AIError('kie.ai: too many redirects for the result image')


class AIError(Exception):
    pass


def settings(cfg):
    """ai settings: DEFAULTS <- config `ai:` <- env keys."""
    s = dict(DEFAULTS)
    s.update({k: v for k, v in (cfg.get('ai') or {}).items() if v not in (None, '')})
    s['gemini_api_key'] = os.environ.get('GEMINI_API_KEY') or s['gemini_api_key']
    s['kie_api_key'] = os.environ.get('KIE_API_KEY') or s['kie_api_key']
    return s


def available(s):
    """{provider: configured?} — keys never leave the server."""
    return {'gemini': bool(s['gemini_api_key']), 'kie': bool(s['kie_api_key'])}


def model_of(s, provider):
    return s['gemini_model'] if provider == 'gemini' else s['kie_model']


# ---------- input / output images ----------

def prepare(src):
    """PNG bytes sent to the model: the logo flattened on a contrasting background, 4x up."""
    with Image.open(src) as im:
        im = im.convert('RGBA')
    a = im.getchannel('A')
    if a.getbbox():
        lum = ImageStat.Stat(im.convert('L'), a.point(lambda v: 255 if v > 128 else 0)).mean[0]
    else:
        lum = 0
    bg = Image.new('RGBA', im.size, (0, 0, 0, 255) if lum > 170 else (255, 255, 255, 255))
    bg.alpha_composite(im)
    out = bg.convert('RGB').resize((im.size[0] * 4, im.size[1] * 4), Image.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, 'PNG')
    return buf.getvalue()


def postprocess(raw, size, params):
    """The model's answer as a styled RGBA logo of `size`.

    Returns (image, warnings). 'mono' styles (white on black): luminance becomes alpha
    (soft, below ~16% = gone) and `mode` recolours it. 'key' styles (colours on chroma
    green): the background colour (from the corners) is keyed out by distance, the
    green spill on soft edges removed and the colours kept. Then the content is fitted
    onto the source canvas and logo_style.apply() adds margin/emboss/shadow/tile/fade
    from `params` (background and plate removal off: the AI already did that).
    """
    with Image.open(io.BytesIO(raw)) as im:
        im = im.convert('RGB')
    if kind_of(params) == 'key':
        logo, alpha = _key_out(im)
    else:
        lum = im.convert('L')
        # the model was asked for black; if the corners are light it answered inverted
        corners = [lum.getpixel(xy) for xy in ((0, 0), (lum.width - 1, 0), (0, lum.height - 1), (lum.width - 1, lum.height - 1))]
        if sum(corners) / 4 > 128:
            lum = ImageChops.invert(lum)
        alpha = lum.point(lambda v: 0 if v < 40 else 255 if v > 200 else (v - 40) * 255 // 160)
        logo = Image.new('RGBA', im.size, (255, 255, 255, 0))
        logo.putalpha(alpha)
    box = alpha.point(lambda v: 255 if v > 16 else 0).getbbox()
    canvas = Image.new('RGBA', size, (0, 0, 0, 0))
    if box:
        part = logo.crop(box)
        part.thumbnail(size, Image.LANCZOS)
        canvas.paste(part, ((size[0] - part.size[0]) // 2, (size[1] - part.size[1]) // 2))
    p = dict(logo_style.normalize(params), remove_bg=False, plate='off')
    if kind_of(params) == 'key':
        p['mode'] = 'keep'                # the look (colours, chrome shading) is the AI's
    elif p['mode'] == 'keep':
        p['mode'] = 'white'               # the AI version has no colours to keep
    warn = []
    return logo_style.apply(canvas, p, warn), warn


def _key_out(im):
    """(RGBA logo, alpha) of an answer on a flat background: the corner colour keyed out."""
    w, h, n = im.width, im.height, max(2, min(im.size) // 40)
    patches = [im.crop((x, y, x + n, y + n)) for x, y in ((0, 0), (w - n, 0), (0, h - n), (w - n, h - n))]
    bg = tuple(sorted(c)[len(c) // 2] for c in zip(*[px for pt in patches for px in pt.getdata()]))
    alpha = logo_style._diff(im, bg).point(logo_style._ramp(40, 60))
    if bg[1] > max(bg[0], bg[2]) + 60:
        # green spill: on soft edges the green can be no stronger than red/blue
        r, g, b = im.split()
        spill = Image.merge('RGB', (r, ImageChops.darker(g, ImageChops.lighter(r, b)), b))
        im = Image.composite(im, spill, alpha.point(lambda v: 255 if v > 245 else 0))
    out = im.convert('RGBA')
    out.putalpha(alpha)
    return out, alpha


def to_png(im):
    buf = io.BytesIO()
    im.save(buf, 'PNG', optimize=True)
    return buf.getvalue()


# ---------- providers ----------

def _find_image(obj):
    """First base64 image in a JSON answer (Interactions `{"type":"image","data"}` or
    generateContent `inlineData`/`inline_data`)."""
    stack = [obj]
    while stack:
        o = stack.pop(0)
        if isinstance(o, dict):
            inline = o.get('inlineData') or o.get('inline_data')
            if isinstance(inline, dict) and inline.get('data'):
                return inline['data']
            if o.get('type') == 'image' and isinstance(o.get('data'), str):
                return o['data']
            stack.extend(o.values())
        elif isinstance(o, list):
            stack.extend(o)
    return None


def _err(r):
    try:
        j = r.json()
        msg = (j.get('error') or {}).get('message') if isinstance(j.get('error'), dict) else j.get('error') or j.get('msg')
    except ValueError:
        msg = r.text[:200]
    return f"HTTP {r.status_code}: {msg or r.reason}"


def call_gemini(s, png, prompt):
    key, model = s['gemini_api_key'], s['gemini_model']
    h = {'x-goog-api-key': key, 'Content-Type': 'application/json'}
    b64 = base64.b64encode(png).decode()
    base = s.get('gemini_url') or GEMINI_URL
    r = requests.post(f'{base}/interactions', headers=h, timeout=TIMEOUT, json={
        'model': model,
        'input': [{'type': 'text', 'text': prompt},
                  {'type': 'image', 'mime_type': 'image/png', 'data': b64}],
        'response_format': {'type': 'image', 'aspect_ratio': '4:3', 'image_size': '1K'}})
    if r.status_code == 404:
        # older API surface: generateContent
        r = requests.post(f'{base}/models/{model}:generateContent', headers=h, timeout=TIMEOUT, json={
            'contents': [{'parts': [{'text': prompt}, {'inline_data': {'mime_type': 'image/png', 'data': b64}}]}],
            'generationConfig': {'responseModalities': ['IMAGE'], 'imageConfig': {'aspectRatio': '4:3'}}})
    if r.status_code != 200:
        raise AIError('Gemini ' + _err(r))
    data = _find_image(r.json())
    if not data:
        raise AIError('Gemini: no image in the answer (refused or text only)')
    return base64.b64decode(data)


def call_kie(s, png, prompt, poll=3, max_wait=300):
    h = {'Authorization': f"Bearer {s['kie_api_key']}", 'Content-Type': 'application/json'}
    api, upload = s.get('kie_url') or KIE_API, s.get('kie_upload_url') or KIE_UPLOAD
    r = requests.post(upload, headers=h, timeout=TIMEOUT, json={
        'base64Data': 'data:image/png;base64,' + base64.b64encode(png).decode(),
        'uploadPath': 'images/logos-updater', 'fileName': f'logo_{hashlib.md5(png).hexdigest()[:10]}.png'})
    j = r.json() if r.headers.get('content-type', '').startswith('application/json') else {}
    url = (j.get('data') or {}).get('downloadUrl')
    if r.status_code != 200 or not url:
        raise AIError('kie.ai upload ' + _err(r))
    r = requests.post(f'{api}/createTask', headers=h, timeout=TIMEOUT, json={
        'model': s['kie_model'],
        'input': {'prompt': prompt, 'image_urls': [url], 'output_format': 'png', 'aspect_ratio': '4:3'}})
    j = r.json() if r.status_code == 200 else {}
    task = (j.get('data') or {}).get('taskId')
    if j.get('code') != 200 or not task:
        raise AIError(f"kie.ai createTask: {j.get('msg') or _err(r)}")
    t0 = time.time()
    while True:
        time.sleep(poll)
        r = requests.get(f'{api}/recordInfo', headers=h, params={'taskId': task}, timeout=TIMEOUT)
        d = (r.json().get('data') or {}) if r.status_code == 200 else {}
        st = d.get('state')
        if st == 'success':
            urls = json.loads(d.get('resultJson') or '{}').get('resultUrls') or []
            if not urls:
                raise AIError('kie.ai: task finished without an image')
            return _download_result(urls[0])
        if st == 'fail':
            raise AIError(f"kie.ai: {d.get('failMsg') or d.get('failCode') or 'failed'}")
        if time.time() - t0 > max_wait:
            raise AIError('kie.ai: timeout waiting for the task')
        poll = min(poll + 1, 8)


# ---------- cache ----------

class Cache:
    """Raw model answers under <styled_dir>/_ai/<ECC>/<NAME>.<hash>.png."""

    def __init__(self, styled_dir):
        self.root = os.path.join(styled_dir, '_ai')

    @staticmethod
    def tag(src_png, prompt, provider, model):
        return hashlib.md5(src_png + f'|{prompt}|{provider}|{model}'.encode()).hexdigest()[:12]

    def path(self, key, tag):
        ecc, fn = key.upper().split('/')
        return os.path.join(self.root, ecc, f'{os.path.splitext(fn)[0]}.{tag}.png')

    def get(self, key, tag):
        try:
            with open(self.path(key, tag), 'rb') as f:
                return f.read()
        except OSError:
            return None

    def put(self, key, tag, raw):
        import config
        config.atomic_write(self.path(key, tag), raw)


def redraw(s, provider, key, png, cache, prompt=PROMPT, allow_call=True):
    """(raw answer bytes, from_cache) for one logo (`png` = prepare(source)); raw is None
    when not cached and `allow_call` is False. Raises AIError on provider errors."""
    if provider not in ('gemini', 'kie'):
        raise AIError(f'unknown provider {provider}')
    if not available(s)[provider]:
        raise AIError(f'no API key for {provider} (config.yaml ai.{provider}_api_key or env)')
    tag = Cache.tag(png, prompt, provider, model_of(s, provider))
    raw = cache.get(key, tag)
    if raw is not None:
        return raw, True
    if not allow_call:
        return None, False
    raw = (call_gemini if provider == 'gemini' else call_kie)(s, png, prompt)
    Image.open(io.BytesIO(raw)).verify()          # an image, not an error page
    cache.put(key, tag, raw)
    return raw, False
