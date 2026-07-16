"""One-off script: generate all icon size variants from master_alpha.png.

256/128/64/48: plain high-quality LANCZOS downscale -- master_alpha.png has
enough resolution (2048px) that fine petal detail survives fine down to 48px.

32/16: a plain downscale of this particular piece of art turns to mush at
these sizes (the petal field is dozens of overlapping translucent
quadrilaterals; by 32px the fine seams between them just become noise, and
the ghost-thin outer petal edges vanish or dither). Instead:
  - Heavy posterize collapses the many overlapping translucent layers into
    a handful of flat tone bands, which reads as ~8 bold petals rather than
    a gradient blur once shrunk.
  - A darkness-based dilation thickens the wheel's rim/spokes/hub (all
    notably darker than the surrounding petal field) by ~1-2px at working
    resolution, so they do not disappear into anti-aliasing at 16-32px.
  - Alpha hardening pushes the faint ghost-edge alpha values toward 0 or
    255 (steepened curve) so the silhouette holds against arbitrary
    backgrounds instead of fading away at a size where fine translucency
    just reads as noise.

Not part of the app; run once to populate assets/icon/*.png.
"""
import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
from scipy import ndimage

SRC = r"C:\Users\Morne\Projects\Samsara-dev\assets\icon\master_alpha.png"
OUT_DIR = r"C:\Users\Morne\Projects\Samsara-dev\assets\icon"

STANDARD_SIZES = [256, 128, 64, 48]
SIMPLIFIED_SIZES = [32, 16]
WORK_SIZE = 128  # simplification is done at this size, then downscaled


def darken_dilate(rgba, luminance_thresh=95, iterations=1):
    """Grow dark (wheel rim/spoke/hub) regions by `iterations` px so thin
    dark structure survives being shrunk to icon size."""
    arr = np.asarray(rgba).astype(np.float64)
    rgb = arr[..., :3]
    alpha = arr[..., 3]
    luminance = rgb.mean(axis=-1)
    dark_mask = (luminance < luminance_thresh) & (alpha > 40)

    grown_mask = ndimage.binary_dilation(dark_mask, iterations=iterations)
    new_dark = grown_mask & ~dark_mask

    # Representative dark color: median of the original dark pixels (the
    # wheel's own rim/hub tones), not pure black, so growth blends in.
    if dark_mask.any():
        dark_color = np.median(rgb[dark_mask], axis=0)
    else:
        dark_color = np.array([40.0, 20.0, 20.0])

    out = arr.copy()
    out[new_dark, :3] = dark_color
    out[new_dark, 3] = 255.0
    return Image.fromarray(out.astype(np.uint8), mode="RGBA")


def harden_alpha(rgba, gamma=0.55, floor=18):
    """Steepen the alpha curve so faint ghost-edge translucency either
    settles near 0 or climbs toward fully opaque, instead of sitting at a
    middling value that just dithers away at small sizes."""
    arr = np.asarray(rgba).astype(np.float64)
    a = arr[..., 3] / 255.0
    a = np.where(arr[..., 3] < floor, 0.0, a ** gamma)
    out = arr.copy()
    out[..., 3] = np.clip(a * 255.0, 0, 255)
    return Image.fromarray(out.astype(np.uint8), mode="RGBA")


def simplify(img_2048):
    work = img_2048.resize((WORK_SIZE, WORK_SIZE), Image.LANCZOS)

    # Thicken wheel rim/spokes/hub relative to the petal field.
    work = darken_dilate(work, luminance_thresh=95, iterations=1)

    # Flatten the overlapping translucent petal layers into bold bands.
    rgb = work.convert("RGB")
    rgb = ImageOps.posterize(rgb, bits=4)
    rgb = ImageEnhance.Contrast(rgb).enhance(1.35)
    rgb = ImageEnhance.Color(rgb).enhance(1.2)
    r, g, b = rgb.split()
    _, _, _, a = work.split()
    work = Image.merge("RGBA", (r, g, b, a))

    # Harden the outer silhouette (less translucent ghost fade).
    work = harden_alpha(work, gamma=0.55, floor=18)

    # Slight sharpen so the thickened wheel spokes stay crisp through the
    # final downscale rather than softening back out.
    work = work.filter(ImageFilter.UnsharpMask(radius=2, percent=60, threshold=2))
    return work


def main():
    master = Image.open(SRC).convert("RGBA")
    print(f"master: {master.size} {master.mode}")

    for size in STANDARD_SIZES:
        out = master.resize((size, size), Image.LANCZOS)
        path = f"{OUT_DIR}\\samsara_{size}.png"
        out.save(path)
        print(f"saved {path} ({out.size})")

    simplified_master = simplify(master)
    for size in SIMPLIFIED_SIZES:
        out = simplified_master.resize((size, size), Image.LANCZOS)
        path = f"{OUT_DIR}\\samsara_{size}.png"
        out.save(path)
        print(f"saved {path} ({out.size}) [simplified variant]")


if __name__ == "__main__":
    main()
