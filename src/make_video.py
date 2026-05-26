"""
make_video.py

work/script.json + work/audio/*.wav + work/images/*.png から
1920x1080 / 16:9 / 約3分の動画を組み立てる。

参考動画(0KwNzceBEF0) のスタイル:
- 白背景中央にいらすとや画像を大きく配置
- 画面上部に太字黒テロップを焼き込み
- ナレーション+BGM (Escort.mp3 をシームレスループ)
- 各シーン = 1 ナレーションを 1 静止画で表示（必要なら今後画像ローテーション拡張）
"""
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

WORK_DIR = Path("work")
OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

SCRIPT_IN = WORK_DIR / "script.json"
AUDIO_INFO_IN = WORK_DIR / "audio.json"
IMAGES_INFO_IN = WORK_DIR / "images.json"

BGM_PATH = Path("assets/bgm/Escort.mp3")
BGM_VOLUME_DB = -16  # ナレーション基準で -16dB（雑学チャンネル想定の控えめ）

W, H = 1920, 1080
FPS = 30

# フォント解決: Linux runner では Noto Sans CJK Bold、Windows ローカルでは Yu Gothic UI Bold
_LINUX_FONT_BOLD = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
_LINUX_FONT_REG = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
_WIN_FONT_BOLD = "C:/Windows/Fonts/YuGothB.ttc"
_WIN_FONT_REG = "C:/Windows/Fonts/YuGothR.ttc"


def detect_fonts() -> tuple[str, str]:
    if platform.system() == "Windows" and Path(_WIN_FONT_BOLD).exists():
        return _WIN_FONT_BOLD, _WIN_FONT_REG
    if Path(_LINUX_FONT_BOLD).exists():
        return _LINUX_FONT_BOLD, _LINUX_FONT_REG
    raise RuntimeError("font not found; install fonts-noto-cjk on Linux or Yu Gothic on Windows")


def ff_path(p: str | Path) -> str:
    """ffmpeg drawtext などで使えるパス文字列を返す（Windows のドライブレターをエスケープ）"""
    s = str(Path(p)).replace("\\", "/")
    if platform.system() == "Windows":
        s = re.sub(r"^([A-Za-z]):", r"\1\\:", s)
    return s


def wrap_text(s: str, width: int) -> str:
    """日本語向けの単純文字幅折り返し"""
    lines = []
    cur = ""
    for ch in s:
        cur += ch
        if len(cur) >= width:
            lines.append(cur)
            cur = ""
    if cur:
        lines.append(cur)
    return "\n".join(lines)


def make_scene_clip(
    *,
    index: int,
    scene: dict,
    wav_path: Path,
    image_path: Path,
    duration: float,
    out_path: Path,
    font_bold: str,
) -> None:
    """1 シーン分の mp4 を生成。白背景に画像中央配置 + 上部太字黒テロップ"""
    telop = scene["telop"]
    telop_wrapped = wrap_text(telop, 16)
    telop_file = WORK_DIR / f"telop_{index:02d}.txt"
    telop_file.write_text(telop_wrapped, encoding="utf-8")

    # 画像サイズ: 縦は表示領域の 60% (テロップ帯を上 25% 確保)、幅は同じ比率内に収める
    img_h = int(H * 0.62)
    img_y = int(H * 0.32)  # 上部 32% 〜 から表示

    ff_bold = ff_path(font_bold)
    ff_telop = ff_path(telop_file)
    ff_img = str(Path(image_path).resolve()).replace("\\", "/")
    ff_wav = str(wav_path.resolve()).replace("\\", "/")

    # フィルタ:
    # 1. 白背景 1920x1080 を color source で生成
    # 2. 画像を高さ img_h にスケール（縦横比維持）、白背景中央上寄り (img_y) に overlay
    # 3. 上部に太字黒テロップ (画面上部 8% 〜)
    filter_complex = ";".join([
        f"color=c=white:s={W}x{H}:r={FPS}[bg]",
        f"[1:v]scale=-1:{img_h}:flags=lanczos,format=rgba[img]",
        f"[bg][img]overlay=x=(W-w)/2:y={img_y}[base]",
        (
            f"[base]drawtext=fontfile='{ff_bold}':textfile='{ff_telop}':"
            f"fontcolor=black:fontsize=80:"
            f"x=(w-text_w)/2:y={int(H * 0.05)}:"
            f"line_spacing=14:borderw=2:bordercolor=white[v]"
        ),
    ])

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-loop", "1", "-i", str(image_path),
        "-i", str(wav_path),
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "1:a",
        "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-r", str(FPS),
        "-ar", "48000", "-ac", "2",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        "-t", f"{duration:.3f}",
        str(out_path),
    ]
    print(f"  [scene {index}] {duration:.2f}s -> {out_path.name}")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-3000:], file=sys.stderr)
        raise RuntimeError(f"ffmpeg failed for scene {index}")


