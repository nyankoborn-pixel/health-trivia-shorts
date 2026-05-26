"""字幕タイミング計算の smoke test。手動実行用。"""
import sys
sys.path.insert(0, ".")
from src.make_video import _compute_subtitle_timing
from src.generate_script import DISCLAIMER_SCENE


def label(pos: int) -> str:
    return "top" if pos == 0 else "bot"


def show(title: str, subs, dur):
    print(f"--- {title} (subs={len(subs)}, duration={dur}s) ---")
    timings = _compute_subtitle_timing(subs, dur)
    for i, (start, end, pos) in enumerate(timings):
        text = subs[i]
        print(f"  [{i}] {label(pos)}: '{text}' {start:.2f}~{end:.2f}s ({end - start:.2f}s 持続)")


if __name__ == "__main__":
    show(
        "通常シーン",
        ["朝日を浴びると", "体内時計がリセットされ", "夜の睡眠の質が高まると", "言われています"],
        30.0,
    )
    print()
    show("disclaimer", DISCLAIMER_SCENE["subtitles"], 9.45)
