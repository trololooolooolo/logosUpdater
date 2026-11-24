# Vendored third-party web assets

Bundled locally so the app works fully offline (no CDN requests).

| Library | Version | Files | Source | License |
|---|---|---|---|---|
| Cropper.js | 1.5.13 | `cropperjs/cropper.min.js`, `cropperjs/cropper.min.css` | https://cdnjs.cloudflare.com/ajax/libs/cropperjs/1.5.13/ (upstream: https://github.com/fengyuanchen/cropperjs) | MIT, see `cropperjs/LICENSE` |

Files are unmodified copies of the CDN builds. To update, download the new
version's `cropper.min.js` / `cropper.min.css`, replace them here and update this table
and the paths in `web/index.html`.
