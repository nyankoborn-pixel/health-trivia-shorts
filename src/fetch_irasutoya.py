"""
fetch_irasutoya.py

work/script.json の各シーンの image_keywords を順にいらすとや内検索し、画像をダウンロードして
work/images/scene_NN.png として配置する。

URL 規約:
- 検索: https://www.irasutoya.com/search?q=<キーワード>
- 個別記事ページ: https://www.irasutoya.com/YYYY/MM/blog-post_xxx.html
- 画像本体は記事ページ内の <a href="...png"><img src="...png"></a> 構造で取得

サイト負荷配慮:
- 各リクエスト間に最低 2 秒のスリープ
- 取得済み画像は cache/irasutoya/ に保存し、同キーワードでの再取得をスキップ

フォールバック:
- 1 キーワード目で 0 件ヒットなら 2,3 番目を試す
- 全キーワード 0 件なら、最も汎用的なキーワード単独で再検索（前後 1〜2 文字をトリム）
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import sys
import time
import traceback
import urllib.parse
from pathlib import Path

import requests
from bs4 import BeautifulSoup

WORK_DIR = Path("work")
IMAGES_DIR = WORK_DIR / "images"
IMAGES_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR = Path("cache/irasutoya")
CACHE_DIR.mkdir(parents=True, exist_ok=True)
SCRIPT_IN = WORK_DIR / "script.json"
IMAGES_OUT = WORK_DIR / "images.json"

SEARCH_URL = "https://www.irasutoya.com/search?q={q}"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36 health-trivia-shorts"
SLEEP_BETWEEN_REQUESTS = 2.0  # サイト配慮: 同期処理で 2 秒以上空ける
MAX_FALLBACK_RESULTS = 3      # 検索ページから最大 3 件を候補として保持

# disclaimer シーンが画像取得に失敗した場合の最終保険。compliance critical なシーンが
# silent skip されないよう、リポに同梱した固定イラストへフォールバックする。
DISCLAIMER_FALLBACK = Path("assets/fallback/disclaimer.png")

# PNG / JPEG マジックバイト。404 HTML を画像として保存してしまうのを防ぐ
_IMG_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")


def polite_sleep():
    time.sleep(SLEEP_BETWEEN_REQUESTS + random.uniform(0, 0.5))


def fetch(url: str) -> str | None:
    """HTML を取得。失敗時は None を返し、例外で全パイプラインを落とさない。"""
    print(f"  [fetch] {url}")
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"  [error] fetch failed: {type(e).__name__}: {e}")
        return None


def search_keyword(keyword: str) -> list[str]:
    """検索結果ページから記事 URL を最大 MAX_FALLBACK_RESULTS 件取得。失敗時は空リスト。"""
    url = SEARCH_URL.format(q=urllib.parse.quote(keyword))
    html = fetch(url)
    polite_sleep()
    if html is None:
        return []
    try:
        soup = BeautifulSoup(html, "lxml")
        article_urls: list[str] = []
        for a in soup.select("div.boxmeta.clearfix h2 a[href]"):
            href = a.get("href")
            if href and href.startswith("https://www.irasutoya.com/") and ".html" in href:
                if href in article_urls:
                    continue
                article_urls.append(href)
                if len(article_urls) >= MAX_FALLBACK_RESULTS:
                    break
        return article_urls
    except Exception as e:
        print(f"  [error] search_keyword parse failed: {type(e).__name__}: {e}")
        return []


def extract_image_url(article_url: str) -> str | None:
    """記事ページから画像本体 URL を抽出。失敗時は None。"""
    html = fetch(article_url)
    polite_sleep()
    if html is None:
        return None
    try:
        soup = BeautifulSoup(html, "lxml")
        post_body = soup.select_one("div.entry, div.post-body, div.entry-content")
        if not post_body:
            post_body = soup
        for a in post_body.select("a[href]"):
            href = a["href"]
            if re.search(r"\.(png|jpe?g)(\?|$)", href, re.IGNORECASE):
                return href
        # fallback: 最初の大きめの img
        for img in post_body.select("img[src]"):
            src = img["src"]
            if re.search(r"\.(png|jpe?g)(\?|$)", src, re.IGNORECASE):
                return src
        print(f"  [miss] no image url found in article")
        return None
    except Exception as e:
        print(f"  [error] extract_image_url parse failed: {type(e).__name__}: {e}")
        return None


def cache_path_for(image_url: str) -> Path:
    """画像 URL → ローカルキャッシュパス。同じ URL は同じパスになる"""
    h = hashlib.sha1(image_url.encode("utf-8")).hexdigest()[:16]
    ext = ".png" if ".png" in image_url.lower() else ".jpg"
    return CACHE_DIR / f"{h}{ext}"


def download_image(image_url: str) -> Path | None:
    """画像をキャッシュへダウンロード（既にあればスキップ）。失敗時は None を返す。"""
    try:
        p = cache_path_for(image_url)
    except Exception as e:
        print(f"  [error] cache_path_for failed: {type(e).__name__}: {e}")
        return None
    if p.exists() and p.stat().st_size > 0:
        print(f"  [cached] {p.name}")
        return p
    print(f"  [dl] {image_url}")
    try:
        r = requests.get(image_url, headers={"User-Agent": USER_AGENT}, timeout=30)
        r.raise_for_status()
        data = r.content
    except Exception as e:
        print(f"  [error] download failed: {type(e).__name__}: {e}")
        polite_sleep()
        return None
    if not any(data.startswith(m) for m in _IMG_MAGIC):
        print(f"  [skip] not an image (first 8 bytes: {data[:8]!r})")
        polite_sleep()
        return None
    try:
        p.write_bytes(data)
    except Exception as e:
        print(f"  [error] write failed: {type(e).__name__}: {e}")
        polite_sleep()
        return None
    polite_sleep()
    return p


def _try_articles(article_urls: list[str], exclude: set[Path]) -> Path | None:
    """検索結果の記事 URL 群を順に試し、exclude に含まれない最初の画像を返す。
    1 記事の処理で例外が出ても次の記事に進む。
    """
    for url in article_urls:
        try:
            image_url = extract_image_url(url)
            if not image_url:
                continue
            img = download_image(image_url)
            if img is None:
                continue
            if img in exclude:
                print(f"  [dup] already used, trying next: {img.name}")
                continue
            return img
        except Exception as e:
            print(f"  [error] _try_articles for {url}: {type(e).__name__}: {e}")
            traceback.print_exc()
            continue
    return None


def find_one_image(keywords: list[str], exclude: set[Path]) -> Path | None:
    """exclude に被らない 1 枚を探す。
    キーワードを順に試し、各キーワードで検索上位 MAX_FALLBACK_RESULTS 件を確認。
    全滅なら短縮キーワードで再試行。
    """
    tried: list[str] = []
    for kw in keywords:
        kw = kw.strip()
        if not kw or kw in tried:
            continue
        tried.append(kw)
        article_urls = search_keyword(kw)
        if not article_urls:
            print(f"  [miss] no result for '{kw}'")
            continue
        img = _try_articles(article_urls, exclude)
        if img is not None:
            return img

    # フォールバック: より短いキーワードで再検索
    for kw in keywords:
        if len(kw) >= 4:
            short = kw[:2]
            if short in tried:
                continue
            tried.append(short)
            print(f"  [fallback] retry with shorter keyword '{short}'")
            article_urls = search_keyword(short)
            if article_urls:
                img = _try_articles(article_urls, exclude)
                if img is not None:
                    return img

    return None


def main() -> int:
    if not SCRIPT_IN.exists():
        print(f"ERROR: {SCRIPT_IN} not found", file=sys.stderr)
        return 1

    script = json.loads(SCRIPT_IN.read_text(encoding="utf-8"))
    scenes = script["scenes"]
    print(f"[fetch_irasutoya] fetching images for {len(scenes)} scenes")

    # いらすとや商用利用 20 点ルール対策。ユニーク取得が IMAGE_CAP に達したら
    # 以降は新規取得を停止し、既出画像を使い回す。
    IMAGE_CAP = 20
    DEFAULT_PER_SCENE = 3
    DISCLAIMER_PER_SCENE = 1
    import random as _random

    all_used: list[Path] = []  # 取得済みユニーク画像（順序保持）
    image_map: dict[str, list[str]] = {}
    missing: list[str] = []

    for i, scene in enumerate(scenes):
        sid = scene.get("id", f"{i+1:02d}")
        # 1 シーンの予期せぬ例外でパイプライン全体を殺さない（このシーンだけ missing 扱い）
        try:
            kws = scene.get("image_keywords", [])
            n_target = DISCLAIMER_PER_SCENE if sid == "disclaimer" else DEFAULT_PER_SCENE
            print(f"\n[scene {sid}] keywords: {kws} (target {n_target} images, cap={len(all_used)}/{IMAGE_CAP})")

            scene_imgs: list[Path] = []

            # === Stage 1: ユニーク画像取得（残り枠の範囲で）===
            for slot in range(n_target):
                if len(all_used) >= IMAGE_CAP:
                    print(f"  [cap] reached {IMAGE_CAP} unique images; switching to reuse mode")
                    break
                img = find_one_image(kws, exclude=set(all_used) | set(scene_imgs))
                if img is None:
                    print(f"  [miss] no fresh image found for slot {slot}")
                    break
                scene_imgs.append(img)
                all_used.append(img)
                print(f"  + slot {slot}: {img.name} (unique now {len(all_used)})")

            # === Stage 2: 不足分は既出画像から使い回し ===
            if len(scene_imgs) < n_target:
                if not all_used:
                    # 1 枚も無いとき: disclaimer は固定フォールバック
                    if sid == "disclaimer" and DISCLAIMER_FALLBACK.exists():
                        scene_imgs.append(DISCLAIMER_FALLBACK)
                        print(f"  [fallback] using bundled disclaimer image")
                while len(scene_imgs) < n_target and all_used:
                    candidates = [p for p in all_used if p not in scene_imgs]
                    if not candidates:
                        candidates = all_used
                    pick = _random.choice(candidates)
                    scene_imgs.append(pick)
                    print(f"  ↺ slot {len(scene_imgs) - 1}: reused {pick.name}")

            if not scene_imgs:
                print(f"  [WARN] no image found for scene {sid}")
                missing.append(sid)
                continue

            # シーンごとに images_dir へコピー（make_video が使うパス）
            out_paths: list[str] = []
            for j, img in enumerate(scene_imgs):
                ext = img.suffix
                dst = IMAGES_DIR / f"scene_{i:02d}_{j:02d}{ext}"
                dst.write_bytes(img.read_bytes())
                out_paths.append(str(dst.resolve()))
            image_map[sid] = out_paths
            print(f"  -> {len(out_paths)} image(s) placed")
        except Exception as e:
            print(f"  [ERROR] scene {sid} crashed: {type(e).__name__}: {e}", file=sys.stderr)
            traceback.print_exc()
            missing.append(sid)
            continue

    IMAGES_OUT.write_text(
        json.dumps({"images": image_map, "missing": missing}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n[fetch_irasutoya] {len(image_map)}/{len(scenes)} resolved, {len(missing)} missing")
    print(f"[fetch_irasutoya] saved: {IMAGES_OUT}")

    if missing and len(missing) > len(scenes) // 2:
        print(f"ERROR: too many missing images ({len(missing)}/{len(scenes)})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