def concat_clips(clip_paths: list[Path], out_path: Path) -> None:
    """シーン群を concat。AAC 境界の glitch 対策で音声は再エンコード"""
    list_file = WORK_DIR / "concat.txt"
    with open(list_file, "w", encoding="utf-8") as f:
        for p in clip_paths:
            f.write(f"file '{p.resolve().as_posix()}'\n")

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-af", "aresample=async=1000",
        str(out_path),
    ]
    print(f"[concat] -> {out_path.name}")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-3000:], file=sys.stderr)
        raise RuntimeError("concat failed")


def mix_bgm(in_video: Path, bgm_path: Path, out_video: Path, bgm_db: int = BGM_VOLUME_DB) -> None:
    """ナレーション動画に BGM をシームレスループでミックス。
    -stream_loop -1 で無限ループ入力にし、-shortest で動画長に合わせて打ち切る。
    """
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-i", str(in_video),
        "-stream_loop", "-1", "-i", str(bgm_path),
        "-filter_complex",
        f"[1:a]volume={bgm_db}dB[bgm];"
        f"[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=0[aout]",
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        str(out_video),
    ]
    print(f"[bgm-mix] -> {out_video.name} (volume {bgm_db}dB, seamless loop)")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-3000:], file=sys.stderr)
        raise RuntimeError("bgm mix failed")


def safe_filename(s: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|]", "_", s)
    s = re.sub(r"\s+", "_", s.strip())
    return s[:60]


def main() -> int:
    for p in (SCRIPT_IN, AUDIO_INFO_IN, IMAGES_INFO_IN):
        if not p.exists():
            print(f"ERROR: {p} not found", file=sys.stderr)
            return 1

    script = json.loads(SCRIPT_IN.read_text(encoding="utf-8"))
    audio_info = json.loads(AUDIO_INFO_IN.read_text(encoding="utf-8"))
    images_info = json.loads(IMAGES_INFO_IN.read_text(encoding="utf-8"))

    font_bold, _ = detect_fonts()
    print(f"[make_video] font: {font_bold}")

    scenes = script["scenes"]
    audio_scenes = audio_info["scenes"]
    images = images_info["images"]

    clip_paths: list[Path] = []
    for i, scene in enumerate(scenes):
        sid = scene.get("id", f"{i+1:02d}")
        ai = audio_scenes[i]
        wav_path = Path(ai["wav"])
        duration = ai["duration"] + 0.2  # 末尾余韻
        if sid not in images:
            print(f"WARN: no image for scene {sid}, skipping clip", file=sys.stderr)
            continue
        image_path = Path(images[sid])
        clip_out = WORK_DIR / f"clip_{i:02d}.mp4"
        make_scene_clip(
            index=i, scene=scene, wav_path=wav_path, image_path=image_path,
            duration=duration, out_path=clip_out, font_bold=font_bold,
        )
        clip_paths.append(clip_out)

    if not clip_paths:
        print("ERROR: no clips generated", file=sys.stderr)
        return 1

    raw_path = WORK_DIR / "concat_raw.mp4"
    concat_clips(clip_paths, raw_path)

    if not BGM_PATH.exists():
        print(f"ERROR: BGM not found at {BGM_PATH}", file=sys.stderr)
        return 1

    today = os.environ.get("HEALTH_TRIVIA_DATE") or __import__("datetime").datetime.now().strftime("%Y-%m-%d")
    title_safe = safe_filename(script.get("title", "untitled"))
    final = OUTPUT_DIR / f"{today}_{title_safe}.mp4"
    mix_bgm(raw_path, BGM_PATH, final)

    # メタ情報も併置（YouTube 説明欄用）
    meta = {
        "title": script.get("title"),
        "description": script.get("description", ""),
        "scenes": len(scenes),
        "video_path": str(final.resolve()),
    }
    (OUTPUT_DIR / f"{today}_{title_safe}.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    size_mb = final.stat().st_size / (1024 * 1024)
    print(f"\n[make_video] DONE: {final} ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
