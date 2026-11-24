"""取り出しを 1 回だけ流して確かめる道具（./hub.sh llm-try）。本物の LLM（ゲートウェイ・OpenRouter）の評価用。

- 送る記事は、データの置き場（NEWSHUB_DATA）の記事の DB の新しい順の N 件（読むだけ。何も書かない・回数の台帳にも数えない・設定の鍵にも触れない）。
- 鍵は環境変数 NEWSHUB_LLM_TRY_KEY だけから読む（画面・出力・例外に出さない。ゲートウェイで鍵が要らなければ未設定でよい）。
- 出力: 送った件数・かかった時間・返事の JSON を検査した結果（読めた記事・概要が日本語の記事・取り出し済みの記事）・物と facts の一覧。
- 行き先の URL は引数の値だけ（記事の中の URL からは呼ばない）。
"""
from __future__ import annotations

import json
import os
import time

from qnapnewshub import extract, llm, prep, settings


def load_items(count: int) -> list[dict]:
    from sqlalchemy import select

    from qnapnewshub import db as news_db
    from qnapnewshub import store
    from qnapnewshub.db import NewsItem

    with news_db.session() as db:
        rows = db.scalars(select(NewsItem).where(NewsItem.kind != "event").order_by(NewsItem.published_at.desc()).limit(count)).all()
        return [store.item_dict(r) for r in rows]


async def try_once(provider: str, base: str, model: str, key: str, items: list[dict], *, timeout: int = 300) -> dict:
    """1 回だけ呼び、検査の結果を返す。LLM が使えなければ {"error": ...}。"""
    messages = [{"role": "system", "content": prep.SYSTEM}, {"role": "user", "content": prep.build_prompt(items)}]
    started = time.monotonic()
    try:
        if provider == "gateway":
            base = settings.gateway_url(base)
            if not base:
                return {"sent": len(items), "seconds": 0.0, "error": "--base にゲートウェイの URL を入れてください"}
            body = {"model": model, "messages": messages, "max_tokens": 6000, "temperature": 0.2}
            status, text = await llm._post_gateway(base, body, key, timeout)  # noqa: SLF001
        else:
            body = {"model": model, "messages": messages, "max_tokens": 6000, "temperature": 0.2, "reasoning": {"enabled": False}}
            status, text = await llm._post(body, key)  # noqa: SLF001
        if status >= 400:
            raise llm.LlmUnavailable(llm._error_message(status, text))  # noqa: SLF001
        content = llm._content_from(f"gateway:{model}" if provider == "gateway" else model, text)  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001
        message = str(exc).replace(key, "[鍵]") if key else str(exc)
        return {"sent": len(items), "seconds": round(time.monotonic() - started, 1), "error": message}
    seconds = round(time.monotonic() - started, 1)
    try:
        got = prep.parse(content, len(items), [prep.source_text(i) for i in items])
        parsed = True
    except ValueError:
        got, parsed = {}, False
    ents: list[dict] = []
    for n, rec in sorted(got.items()):
        if rec["extract"]:
            ents.append({"n": n, "title": items[n - 1]["title"][:80], "entities": rec["extract"]["entities"], "facts": rec["extract"]["facts"]})
    return {"sent": len(items), "seconds": seconds, "error": "", "json_ok": parsed, "returned": len(got),
            "ja_ok": sum(1 for r in got.values() if r["ja"]), "extracted": len(ents), "results": ents,
            "entities_total": sum(len(e["entities"]) for e in ents), "facts_total": sum(len(e["facts"]) for e in ents)}


def report(result: dict) -> str:
    lines = [f"送った記事: {result['sent']} 件 / かかった時間: {result['seconds']} 秒"]
    if result.get("error"):
        lines.append(f"失敗: {result['error']}")
        return "\n".join(lines)
    lines.append(f"返事の JSON: {'読めた' if result['json_ok'] else '読めなかった'} / 記事の返事 {result['returned']} 件 / 概要が日本語 {result['ja_ok']} 件 / 取り出し済み {result['extracted']} 件")
    lines.append(f"物 {result['entities_total']} 個・facts {result['facts_total']} 個（原文に無い数字の fact は検査で捨て済み）")
    for r in result["results"]:
        lines.append(f"\n[{r['n']}] {r['title']}")
        for e in r["entities"]:
            lines.append(f"  物: {e['name']}（{e['type']}）")
        for f in r["facts"]:
            lines.append(f"  fact: {f['about']} / {f['kind']} / {f['text']}" + (f" / 値 {f['value']}" if f["value"] else ""))
    return "\n".join(lines)


def run(provider: str, base: str, model: str, count: int) -> int:
    import asyncio

    key = os.environ.get("NEWSHUB_LLM_TRY_KEY", "").strip()
    if provider == "openrouter" and not key:
        print("OpenRouter の鍵を環境変数 NEWSHUB_LLM_TRY_KEY に入れてください（出力には出しません）")
        return 2
    items = load_items(max(1, min(count, 30)))
    if not items:
        print("記事がありません（NEWSHUB_DATA の記事の DB が空です）")
        return 2
    result = asyncio.run(try_once(provider, base, model, key, items))
    print(report(result))
    print(json.dumps({"summary": {k: v for k, v in result.items() if k != "results"}}, ensure_ascii=False))
    return 1 if result.get("error") else 0
