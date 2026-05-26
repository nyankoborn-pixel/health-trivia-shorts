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

WORK_DIR = Path("work")
WORK_DIR.mkdir(exist_ok=True)
TOPICS_OUT = WORK_DIR / "topics.json"

MODEL = "gemini-2.5-flash"

TARGET_TOPIC_COUNT = 7

SEED_THEMES = [
    "睡眠", "食事", "運動", "ストレス", "免疫", "腸内環境",
    "姿勢", "目の健康", "肩こり", "冷え性", "代謝", "水分補給",
    "歯と口腔", "心の健康", "アンチエイジング", "ビタミン",
    "デスクワーク", "朝の習慣", "夜の習慣", "呼吸",
]


def build_prompt() -> str:
    today = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
    seeds = random.sample(SEED_THEMES, k=4)
    return f"""今日は {today} です。日本の一般視聴者向けの「健康雑学」YouTube 動画用に、以下の条件でトピック候補を {TARGET_TOPIC_COUNT} 件抽出してください。

【条件】
- 各トピックは「知っているとちょっと差がつく」レベルの健康・体・心理に関する雑学
- 信頼できる情報源（厚生労働省、医療法人、製薬会社、大学、査読論文の解説記事、NHK、大手健康メディア）の記事を Google 検索で複数横断し、ファクトを揃える
- 個人ブログ・アフィリエイトサイト・断定的に治療効果を謳う記事は除外
- 医療行為の代替や特定疾患の治療法を断定する内容は禁止
- 今回のテーマ候補（参考）: {", ".join(seeds)}（これらに限定せず、面白い切り口があれば自由）

【各トピックの粒度】
- 30 秒の音声ナレーションで成立する分量
- 「事実 1 〜 2 つ + その背景 + 視聴者にとっての実生活への示唆」程度

【出力形式】
JSON 配列のみ。前後の説明文・マークダウン記号は付けない。各要素は以下:

[
  {{
    "title": "短い見出し（20字以内）",
    "body": "30秒で話せる本文（150〜200字程度の日本語）",
    "sources": ["参照した記事URL", ...]
  }},
  ...
]

JSON配列のみを出力してください。マークダウンの ``` も付けないでください。"""


def call_gemini_with_search() -> list[dict]:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")

    client = genai.Client(api_key=api_key)

    prompt = build_prompt()
    print(f"[collect] requesting {TARGET_TOPIC_COUNT} topics from {MODEL} with google_search")
    response = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.85,
            max_output_tokens=4096,
        ),
    )

    text = (response.text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)

    # 先頭から [ ... ] を抽出（前置きが入った場合の保険）
    m = re.search(r"\[\s*\{.*\}\s*\]", text, re.DOTALL)
    if m:
        text = m.group(0)

    topics = json.loads(text)
    if not isinstance(topics, list):
        raise RuntimeError(f"expected list, got {type(topics)}")
    return topics


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
