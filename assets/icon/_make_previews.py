"""One-off script: composite every generated icon size onto three
background swatches (white, dark #1e1e1e, brand red #7a1f2b) as a grid,
one PNG per background, for visual review. Not part of the app."""
from PIL import Image, ImageDraw, ImageFont

ICON_DIR = r"C:\Users\Morne\Projects\Samsara-dev\assets\icon"
OUT_DIR = r"C:\Users\Morne\Documents\Claude\ui_proof\icon"
SIZES = [256, 128, 64, 48, 32, 16]
BACKGROUNDS = {
    "white": (255, 255, 255),
    "dark_1e1e1e": (0x1E, 0x1E, 0x1E),
    "red_7a1f2b": (0x7A, 0x1F, 0x2B),
}

CELL = 300
PAD = 24
LABEL_H = 28


def main():
    icons = {}
    for size in SIZES:
        path = f"{ICON_DIR}\\samsara_{size}.png"
        icons[size] = Image.open(path).convert("RGBA")
        print(f"loaded samsara_{size}.png -> {icons[size].size}")

    try:
        font = ImageFont.truetype("segoeui.ttf", 18)
    except OSError:
        font = ImageFont.load_default()

    n = len(SIZES)
    sheet_w = n * CELL + (n + 1) * PAD
    sheet_h = CELL + 2 * PAD + LABEL_H

    for bg_name, bg_rgb in BACKGROUNDS.items():
        sheet = Image.new("RGB", (sheet_w, sheet_h), bg_rgb)
        draw = ImageDraw.Draw(sheet)
        text_color = (20, 20, 20) if bg_name == "white" else (235, 235, 235)

        for i, size in enumerate(SIZES):
            cell_x = PAD + i * (CELL + PAD)
            cell_y = PAD
            icon = icons[size]
            paste_x = cell_x + (CELL - size) // 2
            paste_y = cell_y + (CELL - LABEL_H - size) // 2
            sheet.paste(icon, (paste_x, paste_y), icon)
            label = f"{size}px"
            draw.text((cell_x + CELL // 2 - 18, cell_y + CELL - LABEL_H + 4),
                       label, fill=text_color, font=font)

        out_path = f"{OUT_DIR}\\preview_{bg_name}.png"
        sheet.save(out_path)
        print(f"saved {out_path} ({sheet.size})")


if __name__ == "__main__":
    main()
