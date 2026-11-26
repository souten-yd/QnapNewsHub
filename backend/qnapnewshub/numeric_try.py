"""数値パスを 1 回だけ流して歩留まりを確かめる道具（./hub.sh numeric-try）。本物の LLM（ゲートウェイ・OpenRouter）の評価用。llm_try.py と同じく何も書かない。

- 記事は NEWSHUB_DATA の記事の DB の新しい順（取り出し済みの記事。読むだけ。num_v も見ない・書かない・回数の台帳にも数えない・設定の書き込みもしない）。
- 鍵: 環境変数 NEWSHUB_LLM_TRY_KEY、無ければ置き場の secretbox（openrouter_key・gateway_key）から読む。どちらも出力・例外に出さない。ゲートウェイの URL は --base、無ければ設定の値。
- 先に構造化出力（json_schema）で呼び、断られたら（HTTP 400・422）普通の JSON で 1 回やり直す。どちらで通ったかを出す。
- 出力: 候補の文のある記事の数・LLM が返した数値・検査を通った数値（落ちた理由別）・条件の欄ごとの埋まり方（規則だけ／LLM で増えた／どちらも無い）。
"""
from __future__ import annotations

import json
import re
import os
import time

from qnapnewshub import bench, llm, numeric, settings


_BENCH = re.compile(r"tok(?:en)?s?/s|t/s\b|tokens per second|\bpp\d{3}\b|\btg\d{3}\b|llama-bench|prefill|decode speed|生成速度", re.I)


def load_articles(count: int, scan: int = 600, *, bench: bool = False) -> tuple[list[dict], dict]:
    """新しい順の記事 count 件を読み、候補の文のある記事の入力を作る（本文があれば全文から）。
    bench=True: 本文が保存されていて、ベンチマークの語（tok/s・pp512・prefill など）が載る記事だけを、新しい順に count 件（条件の結び付けを確かめる用）。"""
    from sqlalchemy import select

    from qnapnewshub import db as news_db
    from qnapnewshub import store
    from qnapnewshub.db import NewsItem

    with news_db.session() as db:
        rows = db.scalars(select(NewsItem).where(NewsItem.kind != "event", NewsItem.extract_v >= 1).order_by(NewsItem.published_at.desc()).limit(5000 if bench else min(count, scan))).all()
        items = [store.item_dict(r) for r in rows]
    bodies = numeric.full_bodies([i["id"] for i in items])
    if bench:
        items = [i for i in items if i["id"] in bodies and _BENCH.search(bodies[i["id"]])][:count]
        bodies = {i["id"]: bodies[i["id"]] for i in items}
    arts = []
    for i in items:
        got = numeric.candidates_for(i["title"], numeric.article_text(i["title"], i.get("summary") or "", bodies.get(i["id"], "")))
        if got:
            arts.append({"id": i["id"], "title": i["title"], "source_name": i["source"], "body": i["id"] in bodies, **got})
    return arts, {"read": len(items), "with_body": len(bodies), "with_candidates": len(arts)}


def _secret(provider: str) -> str:
    from qnapnewshub import secretbox

    name = llm.GATEWAY_KEY_NAME if provider == "gateway" else llm.KEY_NAME
    try:
        return (secretbox.get_secret(name) or "") if secretbox.has_secret(name) else ""
    except secretbox.SecretError:
        return ""


async def _post(provider: str, base: str, model: str, key: str, messages: list[dict], with_schema: bool) -> tuple[int, str]:
    body = {"model": model, "messages": messages, "max_tokens": numeric.max_tokens(numeric.NUMERIC_BATCH), "temperature": 0.2}
    if with_schema:
        body["response_format"] = numeric.response_format()
    if provider == "gateway":
        return await llm._post_gateway(base, body, key, 300)  # noqa: SLF001
    body["reasoning"] = {"enabled": False}
    return await llm._post(body, key)  # noqa: SLF001


def _replay_path(part: list[dict]):
    """環境変数 NUMERIC_TRY_REPLAY（置き場）があれば、記事の組ごとの返事の保存先。無ければ None。
    1 回目は保存、2 回目からは再生する。LLM の返事は回ごとにぶれるので、検査の版どうしは同じ返事で比べる（2026-11-26）。"""
    import hashlib
    from pathlib import Path

    root = os.environ.get("NUMERIC_TRY_REPLAY", "").strip()
    if not root:
        return None
    Path(root).mkdir(parents=True, exist_ok=True)
    return Path(root) / (hashlib.sha256(",".join(str(a["id"]) for a in part).encode()).hexdigest()[:16] + ".json")


