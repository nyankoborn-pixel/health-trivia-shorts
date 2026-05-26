"""
build.py

健康雑学動画パイプラインの統合エントリポイント。
順次:
  1. collect_topics  Google 検索 + Claude でトピック候補を抽出
  2. generate_script Claude で台本生成
  3. fetch_irasutoya いらすとや画像取得
  4. synth_voice     VOICEVOX で音声合成
  5. make_video      ffmpeg で動画組み立て
  6. make_thumbnail  サムネ画像生成

各ステップは独立 main を持つので、--from / --to で部分実行も可能。
"""
from __future__ import annotations

import argparse
import importlib
import sys
import time

STEPS = [
    ("collect", "src.collect_topics"),
    ("script", "src.generate_script"),
    ("images", "src.fetch_irasutoya"),
    ("voice", "src.synth_voice"),
    ("video", "src.make_video"),
    ("thumb", "src.make_thumbnail"),
]


def run_step(name: str, module_path: str) -> int:
    print(f"\n{'=' * 60}\n[{name}] {module_path}\n{'=' * 60}")
    t0 = time.time()
    mod = importlib.import_module(module_path)
    rc = mod.main()
    elapsed = time.time() - t0
    print(f"[{name}] rc={rc} elapsed={elapsed:.1f}s")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="frm", default=None, help="開始ステップ名")
    ap.add_argument("--to", dest="to", default=None, help="終了ステップ名（含む）")
    args = ap.parse_args()

    names = [s[0] for s in STEPS]
    start_idx = names.index(args.frm) if args.frm else 0
    end_idx = names.index(args.to) if args.to else len(STEPS) - 1
    if start_idx > end_idx:
        print(f"ERROR: --from '{args.frm}' is after --to '{args.to}'", file=sys.stderr)
        return 1

    for name, module_path in STEPS[start_idx:end_idx + 1]:
        rc = run_step(name, module_path)
        if rc != 0:
            print(f"\n[build] FAIL at step '{name}' (rc={rc})", file=sys.stderr)
            return rc

    print(f"\n[build] ALL DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
