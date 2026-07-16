"""One-off script: remove master.png's flat crimson background to
transparency while preserving translucent (ghost-thin) petal edges and the
dark wheel hub.

Approach (see task report for rationale):
  1. Sample a robust background reference color from pixels far from the
     canvas center (radius > 1300px on the 2048px canvas), safely outside
     the flower.
  2. Compute a per-pixel color distance from that reference.
  3. Threshold that distance map to find "background-like" pixels, then
     run connected-component labeling and keep ONLY the components that
     touch the image border as "the background region" -- this is the
     key step that protects interior petal pixels that happen to land
     close to the background color (translucent overlaps) from being
     eaten, since they are enclosed by non-background-like pixels and
     never connect to a border seed.
  4. Within the connected background region, alpha is a smooth ramp based
     on distance (not a hard cut), so the ghost-thin petal edges keep
     their partial translucency instead of a jagged cutoff.
  5. Outside that region (true interior, including the dark wheel hub,
     which is far from the background color anyway and never enters the
     background-like mask to begin with) stays fully opaque.

Not part of the app; run once to produce assets/icon/master_alpha.png.
"""
import numpy as np
from PIL import Image
from scipy import ndimage

SRC = r"C:\Users\Morne\Documents\Claude\icon\master.png"
OUT = r"C:\Users\Morne\Projects\Samsara-dev\assets\icon\master_alpha.png"

CONNECT_THRESH = 30.0   # distance below this = "background-like" for flood connectivity
ALPHA_LOW = 6.0         # distance <= this within the connected region -> alpha 0
ALPHA_HIGH = 26.0       # distance >= this within the connected region -> alpha 255


def main():
    img = Image.open(SRC).convert("RGB")
    arr = np.asarray(img).astype(np.float64)  # (H, W, 3)
    h, w, _ = arr.shape
    cy, cx = h / 2, w / 2

    yy, xx = np.mgrid[0:h, 0:w]
    radius = np.hypot(xx - cx, yy - cy)
    pure_bg_mask = radius > 1300
    bg_ref = arr[pure_bg_mask].reshape(-1, 3).mean(axis=0)
    print(f"background reference (r>1300 avg): {bg_ref}")

    dist = np.sqrt(((arr - bg_ref) ** 2).sum(axis=-1))
    print(f"dist stats: min={dist.min():.1f} max={dist.max():.1f} "
          f"mean={dist.mean():.1f}")

    connect_mask = dist < CONNECT_THRESH

    structure = np.ones((3, 3), dtype=int)  # 8-connectivity
    labeled, num = ndimage.label(connect_mask, structure=structure)
    print(f"connected components in background-like mask: {num}")

    border_labels = set(labeled[0, :].tolist()) | set(labeled[-1, :].tolist())
    border_labels |= set(labeled[:, 0].tolist()) | set(labeled[:, -1].tolist())
    border_labels.discard(0)
    print(f"labels touching border: {sorted(border_labels)}")

    bg_region = np.isin(labeled, list(border_labels))
    print(f"background region: {bg_region.sum()} px "
          f"({100*bg_region.sum()/bg_region.size:.1f}% of image)")

    alpha = np.full((h, w), 255.0)
    ramp = np.clip((dist - ALPHA_LOW) / (ALPHA_HIGH - ALPHA_LOW), 0.0, 1.0) * 255.0
    alpha[bg_region] = ramp[bg_region]

    rgba = np.dstack([arr, alpha]).astype(np.uint8)
    out_img = Image.fromarray(rgba, mode="RGBA")
    out_img.save(OUT)
    print(f"saved {OUT}  size={out_img.size}")

    # Sanity: report a few known sample points.
    def sample(name, x, y):
        print(f"  {name}: rgba={tuple(rgba[y, x])}")
    sample("top-left corner (0,0)", 0, 0)
    sample("wheel hub center", w // 2, h // 2)
    sample("wheel interior", w // 2 + 250, h // 2 - 100)
    sample("petal bright orange", w // 2 - 200, 250)


if __name__ == "__main__":
    main()
