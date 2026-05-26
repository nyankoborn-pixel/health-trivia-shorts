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


def polite_sleep():
    time.sleep(SLEEP_BETWEEN_REQUESTS + random.uniform(0, 0.5))


def fetch(url: str) -> str:
    """HTML を取得"""
    print(f"  [fetch] {url}")
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
    r.raise_for_status()
    return r.text


def search_keyword(keyword: str) -> list[str]:
    """検索結果ページから記事 URL を最大 MAX_FALLBACK_RESULTS 件取得"""
    url = SEARCH_URL.format(q=urllib.parse.quote(keyword))
    html = fetch(url)
    polite_sleep()
    soup = BeautifulSoup(html, "lxml")

    # 記事カードのリンク: <div class="boxmeta clearfix"><h2><a href="..."></a></h2></div>
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


def extract_image_url(article_url: str) -> str | None:
    """記事ページから画像本体 URL を抽出"""
    html = fetch(article_url)
    polite_sleep()
    soup = BeautifulSoup(html, "lxml")

    # 投稿本文の中の <a href="...png"><img src="..."></a> の a タグから本体 URL
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
    return None


def cache_path_for(image_url: str) -> Path:
    """画像 URL → ローカルキャッシュパス。同じ URL は同じパスになる"""
    h = hashlib.sha1(image_url.encode("utf-8")).hexdigest()[:16]
    ext = ".png" if ".png" in image_url.lower() else ".jpg"
    return CACHE_DIR / f"{h}{ext}"


def download_image(image_url: str) -> Path:
    """画像をキャッシュへダウンロード（既にあればスキップ）"""
    p = cache_path_for(image_url)
    if p.exists() and p.stat().st_size > 0:
        return p
    print(f"  [dl] {image_url}")
    r = requests.get(image_url, headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    p.write_bytes(r.content)
    polite_sleep()
    return p


def find_image_for_keywords(keywords: list[str]) -> Path | None:
    """キーワード列を順に試し、最初にヒットした画像をダウンロードして返す。
    全て 0 件なら、各キーワードを 2 文字以上含む短縮版で再検索する。
    """
    tried = []
    for kw in keywords:
        kw = kw.strip()
        if not kw or kw in tried:
            continue
        tried.append(kw)
        article_urls = search_keyword(kw)
        if not article_urls:
            print(f"  [miss] no result for '{kw}'")
            continue
        # 最初の記事から画像を取る
        image_url = extract_image_url(article_urls[0])
        if image_url:
            return download_image(image_url)

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
                image_url = extract_image_url(article_urls[0])
                if image_url:
                    return download_image(image_url)

    return None


def main() -> int:
    if not SCRIPT_IN.exists():
        print(f"ERROR: {SCRIPT_IN} not found", file=sys.stderr)
        return 1

    script = json.loads(SCRIPT_IN.read_text(encoding="utf-8"))
    scenes = script["scenes"]
    print(f"[fetch_irasutoya] fetching images for {len(scenes)} scenes")

    image_map: dict[str, str] = {}
    missing: list[str] = []
    for i, scene in enumerate(scenes):
        sid = scene.get("id", f"{i+1:02d}")
        kws = scene.get("image_keywords", [])
        print(f"\n[scene {sid}] keywords: {kws}")
        img_path = find_image_for_keywords(kws)
        if img_path is None:
            print(f"  [WARN] no image found for scene {sid}")
            missing.append(sid)
            continue
        # シーン用に images_dir へコピー（同じ画像でも上書きで使い回す）
        ext = img_path.suffix
        dst = IMAGES_DIR / f"scene_{i:02d}{ext}"
        dst.write_bytes(img_path.read_bytes())
        image_map[sid] = str(dst.resolve())
        print(f"  -> {dst}")

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
