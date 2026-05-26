"""
make_thumbnail.py

work/script.json と work/images/*.png から、1280x720 のサムネイル画像を生成する。
- 白背景にシーン 1 のいらすとや画像を中央配置
- 上部に動画タイトル（太字黒）を 2-3 行で大きく配置

Anthropic API を使ってタイトルからキャッチコピーを抽出する処理は行わず、
script.json の title をそのまま使う（オーバーキルを避ける）。
"""
from __future__ import annotations

import json
import platform
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WORK_DIR = Path("work")
OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)
SCRIPT_IN = WORK_DIR / "script.json"
IMAGES_INFO_IN = WORK_DIR / "images.json"

THUMB_W, THUMB_H = 1280, 720

_LINUX_FONT_BOLD = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
_WIN_FONT_BOLD = "C:/Windows/Fonts/YuGothB.ttc"


def detect_font() -> str:
    if platform.system() == "Windows" and Path(_WIN_FONT_BOLD).exists():
        return _WIN_FONT_BOLD
    if Path(_LINUX_FONT_BOLD).exists():
        return _LINUX_FONT_BOLD
    raise RuntimeError("font not found")


def wrap_for_thumb(s: str, width: int) -> list[str]:
    """サムネ用に N 文字で折り返し（最大 3 行）"""
    lines: list[str] = []
    cur = ""
    for ch in s:
        cur += ch
        if len(cur) >= width:
            lines.append(cur)
            cur = ""
        if len(lines) == 3:
            break
    if cur and len(lines) < 3:
        lines.append(cur)
    return lines


def main() -> int:
    if not SCRIPT_IN.exists() or not IMAGES_INFO_IN.exists():
        print(f"ERROR: required inputs not found", file=sys.stderr)
        return 1

    script = json.loads(SCRIPT_IN.read_text(encoding="utf-8"))
    images_info = json.loads(IMAGES_INFO_IN.read_text(encoding="utf-8"))
    title = script.get("title", "健康雑学")

    # シーン 1 の画像をサムネに採用（なければシーン 2, 3, ...）
    image_path: Path | None = None
    for scene in script["scenes"]:
        sid = scene.get("id")
        if sid in images_info["images"]:
            image_path = Path(images_info["images"][sid])
            break
    if image_path is None or not image_path.exists():
        print("ERROR: no scene image available for thumbnail", file=sys.stderr)
        return 1

    # キャンバス作成（白背景）
    canvas = Image.new("RGB", (THUMB_W, THUMB_H), "white")

    # 画像配置: 下半分中央に高さ 440 でフィット
    img = Image.open(image_path).convert("RGBA")
    target_h = 460
    ratio = target_h / img.height
    new_w = int(img.width * ratio)
    img = img.resize((new_w, target_h), Image.LANCZOS)
    img_x = (THUMB_W - new_w) // 2
    img_y = THUMB_H - target_h - 20
    canvas.paste(img, (img_x, img_y), img)

    # タイトル: 上部 240px 領域に太字黒で 2-3 行
    font_path = detect_font()
    draw = ImageDraw.Draw(canvas)

    lines = wrap_for_thumb(title, 13)
    font_size = 92 if len(lines) <= 2 else 76
    font = ImageFont.truetype(font_path, font_size)
    line_h = font_size + 14
    total_h = line_h * len(lines)
    start_y = (220 - total_h) // 2 + 10

    for i, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font)
        text_w = bbox[2] - bbox[0]
        x = (THUMB_W - text_w) // 2
        y = start_y + i * line_h
        # 黒文字に白縁取り（背景が白でも文字が画像にかぶる時の保険）
        for dx in (-2, 0, 2):
            for dy in (-2, 0, 2):
                if dx == 0 and dy == 0:
                    continue
                draw.text((x + dx, y + dy), line, font=font, fill="white")
        draw.text((x, y), line, font=font, fill="black")

    # 出力: 同名動画と並ぶ .jpg
    import re
    safe = re.sub(r"[\\/:*?\"<>|]", "_", title)[:60].replace(" ", "_")
    import os, datetime
    today = os.environ.get("HEALTH_TRIVIA_DATE") or datetime.datetime.now().strftime("%Y-%m-%d")
    out = OUTPUT_DIR / f"{today}_{safe}_thumb.jpg"
    canvas.save(out, "JPEG", quality=92)
    print(f"[thumb] saved: {out} ({out.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
