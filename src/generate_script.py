"""
generate_script.py

work/topics.json からトピック候補を読み、Gemini API で 5-6 シーン × 30 秒の台本を生成する。
各シーンには「ナレーション本文 (text)」「画面に焼き込むテロップ (telop)」「いらすとや検索キーワード (image_keywords)」を含める。

末尾シーンに医療免責定型文を必ず付与する。

出力: work/script.json
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from google import genai
from google.genai import types

WORK_DIR = Path("work")
TOPICS_IN = WORK_DIR / "topics.json"
SCRIPT_OUT = WORK_DIR / "script.json"

MODEL = "gemini-2.5-flash"

DISCLAIMER_SCENE = {
    "id": "disclaimer",
    "text": "本動画は一般的な健康雑学です。医療行為を代替するものではありません。体調にご不安のある方は医療機関にご相談ください。",
    "telop": "医療機関にご相談ください",
    "image_keywords": ["問診", "医者", "医療相談"],
}

# 動画シーンが万一描画されなくても YouTube 説明欄で免責が届くよう、
# description 先頭に必ず付与する定型文。
DISCLAIMER_DESC_PREFIX = (
    "※本動画は一般的な健康雑学であり、医療行為を代替するものではありません。"
    "体調にご不安のある方は医療機関にご相談ください。\n\n"
)

SYSTEM_PROMPT = """あなたは健康雑学 YouTube 動画の台本生成 AI です。
日本の一般視聴者向けに、与えられたトピック候補から 5〜6 個を選び、各 30 秒前後で話せるシーン台本を作成してください。

【厳守事項】
1. 与えられたトピックの body に書かれた事実のみ使用し、新しい事実・数字・固有名詞を捏造しないこと
2. 医療行為の代替や特定疾患の治療効果を断定しない。「〜と言われています」「〜の可能性があります」など断定を避ける表現を使う
3. 1 シーンのテロップ (telop) は 20 字以内、ナレーション (text) は 130〜170 字
4. 各シーンは独立して理解できる単発雑学として完結させる
5. 「9 割の人が知らない」のような煽り見出しは避け、落ち着いた知的トーン

【画像検索キーワード (image_keywords)】
各シーンの内容に合う「いらすとや」検索ワードを 2〜3 個列挙する。
- 抽象語より具体語を優先（「健康」より「体温計」「ジョギング」「リンゴ」など）
- いらすとやで実際に画像が存在しそうな語にする（イメージしやすい人物・物体・行動）

【冒頭フック】
1 シーン目は「実は、〜」「あなたは〜していませんか?」のような視聴者に語りかける問いかけ or 意外な事実で始める。

【出力形式】 JSON のみ。前後に説明文・マークダウンを付けない。
{
  "title": "55 文字以内の動画タイトル",
  "description": "YouTube 説明欄用、3〜5 行の文章。素材出典は記載しない",
  "scenes": [
    {
      "id": "01_intro",
      "text": "ナレーション本文（130〜170字）",
      "telop": "上部テロップ（20字以内）",
      "image_keywords": ["キーワード1", "キーワード2", "キーワード3"]
    },
    ...
  ]
}

【注意】「いらすとや」「irasutoya」という単語を title/description/text/telop のいずれにも含めないこと（コラボ誤解を避けるため）。"""


def build_user_prompt(topics: list[dict]) -> str:
    topics_block = "\n\n".join(
        f"【候補 {i+1}】\nタイトル: {t['title']}\n本文: {t['body']}"
        for i, t in enumerate(topics)
    )
    return f"""以下のトピック候補から 5〜6 個を選び、台本 JSON を生成してください。

{topics_block}

選定基準: 視聴者の興味を引き、画像で表現しやすく、医療誤情報リスクの低いもの。
"""


def generate_script(topics: list[dict]) -> dict:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")

    client = genai.Client(api_key=api_key)

    print(f"[script] generating with {len(topics)} candidate topics ({MODEL})")
    response = client.models.generate_content(
        model=MODEL,
        contents=build_user_prompt(topics),
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            temperature=0.7,
            top_p=0.95,
            max_output_tokens=4096,
        ),
    )

    text = (response.text or "").strip()
    (WORK_DIR / "_script_raw.txt").write_text(text, encoding="utf-8")

    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)

    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError:
        sanitized = "".join(
            " " if (ord(c) < 32 and c not in "\t\n\r") else c
            for c in text
        )
        return json.loads(sanitized, strict=False)


def validate_script(script: dict) -> dict:
    if "title" not in script or "scenes" not in script:
        raise RuntimeError(f"missing required fields: {list(script.keys())}")

    scenes = script["scenes"]
    if not isinstance(scenes, list) or not 4 <= len(scenes) <= 7:
        raise RuntimeError(f"scenes must be a list of 4-7 items, got {len(scenes)}")

    forbidden = ("いらすとや", "irasutoya", "イラストや")
    for i, s in enumerate(scenes):
        for k in ("text", "telop", "image_keywords"):
            if k not in s:
                raise RuntimeError(f"scene {i} missing '{k}': {s}")
        s.setdefault("id", f"{i+1:02d}")
        for field in ("text", "telop"):
            for word in forbidden:
                if word in s[field]:
                    raise RuntimeError(f"scene {i} {field} contains forbidden word '{word}'")
        if not isinstance(s["image_keywords"], list) or len(s["image_keywords"]) == 0:
            raise RuntimeError(f"scene {i} image_keywords must be a non-empty list")

    for field in ("title", "description"):
        if field in script:
            for word in forbidden:
                if word in script[field]:
                    raise RuntimeError(f"{field} contains forbidden word '{word}'")

    return script


def append_disclaimer(script: dict) -> dict:
    """動画末尾のシーンと、YouTube 説明欄先頭の両方に免責文言を付与する。
    動画側が画像取得失敗等で skip されても説明欄で必ず免責が届くようにする保険。
    """
    script["scenes"].append(DISCLAIMER_SCENE)
    existing_desc = script.get("description", "")
    script["description"] = DISCLAIMER_DESC_PREFIX + existing_desc
    return script


def main() -> int:
    if not TOPICS_IN.exists():
        print(f"ERROR: {TOPICS_IN} not found. Run collect_topics.py first.", file=sys.stderr)
        return 1

    topics = json.loads(TOPICS_IN.read_text(encoding="utf-8"))
    script = generate_script(topics)
    script = validate_script(script)
    script = append_disclaimer(script)

    SCRIPT_OUT.write_text(
        json.dumps(script, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[script] title: {script['title']}")
    print(f"[script] scenes: {len(script['scenes'])} (incl. disclaimer)")
    print(f"[script] saved: {SCRIPT_OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
