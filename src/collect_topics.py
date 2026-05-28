"""
collect_topics.py

Gemini 2.x API（google_search ツールで Web 検索結果を grounding）で
健康雑学トピック候補を JSON で出力する。

出力: work/topics.json  ([{"title":"...", "body":"...", "sources":["url",...]}, ...])

注: 新しい google-genai SDK を使用。旧 google-generativeai は Gemini 2.x の
google_search ツールに対応していないため。
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from google import genai
from google.genai import types

from src.gemini_retry import call_with_retry, log_finish_reason

WORK_DIR = Path("work")
WORK_DIR.mkdir(exist_ok=True)
TOPICS_OUT = WORK_DIR / "topics.json"

# 過去使用トピックの履歴（重複ネタ防止のためコミットされる）
HISTORY_PATH = Path("logs/topic_history.jsonl")
HISTORY_WINDOW_DAYS = 60     # この期間内の既出トピックは除外対象
HISTORY_MAX_EXCLUDE = 80     # プロンプトに渡す最大件数（古い順に切り詰め）

MODEL = "gemini-2.5-flash"

TARGET_TOPIC_COUNT = 7

SEED_THEMES = [
    "中高年が言ってはいけないこと",
    "中高年がやめなければいけないこと",
    "中高年がやってはいけないこと",
    "中高年が後悔すること",
    "中高年のお金",
    "中高年の人間関係",
]


def load_recent_titles(window_days: int = HISTORY_WINDOW_DAYS, limit: int = HISTORY_MAX_EXCLUDE) -> list[str]:
    """過去 window_days 日以内に使用したトピックタイトルを返す（新しい順）。limit 件まで。"""
    if not HISTORY_PATH.exists():
        return []
    cutoff = datetime.now(timezone(timedelta(hours=9))) - timedelta(days=window_days)
    titles: list[tuple[datetime, str]] = []
    for line in HISTORY_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            ts = datetime.fromisoformat(rec["timestamp"])
            for t in rec.get("topics", []):
                if ts > cutoff:
                    titles.append((ts, t))
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
    titles.sort(key=lambda x: x[0], reverse=True)
    return [t for _, t in titles[:limit]]


def append_to_history(topics: list[dict]) -> None:
    """生成したトピックを履歴に追記。"""
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "timestamp": datetime.now(timezone(timedelta(hours=9))).isoformat(),
        "topics": [t.get("title", "") for t in topics],
    }
    with HISTORY_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def build_research_prompt() -> str:
    """Step 1: grounding で自由文ファクト収集するためのプロンプト"""
    today = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
    # 全 SEED_THEMES を提示し、必ずこのリスト内のテーマに該当させる
    seeds = SEED_THEMES

    # 過去使用済みトピック（重複防止）
    recent = load_recent_titles()
    exclude_block = ""
    if recent:
        bullet = "\n".join(f"  - {t}" for t in recent)
        exclude_block = f"""

【★既出トピック（過去 {HISTORY_WINDOW_DAYS} 日以内に使用済み・今回は避ける）】
{bullet}

上記と同じテーマや切り口は提案しないこと。違う角度・違う対象を必ず選ぶ。"""

    seeds_bullet = "\n".join(f"  - {s}" for s in seeds)
    return f"""今日は {today} です。日本の中高年向け雑学 YouTube 動画用のネタを Google 検索で収集してください。

【条件】
- {TARGET_TOPIC_COUNT} 個の独立した雑学トピックを集める
- 各トピックは「知っているとちょっと差がつく」レベルの雑学
- アフィリエイト目的のサイト・断定的に治療効果を謳う記事は除外
- 健康・医療系トピックでは断定表現を避ける。「〜と言われています」など

【★採用テーマ（必ず以下のいずれかに該当させること。リスト外のテーマは禁止）】
{seeds_bullet}

{TARGET_TOPIC_COUNT} 件全体で、上記テーマから複数の異なる項目をバランス良く混ぜること。1 つのテーマばかりに偏らせない。{exclude_block}

【出力】 各トピックを以下のフォーマットで列挙してください（自由文で構いません）:

トピック {{番号}}: 見出し（20字以内）
本文: 150〜200 字の日本語ナレーション原稿（30 秒で話せる量）
出典: 参照した URL を箇条書き

トピック 2: 見出し
...

JSON 形式は不要です。番号付きの素直なテキストで出力してください。"""


def build_structuring_prompt(research_text: str) -> str:
    """Step 2: 自由文の調査結果を JSON 配列に整形するためのプロンプト"""
    return f"""以下は健康雑学の調査結果です。これを JSON 配列に整形してください。

# 調査結果
{research_text}

# 出力形式
JSON 配列のみ。各要素は以下のキーを持つオブジェクト:
- title: 文字列（20字以内の見出し）
- body: 文字列（150〜200字の本文）
- sources: 文字列の配列（出典URL、無ければ空配列 []）

