from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

BOATS_DIR = Path(__file__).parent
OUTPUT_DIR = BOATS_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

CARD_WIDTH = 400
PHOTO_HEIGHT = 250
ROW_HEIGHT = 36
PADDING = 20
COL1_WIDTH = 220
COL2_WIDTH = CARD_WIDTH - COL1_WIDTH
TABLE_WIDTH = CARD_WIDTH
BG_COLOR = (245, 245, 245)
HEADER_COLOR = (30, 60, 100)
ROW_ALT_COLOR = (225, 232, 245)
ROW_COLOR = (255, 255, 255)
TEXT_COLOR = (30, 30, 30)
HEADER_TEXT_COLOR = (255, 255, 255)

try:
    font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 16)
    font_bold = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 16)
except:
    font = ImageFont.load_default()
    font_bold = font


def parse_txt(path):
    rows = []
    lines = path.read_text().splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if "\t" in line:
            key, val = line.split("\t", 1)
            rows.append((key.strip(), val.strip()))
        elif line and i + 1 < len(lines) and not lines[i + 1].strip().startswith("\t"):
            # value on next line
            next_line = lines[i + 1].strip()
            if next_line and "\t" not in next_line:
                rows.append((line, next_line))
                i += 2
                continue
        i += 1
    return rows


def make_table_image(rows, width):
    n = len(rows)
    height = ROW_HEIGHT * (n + 1) + PADDING  # +1 for header
    img = Image.new("RGB", (width, height), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # Header
    draw.rectangle([0, 0, width, ROW_HEIGHT], fill=HEADER_COLOR)
    draw.text((PADDING, 8), "Eigenschap", font=font_bold, fill=HEADER_TEXT_COLOR)
    draw.text((COL1_WIDTH + PADDING, 8), "Waarde", font=font_bold, fill=HEADER_TEXT_COLOR)

    for i, (key, val) in enumerate(rows):
        y = ROW_HEIGHT * (i + 1)
        bg = ROW_ALT_COLOR if i % 2 == 0 else ROW_COLOR
        draw.rectangle([0, y, width, y + ROW_HEIGHT], fill=bg)
        draw.text((PADDING, y + 8), key, font=font, fill=TEXT_COLOR)
        draw.text((COL1_WIDTH + PADDING, y + 8), val, font=font, fill=TEXT_COLOR)
        draw.line([0, y + ROW_HEIGHT, width, y + ROW_HEIGHT], fill=(200, 200, 200))

    draw.line([COL1_WIDTH, 0, COL1_WIDTH, height], fill=(180, 180, 180))
    return img


def process(img_path, txt_path, out_path):
    photo = Image.open(img_path).convert("RGB")
    rows = parse_txt(txt_path)

    # Crop/resize photo to fixed CARD_WIDTH x PHOTO_HEIGHT (center crop)
    scale = max(CARD_WIDTH / photo.width, PHOTO_HEIGHT / photo.height)
    resized = photo.resize((int(photo.width * scale), int(photo.height * scale)), Image.LANCZOS)
    left = (resized.width - CARD_WIDTH) // 2
    top = (resized.height - PHOTO_HEIGHT) // 2
    photo = resized.crop((left, top, left + CARD_WIDTH, top + PHOTO_HEIGHT))

    table = make_table_image(rows, CARD_WIDTH)

    combined = Image.new("RGB", (CARD_WIDTH, PHOTO_HEIGHT + table.height), BG_COLOR)
    combined.paste(photo, (0, 0))
    combined.paste(table, (0, PHOTO_HEIGHT))
    combined.save(out_path)
    print(f"Saved {out_path}")


ids = sorted(set(p.stem for p in BOATS_DIR.glob("*.txt")))
for id_ in ids:
    img_path = next(BOATS_DIR.glob(f"{id_}.jp*"), None)
    txt_path = BOATS_DIR / f"{id_}.txt"
    if img_path and txt_path.exists():
        process(img_path, txt_path, OUTPUT_DIR / f"{id_}.jpg")
