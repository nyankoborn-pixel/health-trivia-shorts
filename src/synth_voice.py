"""
synth_voice.py

work/script.json の各シーンを VOICEVOX 青山龍星 (speaker_id=13) で WAV 化する。
出力: work/audio/scene_NN.wav
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import requests

WORK_DIR = Path("work")
AUDIO_DIR = WORK_DIR / "audio"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
SCRIPT_IN = WORK_DIR / "script.json"
AUDIO_INFO_OUT = WORK_DIR / "audio.json"

VOICEVOX_URL = os.environ.get("VOICEVOX_URL", "http://localhost:50021")
SPEAKER_ID = int(os.environ.get("VOICEVOX_SPEAKER_ID", "13"))  # 青山龍星 ノーマル
SPEED_SCALE = float(os.environ.get("VOICEVOX_SPEED", "1.05"))


def synth_one(text: str, out_path: Path) -> float:
    r = requests.post(
        f"{VOICEVOX_URL}/audio_query",
        params={"text": text, "speaker": SPEAKER_ID},
        timeout=30,
    )
    r.raise_for_status()
    q = r.json()
    q["speedScale"] = SPEED_SCALE
    q["volumeScale"] = 1.0

    r = requests.post(
        f"{VOICEVOX_URL}/synthesis",
        params={"speaker": SPEAKER_ID},
        json=q,
        timeout=120,
    )
    r.raise_for_status()
    out_path.write_bytes(r.content)

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(out_path)],
        capture_output=True, text=True, check=True,
    )
    return float(probe.stdout.strip())


def main() -> int:
    if not SCRIPT_IN.exists():
        print(f"ERROR: {SCRIPT_IN} not found", file=sys.stderr)
        return 1

    # VOICEVOX 起動確認
    try:
        v = requests.get(f"{VOICEVOX_URL}/version", timeout=5)
        print(f"[synth] VOICEVOX version: {v.text}")
    except Exception as e:
        print(f"ERROR: VOICEVOX not reachable at {VOICEVOX_URL}: {e}", file=sys.stderr)
        return 1

    script = json.loads(SCRIPT_IN.read_text(encoding="utf-8"))
    scenes = script["scenes"]

    audio_info: list[dict] = []
    total = 0.0
    for i, scene in enumerate(scenes):
        sid = scene.get("id", f"{i+1:02d}")
        out = AUDIO_DIR / f"scene_{i:02d}.wav"
        duration = synth_one(scene["text"], out)
        audio_info.append({
            "id": sid,
            "index": i,
            "wav": str(out.resolve()),
            "duration": duration,
        })
        total += duration
        print(f"  [scene {sid}] {duration:.2f}s -> {out.name}")

    AUDIO_INFO_OUT.write_text(
        json.dumps({"scenes": audio_info, "total_duration": total}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[synth] total duration: {total:.2f}s")
    print(f"[synth] saved: {AUDIO_INFO_OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