調査結果に含まれるトピック数だけ配列要素を作ってください。
他のキーは追加しないこと。
"""


def call_gemini_with_search() -> list[dict]:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")

    client = genai.Client(api_key=api_key)

    # === Step 1: grounding で自由文ファクト収集 ===
    print(f"[collect] step1: research with {MODEL} + google_search")
    research = call_with_retry(
        lambda: client.models.generate_content(
            model=MODEL,
            contents=build_research_prompt(),
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.85,
                max_output_tokens=16384,
            ),
        ),
        label="collect-step1",
    )
    log_finish_reason(research, "collect-step1")
    research_text = (research.text or "").strip()
    (WORK_DIR / "_collect_raw.txt").write_text(research_text, encoding="utf-8")
    if not research_text:
        raise RuntimeError("Step 1 grounding returned empty (safety filter?)")
    print(f"[collect] step1 done: {len(research_text)} chars of research")

    # === Step 2: JSON モードで構造化（grounding なし、温度低め、JSON 失敗時 1 回再試行）===
    topics = None
    last_err: Exception | None = None
    for attempt in range(2):
        print(f"[collect] step2: structuring to JSON (attempt {attempt + 1})")
        structured = call_with_retry(
            lambda: client.models.generate_content(
                model=MODEL,
                contents=build_structuring_prompt(research_text),
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.2,
                    max_output_tokens=16384,
                ),
            ),
            label="collect-step2",
        )
        log_finish_reason(structured, f"collect-step2-attempt{attempt + 1}")
        json_text = (structured.text or "").strip()
        (WORK_DIR / f"_collect_json_{attempt}.txt").write_text(json_text, encoding="utf-8")
        if not json_text:
            last_err = RuntimeError("Step 2 returned empty")
            if attempt == 0:
                continue
            raise last_err
        try:
            topics = _safe_json_loads(json_text)
            break
        except json.JSONDecodeError as e:
            last_err = e
            print(f"[collect] step2 JSON parse failed: {e}")
            if attempt == 0:
                print("[collect] retrying step2")
                continue
            raise

    if topics is None:
        raise RuntimeError(f"collect step2 failed: {last_err}")
    if not isinstance(topics, list):
        raise RuntimeError(f"expected list, got {type(topics)}")
    return topics


def _safe_json_loads(text: str):
    """grounding 出力に混入しがちな制御文字 (BEL, BS, 等) を許容して JSON パースする。

    まず strict=False で試し（\t \n \r を文字列内に許可）、それでも落ちるなら
    印字不可な ASCII 制御文字 (0-31 のうち \t \n \r 以外) を空白に置換して再試行する。
    """
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError:
        sanitized = "".join(
            " " if (ord(c) < 32 and c not in "\t\n\r") else c
            for c in text
        )
        return json.loads(sanitized, strict=False)


def validate_topics(topics: list[dict]) -> list[dict]:
    valid = []
    for i, t in enumerate(topics):
        if not all(k in t for k in ("title", "body")):
            print(f"  [skip] topic {i} missing required keys: {t}")
            continue
        if len(t["body"]) < 50:
            print(f"  [skip] topic {i} body too short ({len(t['body'])}字)")
            continue
        t.setdefault("sources", [])
        valid.append(t)
    return valid


MIN_TOPICS_OK = 5      # この件数以上ならそのまま採用
MIN_TOPICS_ACCEPT = 3  # この件数未満なら失敗扱い


def main() -> int:
    # 1回目で 5 件未満 or 例外なら 1 回だけ再試行（Gemini 混雑時の短縮応答や transient エラー対策）
    import traceback as _tb
    topics: list[dict] = []
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            topics = call_gemini_with_search()
            topics = validate_topics(topics)
            print(f"[collect] attempt {attempt + 1}: {len(topics)} valid topics")
            if len(topics) >= MIN_TOPICS_OK:
                break
            if attempt == 0:
                print(f"[collect] under threshold ({MIN_TOPICS_OK}); retrying once")
        except Exception as e:
            last_err = e
            print(f"[collect] attempt {attempt + 1} crashed: {type(e).__name__}: {e}", file=sys.stderr)
            _tb.print_exc()
            if attempt == 0:
                print(f"[collect] retrying once after exception")

    if len(topics) < MIN_TOPICS_ACCEPT:
        if last_err is not None and len(topics) == 0:
            print(f"ERROR: collect failed: {type(last_err).__name__}: {last_err}", file=sys.stderr)
        else:
            print(f"ERROR: only {len(topics)} valid topics after retry, need at least {MIN_TOPICS_ACCEPT}", file=sys.stderr)
        return 1

    TOPICS_OUT.write_text(
        json.dumps(topics, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[collect] {len(topics)} topics saved to {TOPICS_OUT}")
    for t in topics[:7]:
        print(f"  - {t['title']}")

    # 履歴に追記（次回ビルドの重複防止のため）
    append_to_history(topics)
    print(f"[collect] history updated: {HISTORY_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
