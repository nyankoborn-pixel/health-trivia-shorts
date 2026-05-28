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
BGM_VOLUME_DB = -20  # ナレーション基準で -20dB（参考動画相当の控えめ）

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


# 字幕レイアウト定数
SUB_FONTSIZE = 96
SUB_Y_TOP = int(H * 0.04)         # 上字幕の y 位置 (≈ 43)
SUB_Y_BOTTOM = H - SUB_FONTSIZE - int(H * 0.04)  # 下字幕の y 位置 (≈ 941)
SUB_COLOR = "black"
# 画像エリア: 上下字幕の隙間に収まるサイズ。垂直中央配置。
IMG_HEIGHT_RATIO = 0.50           # H の 50% (= 540px)


def _compute_subtitle_timing(subtitles: list[str], duration: float) -> list[tuple[float, float, int]]:
    """各 subtitle の (開始秒, 終了秒, position) を計算。
    - 開始秒は累積文字数比率で proportional に割り振る
    - position は index % 2 (0=top, 1=bottom)
    - 終了秒は「同じ position の次の subtitle の開始秒」まで持続。
    """
    if not subtitles:
        return []
    total_chars = sum(len(s) for s in subtitles) or 1
    starts: list[float] = []
    cum = 0
    for s in subtitles:
        starts.append(duration * cum / total_chars)
        cum += len(s)

    n = len(subtitles)
    out: list[tuple[float, float, int]] = []
    for i in range(n):
        pos = i % 2
        next_same = None
        for j in range(i + 1, n):
            if j % 2 == pos:
                next_same = j
                break
        end = starts[next_same] if next_same is not None else duration + 0.5
        out.append((starts[i], end, pos))
    return out


def _compute_image_segments(n_images: int, duration: float) -> list[tuple[float, float]]:
    """画像ローテーションの (開始秒, 終了秒) を等分で計算。"""
    if n_images <= 0:
        return []
    slot = duration / n_images
    segs: list[tuple[float, float]] = []
    for i in range(n_images):
        start = i * slot
        end = (i + 1) * slot if i < n_images - 1 else duration + 0.5
        segs.append((start, end))
    return segs


def make_scene_clip(
    *,
    index: int,
    scene: dict,
    wav_path: Path,
    image_paths: list[Path],
    duration: float,
    out_path: Path,
    font_bold: str,
) -> None:
    """1 シーン分の mp4 を生成。
    - 白背景中央に画像（縮小）。複数枚なら等分時間でローテーション
    - subtitles 配列を上下交互に焼き込み、同位置の次が出るまで持続表示
    - important=True の subtitle は赤色 (SUB_COLOR_IMPORTANT) で描画
    """
    if not image_paths:
        raise RuntimeError(f"scene {index} has no images")

    subtitles = scene.get("subtitles") or []
    timings = _compute_subtitle_timing(subtitles, duration)
    img_segs = _compute_image_segments(len(image_paths), duration)

    # 各 subtitle テキストをファイルに書き出す
    sub_files: list[Path] = []
    for i, s in enumerate(subtitles):
        f = WORK_DIR / f"sub_{index:02d}_{i:02d}.txt"
        f.write_text(s, encoding="utf-8")
        sub_files.append(f)

    img_h = int(H * IMG_HEIGHT_RATIO)
    img_y = (H - img_h) // 2  # 垂直中央配置

    ff_bold = ff_path(font_bold)
    n_imgs = len(image_paths)

    # 入力: [0..n_imgs-1]=images (loop 1), [n_imgs]=wav
    cmd_inputs: list[str] = []
    for p in image_paths:
        cmd_inputs += ["-loop", "1", "-i", str(p)]
    cmd_inputs += ["-i", str(wav_path)]

    # フィルタ構築
    fc_parts: list[str] = [
        f"color=c=white:s={W}x{H}:r={FPS}[bg]",
    ]
    # 各画像を scale → 順次 overlay (enable で時間切替)
    for i in range(n_imgs):
        fc_parts.append(f"[{i}:v]scale=-1:{img_h}:flags=lanczos,format=rgba[img{i}]")
    last_label = "bg"
    for i, (start, end) in enumerate(img_segs):
        next_label = f"base{i}" if i < n_imgs - 1 else "base"
        fc_parts.append(
            f"[{last_label}][img{i}]overlay=x=(W-w)/2:y={img_y}:"
            f"enable='between(t\\,{start:.3f}\\,{end:.3f})'[{next_label}]"
        )
        last_label = next_label

    # 字幕 drawtext を順次重ねる
    for i, ((start, end, pos), sub_file) in enumerate(zip(timings, sub_files)):
        y = SUB_Y_TOP if pos == 0 else SUB_Y_BOTTOM
        ff_text = ff_path(sub_file)
        next_label = f"v{i}"
        fc_parts.append(
            f"[{last_label}]drawtext=fontfile='{ff_bold}':textfile='{ff_text}':"
            f"fontcolor={SUB_COLOR}:fontsize={SUB_FONTSIZE}:"
            f"x=(w-text_w)/2:y={y}:"
            f"borderw=2:bordercolor=white:"
            f"enable='between(t\\,{start:.3f}\\,{end:.3f})'[{next_label}]"
        )
        last_label = next_label

    # 最終出力ラベルを [v] に固定（null パススルー）
    fc_parts.append(f"[{last_label}]null[v]")
    filter_complex = ";".join(fc_parts)

    audio_input_idx = n_imgs  # wav は images の次

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        *cmd_inputs,
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", f"{audio_input_idx}:a",
        "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-r", str(FPS),
        "-ar", "48000", "-ac", "2",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        "-t", f"{duration:.3f}",
        str(out_path),
    ]
    print(f"  [scene {index}] {duration:.2f}s, {n_imgs} imgs, {len(subtitles)} subs -> {out_path.name}")
    # 1 回だけリトライ（一時的な encoder ノイズや I/O ブリップ対策）
    last_stderr = ""
    for attempt in range(2):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            if attempt > 0:
                print(f"  [scene {index}] ffmpeg succeeded on retry")
            return
        last_stderr = r.stderr
        if attempt == 0:
            print(f"  [scene {index}] ffmpeg failed (rc={r.returncode}), retrying once")
    # ここに来たら 2 回とも失敗
    log_file = WORK_DIR / f"_ffmpeg_scene_{index:02d}.log"
    log_file.write_text(last_stderr, encoding="utf-8")
    (WORK_DIR / f"_ffmpeg_scene_{index:02d}.filter.txt").write_text(filter_complex, encoding="utf-8")
    print(last_stderr[-3000:], file=sys.stderr)
    raise RuntimeError(f"ffmpeg failed for scene {index} after 2 attempts (full log: {log_file})")


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
    # amix は入力数で自動正規化（2 入力なら全体 -6dB）するため、mix 後に +6dB 戻す。
    # これでナレーションは元の音量を保持しつつ、BGM との相対差は bgm_db のまま。
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-i", str(in_video),
        "-stream_loop", "-1", "-i", str(bgm_path),
        "-filter_complex",
        f"[1:a]volume={bgm_db}dB[bgm];"
        f"[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=0,volume=6dB[aout]",
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        str(out_video),
    ]
    print(f"[bgm-mix] -> {out_video.name} (narr 0dB, bgm {bgm_db}dB, seamless loop)")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-3000:], file=sys.stderr)
        raise RuntimeError("bgm mix failed")


