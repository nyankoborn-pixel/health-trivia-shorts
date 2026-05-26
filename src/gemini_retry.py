"""
gemini_retry.py

Gemini API 呼び出しの共通リトライヘルパー。
- 503 UNAVAILABLE / 429 RESOURCE_EXHAUSTED / 500 INTERNAL 等のサーバ側障害を
  最大 5 回まで指数バックオフ (5, 10, 20, 40, 80 秒) でリトライする
- 4xx 系（400/401/403/404）はクライアント側の問題なので即座に raise
"""
from __future__ import annotations

import time
from typing import Callable, TypeVar

T = TypeVar("T")

# 一時的なサーバ障害として扱うステータス語
_RETRYABLE_TOKENS = (
    "503", "UNAVAILABLE",
    "429", "RESOURCE_EXHAUSTED",
    "500", "INTERNAL",
    "deadline",  # DEADLINE_EXCEEDED
)


def call_with_retry(fn: Callable[[], T], *, max_retries: int = 5, label: str = "gemini") -> T:
    """fn() を呼び、リトライ可能なエラーなら指数バックオフで再試行する。"""
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            msg = str(e)
            retryable = any(tok in msg for tok in _RETRYABLE_TOKENS)
            if attempt == max_retries - 1 or not retryable:
                raise
            wait = 5 * (2 ** attempt)  # 5, 10, 20, 40, 80
            short_msg = msg[:140].replace("\n", " ")
            print(f"  [{label}-retry {attempt + 1}/{max_retries}] {type(e).__name__}: {short_msg}; waiting {wait}s")
            time.sleep(wait)
    # 到達不可（最後の attempt で raise されるため）だが型のため
    raise RuntimeError(f"{label}: retries exhausted")
