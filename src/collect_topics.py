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

from src.gemini_retry import call_with_retry

WORK_DIR = Path("work")
WORK_DIR.mkdir(exist_ok=True)
TOPICS_OUT = WORK_DIR / "topics.json"

MODEL = "gemini-2.5-flash"

TARGET_TOPIC_COUNT = 7

SEED_THEMES = [
    "中高年の健康",
    "中高年の習慣",
    "中高年の雑学",
    "中高年のランキング",
    "中高年の食事",
    "中高年の運動",
    "中高年の睡眠",
    "中高年のストレス",
    "中高年の体の変化",
    "中高年の生活",
    "中高年の脳と記憶",
    "中高年の疲労回復",
    "中高年のNG習慣",
    "中高年の若返り",
]


def build_research_prompt() -> str:
    """Step 1: grounding で自由文ファクト収集するためのプロンプト"""
    today = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
    seeds = random.sample(SEED_THEMES, k=4)
    return f"""今日は {today} です。日本の中高年向け雑学 YouTube 動画用のネタを Google 検索で収集してください。

【条件】
- {TARGET_TOPIC_COUNT} 個の独立した雑学トピックを集める
- テーマ範囲は広く取る: 健康、生活、食、習慣、知恵、文化、歴史、季節、ランキングなど。**健康ジャンルばかりに偏らせない**こと
- 各トピックは「知っているとちょっと差がつく」レベルの雑学
- 信頼できる情報源（公的機関、大学、辞書・事典、大手メディア、専門家解説サイトなど）を複数横断して根拠を揃える
- 個人ブログ・アフィリエイトサイト・断定的に治療効果を謳う記事は除外
- 健康・医療系トピックでは断定表現を避ける。「〜と言われています」など
- 参考テーマ（これに限らず自由に展開してよい）: {", ".join(seeds)}

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
                max_output_tokens=8192,
            ),
        ),
        label="collect-step1",
    )
    research_text = (research.text or "").strip()
    (WORK_DIR / "_collect_raw.txt").write_text(research_text, encoding="utf-8")
    if not research_text:
        raise RuntimeError("Step 1 grounding returned empty (safety filter?)")
    print(f"[collect] step1 done: {len(research_text)} chars of research")

    # === Step 2: JSON モードで構造化（grounding なし、温度低め）===
    print(f"[collect] step2: structuring to JSON")
    structured = call_with_retry(
        lambda: client.models.generate_content(
            model=MODEL,
            contents=build_structuring_prompt(research_text),
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.2,
                max_output_tokens=8192,
            ),
        ),
        label="collect-step2",
    )
    json_text = (structured.text or "").strip()
    (WORK_DIR / "_collect_json.txt").write_text(json_text, encoding="utf-8")
    if not json_text:
        raise RuntimeError("Step 2 JSON structuring returned empty")

    topics = _safe_json_loads(json_text)
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


def main() -> int:
    topics = call_gemini_with_search()
    topics = validate_topics(topics)
    if len(topics) < 5:
        print(f"ERROR: only {len(topics)} valid topics, need at least 5", file=sys.stderr)
        return 1

    TOPICS_OUT.write_text(
        json.dumps(topics, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[collect] {len(topics)} topics saved to {TOPICS_OUT}")
    for t in topics[:5]:
        print(f"  - {t['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
