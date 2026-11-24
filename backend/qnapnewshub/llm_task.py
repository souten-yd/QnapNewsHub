"""LLM を 1 回呼ぶ共通の入口（イベントの要約・今日の傾向の解説）。下ごしらえ（prep.py）と同じモデルの順番（llm_models）を使う。

- ControlDeck のゲートウェイが先（設定 llm.gateway.first）・つながらなければ OpenRouter。使えなかったとき、待っている間に次のモデルへ切り替わっていれば、そのモデルで 1 回やり直す。
- 用途（purpose）ごとの割り当て・1 日の上限・混み合いの待ちは llm.complete が掛ける。ここでは足さない。
- 記事の本文・題は信用しないデータ。道具（tools）は渡さない。返事は JSON だけ。
"""
from __future__ import annotations

import json
import re

from qnapnewshub import llm, llm_models

_FENCE = re.compile(r"```(?:json)?", re.IGNORECASE)


def json_of(text: str) -> object:
    """返事から JSON を取り出す（コードの囲み・前後の文を除く）。読めなければ ValueError。"""
    body = _FENCE.sub("", text or "").strip()
    brace, bracket = body.find("{"), body.find("[")
    if bracket >= 0 and (brace < 0 or bracket < brace):
        start, end = bracket, body.rfind("]")
    else:
        start, end = brace, body.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("JSON がありません")
    return json.loads(body[start:end + 1])


async def ask(cfg: dict, purpose: str, system: str, prompt: str, *, max_tokens: int) -> tuple[str, str]:
    """(返事の文, 使ったモデル)。使えなければ llm の例外（LlmNotAllowed・LlmDailyLimit・LlmUnavailable）。"""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    tried: list[str] = []
    while True:
        chain = llm_models.build_chain(cfg)
        model = llm_models.current(chain)
        tried.append(model)
        try:
            text = await llm.complete(model, messages, max_tokens=max_tokens, temperature=0.2, purpose=purpose, abandon=lambda m=model: llm_models.switched_away(m))
            return text, model
        except llm.LlmDailyLimit:
            raise
        except llm.LlmUnavailable:
            nxt = llm_models.current(llm_models.build_chain(cfg))
            if nxt == model or nxt in tried:
                raise