async def try_once(provider: str, base: str, model: str, key: str, arts: list[dict]) -> dict:
    """候補のある記事を NUMERIC_BATCH 件ずつ送る。{"calls", "seconds", "mode", "returned", "passed", "dropped", "fields", "samples", "error"}。"""
    started = time.monotonic()
    out = {"calls": 0, "mode": "", "returned": 0, "passed": 0, "dropped": {}, "unreadable": 0, "error": "", "samples": [], "arts": len(arts),
           "fields": {k: {"rule_only": 0, "llm_added": 0, "both_same": 0, "none": 0} for k in bench.COND_KEYS}}
    queue = [arts[i:i + numeric.NUMERIC_BATCH] for i in range(0, len(arts), numeric.NUMERIC_BATCH)]
    while queue:
        part = queue.pop(0)
        messages = [{"role": "system", "content": numeric.SYSTEM}, {"role": "user", "content": numeric.build_prompt(part)}]
        replay = _replay_path(part)
        try:
            if replay and replay.exists():      # 保存した返事を再生（同じ返事で検査の版を比べる）
                status, text = 200, replay.read_text(encoding="utf-8")
                out["mode"] = out["mode"] or "replay"
            else:
                status, text = await _post(provider, base, model, key, messages, out["mode"] != "plain")
            if status in (400, 422) and out["mode"] != "plain":
                out["mode"] = "plain"      # 構造化出力を断られた → 普通の JSON で
                status, text = await _post(provider, base, model, key, messages, False)
            elif not out["mode"] and status < 400:
                out["mode"] = "json_schema"
            if status >= 400:
                raise llm.LlmUnavailable(llm._error_message(status, text))  # noqa: SLF001
            if replay and not replay.exists():
                replay.write_text(text, encoding="utf-8")
            content = llm._content_from(f"gateway:{model}" if provider == "gateway" else model, text)  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            if numeric.is_truncated(exc) and len(part) > 1:      # 本番（numeric.ask）と同じく半分に分けて呼び直す
                half = len(part) // 2
                queue[:0] = [part[:half], part[half:]]
                out["split"] = out.get("split", 0) + 1
                continue
            msg = str(exc).replace(key, "[鍵]") if key else str(exc)
            out["error"] = msg
            break
        out["calls"] += 1
        try:
            got, stats = numeric.parse(content, part)
        except ValueError:
            out["unreadable"] += 1
            continue
        out["returned"] += stats["returned"]
        out["passed"] += stats["passed"]
        for k, v in stats["dropped"].items():
            out["dropped"][k] = out["dropped"].get(k, 0) + v
        for n, items in got.items():
            for it in items:
                rule, llm_c = it["rule_cond"], it["llm_cond"]
                for k in bench.COND_KEYS:
                    f = out["fields"][k]
                    if k in rule and k in llm_c:
                        f["both_same"] += 1
                    elif k in rule:
                        f["rule_only"] += 1
                    elif k in llm_c:
                        f["llm_added"] += 1
                    else:
                        f["none"] += 1
                if len(out["samples"]) < 200:
                    out["samples"].append({"title": part[n - 1]["title"][:60], "about": it["about"], "metric": it["metric"], "value": it["value"], "unit": it["unit"],
                                           "cond": numeric.merge_cond(rule, llm_c), "tier": it["tier"]})
    out["seconds"] = round(time.monotonic() - started, 1)
    return out


def report(info: dict, result: dict) -> str:
    lines = [f"読んだ記事 {info['read']} 件（本文あり {info['with_body']}）/ 候補の文のある記事 {info['with_candidates']} 件 / 呼び出し {result['calls']} 回・{result.get('seconds', 0)} 秒 / 方式 {result['mode'] or '-'}"]
    if result["error"]:
        lines.append(f"失敗: {result['error']}")
    if result["unreadable"]:
        lines.append(f"読めなかった返事: {result['unreadable']} 回")
    lines.append(f"LLM が返した数値 {result['returned']} 個 → 検査を通った数値 {result['passed']} 個（落ちた理由: {json.dumps(result['dropped'], ensure_ascii=False) or '{}'}。form=形・quote=引用が渡した文に無い・value=値が渡した文に無い）")
    lines.append("条件の欄ごと（通った数値に対して）: 規則だけ / LLM で増えた / 規則と LLM の両方 / どちらも無し")
    for k in bench.COND_KEYS:
        f = result["fields"][k]
        lines.append(f"  {k:8s} {f['rule_only']:4d} / {f['llm_added']:4d} / {f['both_same']:4d} / {f['none']:4d}")
    # 条件の付いた数値を先に（確かめたいのはそちら）。全部で 30 個まで
    for s in sorted(result["samples"], key=lambda s: not s["cond"])[:30]:
        lines.append(f"  - {s['title']} | {s['about']} {s['metric']} {s['value']}{s['unit']} | {s['cond'] or '条件なし'} | {s['tier']}")
    return "\n".join(lines)


def run(provider: str, base: str, model: str, count: int, *, bench: bool = False) -> int:
    import asyncio

    key = os.environ.get("NEWSHUB_LLM_TRY_KEY", "").strip() or _secret(provider)
    if provider == "gateway":
        base = settings.gateway_url(base) or settings.gateway_url(settings.load()["llm"]["gateway"]["base_url"])
        if not base:
            print("--base にゲートウェイの URL を入れてください")
            return 2
    elif not key:
        print("OpenRouter の鍵を環境変数 NEWSHUB_LLM_TRY_KEY に入れてください（出力には出しません）")
        return 2
    arts, info = load_articles(max(1, min(count, 600)), bench=bench)
    if not arts:
        print(f"候補の文のある記事がありません（読んだ記事 {info['read']} 件）")
        return 2
    result = asyncio.run(try_once(provider, base, model, key, arts))
    print(report(info, result))
    print(json.dumps({"summary": {**info, **{k: v for k, v in result.items() if k != "samples"}}}, ensure_ascii=False))
    return 1 if result["error"] else 0
