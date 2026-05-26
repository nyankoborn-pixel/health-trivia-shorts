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

from src.gemini_retry import call_with_retry

WORK_DIR = Path("work")
TOPICS_IN = WORK_DIR / "topics.json"
SCRIPT_OUT = WORK_DIR / "script.json"

MODEL = "gemini-2.5-flash"

DISCLAIMER_SCENE = {
    "id": "disclaimer",
    "text": "本動画は一般的な健康雑学です。医療行為を代替するものではありません。体調にご不安のある方は、医療機関にご相談ください。",
    "subtitles": [
        {"text": "本動画は一般的な", "important": False},
        {"text": "健康雑学です", "important": False},
        {"text": "医療行為を代替する", "important": True},
        {"text": "ものではありません", "important": False},
        {"text": "体調にご不安の方は", "important": False},
        {"text": "医療機関にご相談を", "important": True},
    ],
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
3. ナレーション (text) は 130〜170 字。subtitles は text を分割した字幕配列
4. 各シーンは独立して理解できる単発雑学として完結させる
5. 「9 割の人が知らない」のような煽り見出しは避け、落ち着いた知的トーン

【字幕 (subtitles) の作り方】★最重要★
- text を「、」「。」や意味の区切りで自然に分割したオブジェクト配列
- 各要素は `{"text": "字幕本文", "important": true/false}` 形式
- text フィールドは **14 字以内**（必ず守る。15 字以上は禁止）
- important: そのシーンの結論や数字・意外性が含まれる字幕は true（強調表示）。1 シーン 8〜12 個のうち 2〜3 個程度を true
- 連結すると元の text と同じ内容になるよう構成
- 例: text="朝日を浴びると体内時計がリセットされ、夜の睡眠の質が高まると言われています。"
  → subtitles=[
      {"text": "朝日を浴びると", "important": false},
      {"text": "体内時計がリセットされ", "important": true},
      {"text": "夜の睡眠の質が高まると", "important": true},
      {"text": "言われています", "important": false}
    ]
- 1 シーン = 8〜12 個の subtitles 程度

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
      "text": "ナレーション本文（130〜170字、句点付き）",
      "subtitles": [
        {"text": "14字以内の字幕1", "important": false},
        {"text": "14字以内の字幕2", "important": true},
        "...8〜12個"
      ],
      "image_keywords": ["キーワード1", "キーワード2", "キーワード3"]
    },
    ...
  ]
}

【注意】「いらすとや」「irasutoya」という単語を title/description/text/subtitles のいずれにも含めないこと（コラボ誤解を避けるため）。"""


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
    response = call_with_retry(
        lambda: client.models.generate_content(
            model=MODEL,
            contents=build_user_prompt(topics),
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                temperature=0.7,
                top_p=0.95,
                max_output_tokens=8192,
            ),
        ),
        label="generate_script",
    )

    text = (response.text or "").strip()
    (WORK_DIR / "_script_raw.txt").write_text(text, encoding="utf-8")
    print(f"[script] response: {len(text)} chars")
    if not text:
        # safety filter / blocked / quota 切れ等
        try:
            cand = response.candidates[0] if response.candidates else None
            finish = getattr(cand, "finish_reason", "unknown") if cand else "no-candidate"
        except Exception:
            finish = "unknown"
        raise RuntimeError(f"Gemini returned empty response for script (finish_reason={finish})")

    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)

    try:
        parsed = json.loads(text, strict=False)
    except json.JSONDecodeError:
        sanitized = "".join(
            " " if (ord(c) < 32 and c not in "\t\n\r") else c
            for c in text
        )
        parsed = json.loads(sanitized, strict=False)

    # validate 前にパース結果を dump（validate でこけた時に中身を確認できる）
    (WORK_DIR / "_script_parsed.json").write_text(
        json.dumps(parsed, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return parsed


def _split_text_to_subtitles(text: str, max_len: int = 14) -> list[dict]:
    """text を句読点 (、 。) で区切り、max_len 字以内のチャンクに自動分割。
    subtitles が欠落していた時のフォールバック用。全て important=False で返す。
    """
    import re as _re
    pieces = [p for p in _re.split(r"([、。])", text) if p]
    # 区切り文字を直前のピースに付け戻す
    merged: list[str] = []
    for p in pieces:
        if p in ("、", "。") and merged:
            merged[-1] += p
        else:
            merged.append(p)
    # max_len 超過のチャンクをさらに細かく割る
    out: list[dict] = []
    for chunk in merged:
        while len(chunk) > max_len:
            out.append({"text": chunk[:max_len], "important": False})
            chunk = chunk[max_len:]
        if chunk:
            out.append({"text": chunk, "important": False})
    return out


def _normalize_subtitles(subs: list) -> list[dict]:
    """字幕配列を [{text, important}] 形式に統一する。
    LLM が文字列配列で返してきた場合や dict 形式の場合の両方に対応。
    """
    out: list[dict] = []
    for s in subs:
        if isinstance(s, str):
            out.append({"text": s, "important": False})
        elif isinstance(s, dict) and "text" in s and isinstance(s["text"], str):
            out.append({"text": s["text"], "important": bool(s.get("important", False))})
        else:
            raise RuntimeError(f"invalid subtitle entry: {s!r}")
    return out


def validate_script(script: dict) -> dict:
    if "title" not in script or "scenes" not in script:
        raise RuntimeError(f"missing required fields: keys={list(script.keys())}")

    scenes = script["scenes"]
    if not isinstance(scenes, list) or not 3 <= len(scenes) <= 8:
        raise RuntimeError(f"scenes must be a list of 3-8 items, got {len(scenes) if isinstance(scenes, list) else type(scenes)}")

    forbidden = ("いらすとや", "irasutoya", "イラストや")
    for i, s in enumerate(scenes):
        # 必須キーチェック
        if "text" not in s:
            raise RuntimeError(f"scene {i} missing 'text': scene_keys={list(s.keys())}")
        s.setdefault("id", f"{i+1:02d}")

        # subtitles 欠落時は text から自動分割
        subs = s.get("subtitles")
        if not isinstance(subs, list) or len(subs) == 0:
            print(f"  [warn] scene {i} missing subtitles, deriving from text")
            subs = _split_text_to_subtitles(s["text"], max_len=14)
        # dict 形式に正規化
        subs = _normalize_subtitles(subs)
        s["subtitles"] = subs

        # 各 subtitle の長さチェック（>16 字は警告のみ、make_video 側で折返なしで描画）
        for j, sub in enumerate(subs):
            if not sub["text"].strip():
                raise RuntimeError(f"scene {i} subtitle[{j}] empty text")
            if len(sub["text"]) > 16:
                print(f"  [warn] scene {i} subtitle[{j}] too long ({len(sub['text'])}字): {sub['text']}")

        for word in forbidden:
            if word in s["text"]:
                raise RuntimeError(f"scene {i} text contains forbidden word '{word}'")
            for sub in subs:
                if word in sub["text"]:
                    raise RuntimeError(f"scene {i} subtitle contains forbidden word '{word}'")

        # image_keywords が無い/空なら subtitles[0] から推測
        kws = s.get("image_keywords")
        if not isinstance(kws, list) or len(kws) == 0:
            print(f"  [warn] scene {i} missing image_keywords, deriving from subtitles[0]")
            s["image_keywords"] = [subs[0]["text"][:6]]

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
