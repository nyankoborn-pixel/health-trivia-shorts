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

# YouTube 説明欄先頭に付与する一般注意書き。健康以外の話題も扱うので汎用文言。
DISCLAIMER_DESC_PREFIX = (
    "※本動画は一般的な情報の紹介です。"
    "健康・医療に関する内容については、専門家にご相談ください。\n\n"
)

SYSTEM_PROMPT = """あなたは中高年向け雑学 YouTube 動画の台本生成 AI です。
日本の中高年視聴者向けに、与えられたトピック候補から 5〜6 個を選び、各 30 秒前後で話せるシーン台本を作成してください。

【テーマの範囲】
健康、生活、食、習慣、知恵、文化、歴史、季節、ランキングなど中高年が興味を持ちそうな雑学。健康トピックばかりに偏らず、幅広く採用する。

【厳守事項】
1. 与えられたトピックの body に書かれた事実のみ使用し、新しい事実・数字・固有名詞を捏造しないこと
2. 健康・医療系の話題では「〜と言われています」「〜の可能性があります」など断定を避ける表現を使う。治療効果の断定は禁止
3. ナレーション (text) は 130〜170 字。subtitles は text を分割した字幕配列
4. 各シーンは独立して理解できる単発雑学として完結させる
5. 「9 割の人が知らない」のような煽り見出しは避け、落ち着いた知的トーン

【字幕 (subtitles) の作り方】★最重要★
- text を「、」「。」や意味の区切りで自然に分割した文字列配列
- **各要素は 14 字以内**（必ず守る。15 字以上は禁止）
- 連結すると text と同じ内容になるよう構成
- 例: text="朝日を浴びると体内時計がリセットされ、夜の睡眠の質が高まると言われています。"
  → subtitles=["朝日を浴びると", "体内時計がリセットされ", "夜の睡眠の質が高まると", "言われています"]
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
      "subtitles": ["14字以内の字幕1", "14字以内の字幕2", "...8〜12個"],
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
    n = len(topics)
    if n >= 6:
        instruction = "以下のトピック候補から 5〜6 個を選び、台本 JSON を生成してください。"
    elif n == 5:
        instruction = "以下の 5 トピックすべてを使って 5 シーンの台本 JSON を生成してください。"
    else:  # 3 or 4
        instruction = f"以下の {n} トピックすべてを使って {n} シーンの台本 JSON を生成してください。各シーンで尺をやや長め（40〜45 秒）に取って 3 分前後に収めること。"

    return f"""{instruction}

{topics_block}

選定基準: 視聴者の興味を引き、画像で表現しやすく、医療誤情報リスクの低いもの。
"""


def _call_gemini_for_script(client, topics: list[dict]):
    """Gemini を 1 回呼び出してレスポンスを返す。"""
    return call_with_retry(
        lambda: client.models.generate_content(
            model=MODEL,
            contents=build_user_prompt(topics),
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                temperature=0.7,
                top_p=0.95,
                max_output_tokens=16384,
            ),
        ),
        label="generate_script",
    )


def _parse_script_json(text: str) -> dict:
    """サニタイズしつつ JSON を読む。読めなければ例外を上に伝える。"""
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


def generate_script(topics: list[dict]) -> dict:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")

    client = genai.Client(api_key=api_key)

    print(f"[script] generating with {len(topics)} candidate topics ({MODEL})")
    parsed = None
    last_err: Exception | None = None
    for attempt in range(2):
        response = _call_gemini_for_script(client, topics)
        text = (response.text or "").strip()
        # raw レスポンス保存（attempt ごとに別ファイル）
        (WORK_DIR / f"_script_raw_{attempt}.txt").write_text(text, encoding="utf-8")
        print(f"[script] attempt {attempt + 1} response: {len(text)} chars")
        if not text:
            try:
                cand = response.candidates[0] if response.candidates else None
                finish = getattr(cand, "finish_reason", "unknown") if cand else "no-candidate"
            except Exception:
                finish = "unknown"
            last_err = RuntimeError(f"Gemini returned empty response (finish_reason={finish})")
            if attempt == 0:
                print("[script] empty response; retrying")
                continue
            raise last_err
        try:
            parsed = _parse_script_json(text)
            break
        except json.JSONDecodeError as e:
            last_err = e
            print(f"[script] JSON parse failed at attempt {attempt + 1}: {e}")
            if attempt == 0:
                print("[script] retrying with fresh generation")
                continue
            raise

    if parsed is None:
        raise RuntimeError(f"generate_script failed after retries: {last_err}")

    # validate 前にパース結果を dump
    (WORK_DIR / "_script_parsed.json").write_text(
        json.dumps(parsed, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return parsed


def _split_text_to_subtitles(text: str, max_len: int = 14) -> list[str]:
    """text を句読点 (、 。) で区切り、max_len 字以内のチャンクに自動分割。
    subtitles が欠落していた時のフォールバック用。
    """
    import re as _re
    pieces = [p for p in _re.split(r"([、。])", text) if p]
    merged: list[str] = []
    for p in pieces:
        if p in ("、", "。") and merged:
            merged[-1] += p
        else:
            merged.append(p)
    out: list[str] = []
    for chunk in merged:
        while len(chunk) > max_len:
            out.append(chunk[:max_len])
            chunk = chunk[max_len:]
        if chunk:
            out.append(chunk)
    return out


def _normalize_subtitles(subs: list) -> list[str]:
    """字幕配列を文字列配列に統一する。dict が来た場合は text フィールドを取り出す。"""
    out: list[str] = []
    for s in subs:
        if isinstance(s, str):
            out.append(s)
        elif isinstance(s, dict) and isinstance(s.get("text"), str):
            out.append(s["text"])
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
        subs = _normalize_subtitles(subs)
        s["subtitles"] = subs

        # 各 subtitle の長さチェック（>16 字は警告のみ）
        for j, sub in enumerate(subs):
            if not sub.strip():
                raise RuntimeError(f"scene {i} subtitle[{j}] empty")
            if len(sub) > 16:
                print(f"  [warn] scene {i} subtitle[{j}] too long ({len(sub)}字): {sub}")

        for word in forbidden:
            if word in s["text"]:
                raise RuntimeError(f"scene {i} text contains forbidden word '{word}'")
            for sub in subs:
                if word in sub:
                    raise RuntimeError(f"scene {i} subtitle contains forbidden word '{word}'")

        # image_keywords が無い/空なら subtitles[0] から推測
        kws = s.get("image_keywords")
        if not isinstance(kws, list) or len(kws) == 0:
            print(f"  [warn] scene {i} missing image_keywords, deriving from subtitles[0]")
            s["image_keywords"] = [subs[0][:6]]

    for field in ("title", "description"):
        if field in script:
            for word in forbidden:
                if word in script[field]:
                    raise RuntimeError(f"{field} contains forbidden word '{word}'")

    return script


def prepend_description_disclaimer(script: dict) -> dict:
    """YouTube 説明欄先頭に一般注意書きを付与する。
    本編シーンには追加しない（概要欄記載で十分・Boss 指示）。
    """
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
    script = prepend_description_disclaimer(script)

    SCRIPT_OUT.write_text(
        json.dumps(script, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[script] title: {script['title']}")
    print(f"[script] scenes: {len(script['scenes'])}")
    print(f"[script] saved: {SCRIPT_OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
