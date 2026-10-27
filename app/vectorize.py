"""Turn raster icons (PNG, JPG, WebP, GIF, BMP) into one-color SVGs so the library stays all-vector.
The shape is found from transparency (or, on an opaque image, from its difference to the border color),
then traced with vtracer, a self-contained pip wheel (MIT)."""
import io
import re

import vtracer
from PIL import Image, ImageChops, ImageStat, UnidentifiedImageError

RASTER_FORMATS = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp", "GIF": "gif", "BMP": "bmp"}
TRACE_PX = 512          # shapes are traced at this size (small sources are upscaled first)
MIN_SOURCE_PX = 128     # below this, warn that the trace may look rough
MAX_PIXELS = 40_000_000
MAX_TRACED_CHARS = 400_000


def is_svg(data: bytes) -> bool:
    return data[:512].lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<")


def _shape_strength(im: Image.Image) -> Image.Image:
    """Grayscale image: 255 = icon, 0 = background."""
    alpha = im.getchannel("A")
    if alpha.getextrema()[0] < 250:  # transparent background: the icon is whatever is opaque
        return alpha
    rgb = im.convert("RGB")
    w, h = rgb.size
    border = [rgb.getpixel((x, y)) for x in range(w) for y in (0, h - 1)]
    border += [rgb.getpixel((x, y)) for y in range(h) for x in (0, w - 1)]
    bg = tuple(sorted(c[i] for c in border)[len(border) // 2] for i in range(3))  # median border color
    r, g, b = ImageChops.difference(rgb, Image.new("RGB", rgb.size, bg)).split()
    return ImageChops.lighter(ImageChops.lighter(r, g), b).point(lambda v: min(255, v * 3))


def raster_to_svg(data: bytes) -> tuple[bytes, str, list[str]]:
    """Returns (svg, source format, warnings). Raises ValueError with a message for the designer."""
    if not data:
        raise ValueError("the file is empty")
    if data[:5] in (b"%PDF-", b"%!PS-"):
        raise ValueError("PDF, AI and EPS files aren't supported: export the icon as SVG "
                         "(Illustrator: File > Export > Export As > SVG) or as a PNG")
    try:
        im = Image.open(io.BytesIO(data))
    except (UnidentifiedImageError, Image.DecompressionBombError):
        raise ValueError("unsupported file type: upload an SVG, or a PNG, JPG, WebP, GIF or BMP image")
    fmt = im.format
    if fmt not in RASTER_FORMATS:
        raise ValueError(f"{fmt} images aren't supported: upload an SVG, or a PNG, JPG, WebP, GIF or BMP image")
    if im.width * im.height > MAX_PIXELS:
        raise ValueError("the image is far too large for an icon; export it at 512 to 1024 px")
    im.seek(0)  # first frame of an animated GIF
    im = im.convert("RGBA")

    warnings = []
    if max(im.size) < MIN_SOURCE_PX:
        warnings.append(f"the source is only {im.width}x{im.height} px, so curves may look rough; "
                        "a 512 px or larger image gives a cleaner trace")
    strength = _shape_strength(im)
    solid = strength.point(lambda v: 255 if v >= 128 else 0)
    bbox = solid.getbbox()
    if not bbox:
        raise ValueError("no icon shape found: the image looks blank, or its background isn't one plain color")
    if ImageStat.Stat(solid.crop(bbox)).mean[0] > 0.6 * 255:
        warnings.append("most of the image is solid; if the icon sits on a tile or badge, upload a version without it")
    if max(ImageStat.Stat(im.convert("RGB"), mask=strength.point(lambda v: 255 if v >= 230 else 0)).stddev) > 40:
        warnings.append("the source has more than one color; it was traced as a single color")

    pad = 2
    strength = strength.crop((max(0, bbox[0] - pad), max(0, bbox[1] - pad),
                              min(im.width, bbox[2] + pad), min(im.height, bbox[3] + pad)))
    scale = TRACE_PX / max(strength.size)
    strength = strength.resize((max(1, round(strength.width * scale)), max(1, round(strength.height * scale))),
                               Image.LANCZOS)
    black_on_white = strength.point(lambda v: 0 if v >= 128 else 255).convert("RGB")
    png = io.BytesIO()
    black_on_white.save(png, "PNG")
    svg = vtracer.convert_raw_image_to_svg(png.getvalue(), img_format="png", colormode="binary", mode="spline",
                                           filter_speckle=4, corner_threshold=60, length_threshold=4.0,
                                           path_precision=3)
    if len(svg) > MAX_TRACED_CHARS:
        raise ValueError("the traced result is far too detailed for an icon (a photo or illustration?)")

    w, h = black_on_white.size  # fixed size -> viewBox, so the icon scales with its frame
    svg = re.sub(r"<\?xml[^>]*\?>\s*|<!--.*?-->\s*", "", svg, flags=re.S)
    svg = re.sub(r"<svg\b[^>]*>", f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}">', svg, count=1)
    return svg.strip().encode("utf-8") + b"\n", fmt, warnings
