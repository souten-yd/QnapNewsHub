"""仕事の列（jobs）: ControlDeck が借りて返す分析の仕事。ARCHITECTURE 5 章。

- 種類: embed（埋め込み）・event_merge（意味でのイベントのまとめ直し）・summarize_hq（重要なイベントの高品質の要約）・extract_retry（取り出しが検査に落ちた記事のやり直し）・image（画像の無い重要な記事の画像）・
  translate_hq（重要な英語の記事の高品質の翻訳。ControlDeck が借りて返す。QNAP では軽い QuickMT で訳し、高品質は ControlDeck 側。translate_flow.py）。
- **重複を防ぐ ID**（dedup_key = 種類:対象:入力の版）。同じ入力の版の仕事は 1 つだけ。入力の版を上げれば（プロンプトの版が変わったなど）新しい仕事になる。
- **借り（lease）**: claim で lease_until（既定 5 分・30〜3600 秒）まで借りる。期限が切れた仕事は、次の claim（と診断の読み）で列に戻る（試行が max_tries に達していれば dead）。
  借りたとき tries を +1。ControlDeck が途中で止まっても、列に戻って再開後に続く。
- **冪等な返し（complete）**: 終わった仕事への 2 回目の complete は何も変えず duplicate を返す。期限が切れた後でも、まだ誰も終えていなければ遅れた結果を受け取る
  （別の係が生きた借りを持っているときだけ 409 相当の lease_conflict）。
- **仕事の作り手**は claim のときに（要る種類の分だけ）作る＝ControlDeck が来ない間は何も書かない:
  extract_retry = 下ごしらえが済んで（または 3 回で諦めて）取り出しが空のままの記事（14 日以内）・summarize_hq = 出どころが 3 以上のイベント・
  image = 出どころ 3 以上のイベントの代表の記事で画像が無いもの。embed・event_merge は結果の取り込み口だけ（作り手は v0.6 以降）。
- **結果の取り込み**: 記事の中身に当たるものは必ず検査してから（extract.validate／clean＝JSON Schema の部分集合）。取り込みはまとめ書き（writer）を通し、保存してから done にする。
  embed は INT8 の表（embeddings）へ。summarize_hq はイベントの summary へ。extract_retry は物・fact へ（stage_extraction）。image は PUT /artifacts で送った生成物を確かめるだけ。
- 書き込みはすべて Write Coordinator（writer.run）を通す。
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from qnapnewshub import db as news_db
from qnapnewshub import extract, store, writer
from qnapnewshub.db import Artifact, Embedding, Event, EventArticle, Job, NewsItem

logger = logging.getLogger("qnapnewshub.jobs")

KINDS = ("embed", "event_merge", "summarize_hq", "extract_retry", "image", "translate_hq")
INPUT_V = {"embed": "e1", "event_merge": "m1", "summarize_hq": "hq1", "extract_retry": extract.PROMPT_VERSION, "image": "img1", "translate_hq": "thq1"}
LEASE_DEFAULT, LEASE_MIN, LEASE_MAX = 300, 30, 3600
CLAIM_MAX = 50
MAX_TRIES = 3
HQ_MIN_SOURCES = 3
CREATE_PER_CLAIM = 100
RETRY_DAYS = 14
SUMMARY_MAX = 1500
RESULT_MAX = 200_000
EMBED_MAX_DIM = 8192

_UTC = timezone.utc


def _now() -> datetime:
    return datetime.now(_UTC).replace(tzinfo=None)


def _iso(value: datetime | None) -> str:
    return value.replace(tzinfo=_UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if value else ""


def dedup_key(kind: str, ref_id: int, input_v: str) -> str:
    return f"{kind}:{ref_id}:{input_v}"


def enqueue(db: Session, kind: str, ref_id: int, input_v: str | None = None) -> bool:
    """仕事を 1 つ列に入れる（同じ重複 ID があれば入れない）。入れたら True。書き手のスレッドで。"""
    if kind not in KINDS:
        raise ValueError(f"仕事の種類が正しくありません: {kind}")
    v = input_v or INPUT_V[kind]
    res = db.execute(sqlite_insert(Job).values(kind=kind, ref_id=ref_id, input_v=v, dedup_key=dedup_key(kind, ref_id, v), status="queued", tries=0, max_tries=MAX_TRIES,
                                               created_at=_now(), updated_at=_now()).on_conflict_do_nothing(index_elements=["dedup_key"]))
    return bool(res.rowcount)


# ---- 作り手 ----

def _has_job(kind: str, v: str):
    return select(Job.id).where(Job.kind == kind, Job.input_v == v, Job.ref_id == NewsItem.id).exists() if kind != "summarize_hq" else \
        select(Job.id).where(Job.kind == kind, Job.input_v == v, Job.ref_id == Event.id).exists()


def create_extract_retry(db: Session, limit: int = CREATE_PER_CLAIM) -> int:
    from qnapnewshub import prep

    since = _now() - timedelta(days=RETRY_DAYS)
    v = INPUT_V["extract_retry"]
    ids = db.scalars(select(NewsItem.id).where(NewsItem.prep_tries >= prep.MAX_TRIES, NewsItem.extract_v == 0, NewsItem.kind != "event", NewsItem.dup_of.is_(None),
                                               NewsItem.published_at >= since, ~_has_job("extract_retry", v)).order_by(NewsItem.published_at.desc()).limit(limit)).all()
    return sum(enqueue(db, "extract_retry", i, v) for i in ids)


def create_summarize_hq(db: Session, limit: int = CREATE_PER_CLAIM) -> int:
    v = INPUT_V["summarize_hq"]
    ids = db.scalars(select(Event.id).where(Event.n_sources >= HQ_MIN_SOURCES, ~_has_job("summarize_hq", v)).order_by(Event.last_at.desc()).limit(limit)).all()
    return sum(enqueue(db, "summarize_hq", i, v) for i in ids)


def create_image(db: Session, limit: int = CREATE_PER_CLAIM) -> int:
    v = INPUT_V["image"]
    ids = db.scalars(select(NewsItem.id).join(Event, Event.rep_id == NewsItem.id).where(Event.n_sources >= HQ_MIN_SOURCES, NewsItem.kind.not_in(("release", "event")),
                                                                                         (NewsItem.image_url.is_(None)) | (NewsItem.image_url == ""), ~_has_job("image", v))
                     .order_by(Event.last_at.desc()).limit(limit)).all()
    return sum(enqueue(db, "image", i, v) for i in ids)


def create_translate_hq(db: Session, limit: int = CREATE_PER_CLAIM) -> int:
    """重要な英語の記事（チャンネルに合う・監視のThings・ホットトピック。本文を取り出せたもの）の高品質の翻訳の仕事。設定 translate.hq_jobs が切なら作らない。"""
    from qnapnewshub import settings, translate_flow

    if not settings.load()["translate"]["hq_jobs"]:
        return 0
    v = INPUT_V["translate_hq"]
    return sum(enqueue(db, "translate_hq", i, v) for i in translate_flow.hq_candidates(db, limit, v))


_CREATORS = {"extract_retry": create_extract_retry, "summarize_hq": create_summarize_hq, "image": create_image, "translate_hq": create_translate_hq}


# ---- 借りる ----

def requeue_expired(db: Session, now: datetime | None = None) -> int:
    """期限切れの借りを列へ戻す（試行が上限なら dead）。戻した数。書き手のスレッドで。"""
    now = now or _now()
    cond = (Job.status == "leased", Job.lease_until < now)
    dead = db.execute(update(Job).where(*cond, Job.tries >= Job.max_tries).values(status="dead", error="借りの期限が切れ、試行の上限に達しました", updated_at=now)).rowcount or 0
    back = db.execute(update(Job).where(*cond, Job.tries < Job.max_tries).values(status="queued", lease_until=None, lease_by=0, updated_at=now)).rowcount or 0
    return int(back) + int(dead)


def _payload(db: Session, job: Job) -> dict:
    """ControlDeck へ渡す入力（記事の中身は「データ」。命令として扱わせない）。"""
    if job.kind == "summarize_hq" or job.kind == "event_merge":
        ev = db.get(Event, job.ref_id)
        if ev is None:
            return {"missing": True}
        from qnapnewshub import events

        return {"event": {"id": ev.id, "title": ev.title, "n_articles": ev.n_articles, "n_sources": ev.n_sources}, "articles": events.summary_inputs(db, ev, max_articles=12)}
    row = db.get(NewsItem, job.ref_id)
    if row is None:
        return {"missing": True}
    if job.kind == "translate_hq":
        from qnapnewshub import settings, translate_flow

        return {"item": {"id": row.id, "title": row.title, "summary": row.summary or "", "source": row.source, "url": row.url, "published_at": store.iso(row.published_at),
                         "body": translate_flow.body_text(db, row.id, settings.load()["translate"]["body_max_chars"])}, "kind": "item", "target_lang": "ja"} if row else {"missing": True}
    base = {"id": row.id, "title": row.title, "summary": row.summary or "", "summary_ja": row.summary_ja or "", "source": row.source, "url": row.url,
            "published_at": store.iso(row.published_at)}
    if job.kind == "image":
        base["img_prompt"] = row.img_prompt or ""
    return {"item": base, "kind": "item"}


def claim(token_id: int, kinds: list[str] | None, limit: int, lease_seconds: int) -> dict:
    """仕事を借りる。{"jobs": [{id, kind, ref_id, input_v, attempt, lease_until, input}], "lease_seconds"}。"""
    kinds = list(kinds or KINDS)
    if any(k not in KINDS for k in kinds):
        raise ValueError("仕事の種類が正しくありません")
    limit = max(1, min(int(limit), CLAIM_MAX))
    lease = max(LEASE_MIN, min(int(lease_seconds), LEASE_MAX))

    def do() -> list[dict]:
        out: list[dict] = []
        with news_db.session() as db:
            now = _now()
            requeue_expired(db, now)
            for kind in kinds:
                if kind in _CREATORS:
                    _CREATORS[kind](db)
            rows = db.scalars(select(Job).where(Job.status == "queued", Job.kind.in_(kinds)).order_by(Job.id).limit(limit)).all()
            for job in rows:
                data = _payload(db, job)
                if data.get("missing"):      # 対象が消えた（記事の削除・イベントの解消）
                    job.status, job.error, job.updated_at = "done", "対象が無くなりました", now
                    continue
                job.status, job.lease_by, job.lease_until, job.tries, job.updated_at = "leased", token_id, now + timedelta(seconds=lease), job.tries + 1, now
                out.append({"id": job.id, "kind": job.kind, "ref_id": job.ref_id, "input_v": job.input_v, "attempt": job.tries, "max_tries": job.max_tries,
                            "lease_until": _iso(job.lease_until), "input": data})
            db.commit()
        return out
    return {"jobs": writer.run("news.jobs.claim", do, user=False), "lease_seconds": lease}


# ---- 返す ----

class Rejected(Exception):
    """結果が検査に落ちた（理由は利用者に見せてよい短い文）。"""


def _decode_vec(res: dict) -> tuple[str, int, float, bytes]:
    model = str(res.get("model") or "")[:80]
    dim = res.get("dim")
    scale = res.get("scale")
    try:
        raw = base64.b64decode(str(res.get("vec_b64", "")), validate=True)
    except (binascii.Error, ValueError):
        raise Rejected("vec_b64 が base64 ではありません") from None
    if not model or not isinstance(dim, int) or not 8 <= dim <= EMBED_MAX_DIM or len(raw) != dim:
        raise Rejected("埋め込みは model・dim（8〜8192）・INT8 の dim バイトです")
    if not isinstance(scale, (int, float)) or isinstance(scale, bool) or not 0 < float(scale) < 1e6:
        raise Rejected("scale が正しくありません")
    return model, dim, float(scale), raw


def _apply(db: Session, job: Job, result: object, model: str) -> None:
    """結果を検査して取り込む（書き手のスレッドで。まとめ書きに積む分はあとで flush）。落ちたら Rejected。"""
    if not isinstance(result, dict):
        raise Rejected("結果は JSON のオブジェクトです")
    kind = job.kind
    if kind == "embed":
        m, dim, scale, raw = _decode_vec(result)
        ek = str(result.get("target") or "item")
        if ek not in ("item", "event"):
            raise Rejected("target は item か event です")
        db.merge(Embedding(kind=ek, ref_id=job.ref_id, model=m, dim=dim, scale=scale, vec=raw, created_at=_now()))
    elif kind == "summarize_hq":
        summary = extract.clean_text(result.get("summary"), SUMMARY_MAX)
        if not 20 <= len(summary) <= SUMMARY_MAX:
            raise Rejected("要約は 20〜1500 文字です")
        if db.get(Event, job.ref_id) is None:
            raise Rejected("そのイベントはもうありません")
        writer.get().stage_event_summary(job.ref_id, {"summary": summary, "model": model or "controldeck", "at": datetime.now(_UTC)})
    elif kind == "extract_retry":
        row = db.get(NewsItem, job.ref_id)
        if row is None:
            raise Rejected("その記事はもうありません")
        data = extract.clean(result, f"{row.title}\n{row.summary or ''}\n{row.summary_ja or ''}")
        if data is None or not (data["entities"] or data["facts"]):
            raise Rejected("取り出しの形が合わない（または空）です")
        from qnapnewshub import prep

        sets: dict = {}
        prep._stage_extract(row.id, {"extract": data}, model or "controldeck", sets)  # noqa: SLF001
        if sets:
            writer.get().patch_item(row.id, set=sets)
    elif kind == "translate_hq":
        row = db.get(NewsItem, job.ref_id)
        if row is None:
            raise Rejected("その記事はもうありません")
        from qnapnewshub import translate_flow

        rec = translate_flow.hq_record(result, model or "controldeck")
        if rec is None:
            raise Rejected("翻訳の形が合いません（title_ja・summary_ja・body_ja のどれかは日本語の文字を含む文字列）")
        writer.get().stage_translation(row.id, rec)
    elif kind == "image":
        row = db.get(NewsItem, job.ref_id)
        sha = str(result.get("sha256") or "")
        art = db.get(Artifact, sha) if len(sha) == 64 else None
        if row is None or art is None or not (row.image_url or "").endswith(f"{sha}.{art.ext}"):
            raise Rejected("先に PUT /artifacts/items/{id}/image で画像を送ってください（sha256 が記事の画像と一致しません）")
    elif kind == "event_merge":
        groups = result.get("groups")
        if not isinstance(groups, list) or len(groups) > 200 or not all(isinstance(g, list) and 2 <= len(g) <= 200 and all(isinstance(i, int) for i in g) for g in groups):
            raise Rejected("groups は記事の id の配列の配列です")      # 取り込み（イベントのまとめ直し）は v0.5 以降。いまは検査して結果を残すだけ
    else:  # pragma: no cover
        raise Rejected("未対応の種類です")


def complete(token_id: int, results: list[dict]) -> dict:
    """{"results": [{job_id, status: done|duplicate|failed|rejected|unknown|lease_conflict, error}]}。"""
    def do() -> list[dict]:
        out: list[dict] = []
        pending_flush = False
        with news_db.session() as db:
            now = _now()
            for r in results:
                jid = int(r["job_id"])
                job = db.get(Job, jid)
                if job is None:
                    out.append({"job_id": jid, "status": "unknown", "error": "その仕事はありません"})
                    continue
                if job.status in ("done", "dead"):
                    out.append({"job_id": jid, "status": "duplicate" if job.status == "done" else "dead", "error": ""})
                    continue
                if job.status == "leased" and job.lease_by != token_id and job.lease_until and job.lease_until > now:
                    out.append({"job_id": jid, "status": "lease_conflict", "error": "別の係が借りています"})
                    continue
                err = str(r.get("error") or "")[:300]
                if r.get("status") == "failed":
                    outcome, err = "failed", err or "ControlDeck が失敗を返しました"
                else:
                    try:
                        _apply(db, job, r.get("result"), str(r.get("model") or "")[:200])
                        outcome = "done"
                        pending_flush = pending_flush or job.kind in ("summarize_hq", "extract_retry", "translate_hq")
                    except Rejected as exc:
                        outcome, err = "rejected", str(exc)[:300]
                if outcome == "done":
                    job.status, job.error, job.lease_until, job.model, job.updated_at = "done", "", None, str(r.get("model") or "")[:200], now
                    raw = json.dumps(r.get("result"), ensure_ascii=False)
                    job.result = raw[:2000] if job.kind != "embed" else ""
                else:
                    job.status = "dead" if job.tries >= job.max_tries else "queued"
                    job.error, job.lease_until, job.lease_by, job.updated_at = err, None, 0, now
                out.append({"job_id": jid, "status": outcome, "error": err})
            if pending_flush:      # まとめ書きに積んだ結果を、done にする前に保存する（途中で止まっても、列に戻って同じ結果をもう一度取り込むだけ）
                db.commit()
                writer.get()._flush("action")  # noqa: SLF001
            db.commit()
        return out
    return {"results": writer.run("news.jobs.complete", do, user=False)}


KEEP_DAYS = 30


def trim(db: Session) -> int:
    """終わって（done・dead）KEEP_DAYS 日たった仕事を消す（まとめ書きの中から。コミットは呼ぶ側）。重複を防ぐ ID も消えるが、対象の記事が古くなっているので作り手は再び作らない。"""
    from sqlalchemy import delete

    return db.execute(delete(Job).where(Job.status.in_(("done", "dead")), Job.updated_at < _now() - timedelta(days=KEEP_DAYS))).rowcount or 0


# ---- 診断 ----

def summary(db: Session) -> dict:
    now = _now()
    rows = dict(db.execute(select(Job.status, func.count()).group_by(Job.status)).all())
    expired = db.scalar(select(func.count()).where(Job.status == "leased", Job.lease_until < now)) or 0
    leased = int(rows.get("leased", 0))
    by_kind = {k: int(n) for k, n in db.execute(select(Job.kind, func.count()).where(Job.status == "queued").group_by(Job.kind)).all()}
    done_day = db.scalar(select(func.count()).where(Job.status == "done", Job.updated_at >= now - timedelta(days=1))) or 0
    return {"queued": int(rows.get("queued", 0)), "leased": leased - int(expired), "expired": int(expired), "dead": int(rows.get("dead", 0)), "done_24h": int(done_day), "queued_by_kind": by_kind}