def safe_filename(s: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|]", "_", s)
    s = re.sub(r"\s+", "_", s.strip())
    return s[:60]


def _format_time(seconds: float) -> str:
    """秒数を MM:SS 形式に整形。"""
    total = int(seconds + 0.5)
    return f"{total // 60:02d}:{total % 60:02d}"


def _flatten_description(desc: str) -> str:
    """description から改行と過剰な空白を除去。コピペ用にプレーンテキスト化。"""
    if not desc:
        return ""
    # \r\n / \n / \r を全て空白に
    desc = re.sub(r"[\r\n]+", " ", desc)
    # 連続空白を 1 個に圧縮
    desc = re.sub(r"[ \t　]+", " ", desc)
    return desc.strip()


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
    timeline: list[dict] = []
    cumulative = 0.0
    for i, scene in enumerate(scenes):
        sid = scene.get("id", f"{i+1:02d}")
        ai = audio_scenes[i]
        wav_path = Path(ai["wav"])
        duration = ai["duration"] + 0.2  # 末尾余韻
        if sid not in images:
            print(f"WARN: no image for scene {sid}, skipping clip", file=sys.stderr)
            continue
        raw_imgs = images[sid]
        if isinstance(raw_imgs, str):
            raw_imgs = [raw_imgs]
        image_paths = [Path(p) for p in raw_imgs]
        clip_out = WORK_DIR / f"clip_{i:02d}.mp4"
        make_scene_clip(
            index=i, scene=scene, wav_path=wav_path, image_paths=image_paths,
            duration=duration, out_path=clip_out, font_bold=font_bold,
        )
        clip_paths.append(clip_out)
        # シーン単位のタイムライン（concat 時点での開始/終了秒）
        summary = scene.get("text", "") or ""
        summary = re.sub(r"\s+", " ", summary).strip()
        if len(summary) > 40:
            summary = summary[:40] + "…"
        timeline.append({
            "id": sid,
            "start": _format_time(cumulative),
            "end": _format_time(cumulative + duration),
            "duration_sec": round(duration, 2),
            "summary": summary,
        })
        cumulative += duration

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

    # メタ情報も併置（YouTube 説明欄用）。description は改行除去してコピペしやすく
    meta = {
        "title": script.get("title"),
        "description": _flatten_description(script.get("description", "")),
        "scene_count": len(timeline),
        "total_duration": _format_time(cumulative),
        "timeline": timeline,
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
