"""ニュースの専用 DB（data_dir/news/news.sqlite3）。本体の DB・知識基盤の DB とは別のファイル。

- 記事・操作・全文検索（FTS5）・まとめを持つ。ニュースの量（最大 5 万件）と作り直しやすさのため、本体の移行（Alembic）に載せない。
- 知識基盤（app/knowledge/store.py）に倣い、開くとき表・索引を作り（冪等）、後から足した列はそのファイルを最初に開いたとき 1 回足す。
- user_id は整数のまま持つ（本体の users への外部キーは張らない。ファイルをまたぐ制約は作らない）。
  利用者は削除されない（無効にするだけ）ので、行の掃除は要らない。存在しない利用者の行は、その人の id で引かない限り誰にも見えない。
- 同期の SQLAlchemy。async から呼ぶときは asyncio.to_thread を使う（AGENTS.md 絶対ルール 9）。
"""
from __future__ import annotations

import logging
import os
import threading
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint, create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from qnapnewshub.config import data_dir

logger = logging.getLogger("qnapnewshub.db")


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class NewsItem(Base):
    """ニュースの専門ページの記事（アーカイブ。ふだんは消さない）。同じ url は 1 行（更新だけ）。
    kind は release・news・model・event・forum・reddit（後から social などを足せる）。
    user_id は event のときだけ宛先（他は NULL＝全員）。全文検索の仮想表 news_items_fts は app/news/fts.py。"""

    __tablename__ = "news_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    source: Mapped[str] = mapped_column(String(120))
    source_key: Mapped[str] = mapped_column(String(200), index=True)
    title: Mapped[str] = mapped_column(String(400))
    url: Mapped[str] = mapped_column(String(1000), unique=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    image_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # 話題の語の JSON 配列（日本語をそのまま持つので LIKE で絞れる。ensure_ascii=False で書く）
    topics: Mapped[str] = mapped_column(Text, default="[]")
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # 一言要約（LLM。任意）。空なら使わない
    gist: Mapped[str] = mapped_column(Text, default="")
    # 記事のページから画像を取ろうとした印（取れなくても True。再試行しない）。app/news/images.py
    image_tried: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # 英語の抜粋の日本語訳（LLM。2026-11-23 利用者「英語のニュースは日本語に訳して概要を記載」）。空なら訳していない。題は訳さない
    summary_ja: Mapped[str] = mapped_column(Text, default="")
    # 下ごしらえ（app/news/prep.py）が付けた、画像生成のための英語の短い描写。空なら無い
    img_prompt: Mapped[str] = mapped_column(Text, default="", server_default="")
    # 下ごしらえの試行の回数（失敗のたびに +1。PREP_DONE なら済み。3 回で諦めて次の取得でも送らない）
    prep_tries: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 重複の除去（app dedup.py）: 正規化した URL・取得元の GUID・題＋抜粋の SimHash（符号つき 64bit）・代表の記事の id（NULL なら自分が代表）
    canon_url: Mapped[str] = mapped_column(String(1000), default="", server_default="")
    guid: Mapped[str] = mapped_column(String(500), default="", server_default="")
    simhash: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    dup_of: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 要素の取り出し（entities.py）の版。0 なら未（夜の後回しの仕事が拾う）。下ごしらえと同じ 1 回の呼び出しで取り出したら EXTRACT_VERSION が入る
    extract_v: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 画像が ControlDeck の生成物（artifacts.py）に差し替えられた印。画面に「AI 生成のイメージ」を出す
    image_ai: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # 本文の取り出し（bodies.py）: 0 未・1 取れた（news_body に圧縮した本文）・2 取れない（理由は news_body.reason。二度と試さない）
    body_state: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 1 年より古い記事の抜粋を圧縮して news_cold へ移した印（summary は空。索引からは抜粋を外す）
    cold: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class NewsBody(Base):
    """記事の本文（Trafilatura で取り出したもの）を Zstandard で圧縮した BLOB。細かいファイルを作らず DB の中に持つ。
    state=1 なら body に圧縮した本文（raw_len は圧縮前のバイト数）。state=2 なら取れなかった理由（reason）だけで、二度と試さない。"""

    __tablename__ = "news_body"
    __table_args__ = {"sqlite_with_rowid": False}
    item_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    state: Mapped[int] = mapped_column(Integer, default=1)
    body: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    raw_len: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(String(40), default="")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class NewsCold(Base):
    """古い記事の抜粋（summary）を Zstandard で圧縮したもの。raw_len は圧縮前のバイト数（診断の「圧縮の効果」用）。"""

    __tablename__ = "news_cold"
    __table_args__ = {"sqlite_with_rowid": False}
    item_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    z: Mapped[bytes] = mapped_column(LargeBinary)
    raw_len: Mapped[int] = mapped_column(Integer, default=0)


class NewsMeta(Base):
    """検索の索引の状態などの小さな値（search_v・search_cursor・search_hi）。"""

    __tablename__ = "news_meta"
    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class NewsAction(Base):
    """利用者が記事にした操作（open・like・dislike・hide・star）。好みの計算の元。1 人・1 記事・1 種類で 1 行（時刻だけ更新）。"""

    __tablename__ = "news_actions"
    __table_args__ = (UniqueConstraint("user_id", "item_id", "action", name="uq_news_action"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("news_items.id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(String(16))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NewsDigest(Base):
    """日刊・週刊のまとめ。period=daily/weekly、day はその日（週刊は週の初めの月曜）の YYYY-MM-DD。同じ（period, day）は 1 行（作り直しで置き換え）。
    fallback=True は LLM を使えず作った「見出しの一覧」。error はそのときの理由（LLM で作れたときは空）。"""

    __tablename__ = "news_digests"
    __table_args__ = (UniqueConstraint("period", "day", name="uq_news_digest"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    period: Mapped[str] = mapped_column(String(8), index=True)
    day: Mapped[str] = mapped_column(String(10), index=True)
    body: Mapped[str] = mapped_column(Text, default="")
    item_ids: Mapped[str] = mapped_column(Text, default="[]")
    model: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    error: Mapped[str] = mapped_column(Text, default="")
    fallback: Mapped[bool] = mapped_column(Boolean, default=False)


class Entity(Base):
    """物（モデル・ソフトウェア・組織・人・技術・製品・ゲーム・制度・場所）。key は表記ゆれを寄せた正規化のキー（entities.normalize_key）で、1 つの key は 1 行。
    name は最初に見えた表示名、type は最初の種類（other だった物は、より具体的な種類が出たら更新）。article_count は article_entities の数（保存のたびに数え直す）。"""

    __tablename__ = "entities"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(200), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(16), index=True, default="other")
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    article_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", index=True)


class EntityAlias(Base):
    """物の別名（正規化のキーは同じだが、表記が違うもの。例: "GPT-4o" と "GPT 4o"）。"""

    __tablename__ = "entity_aliases"
    __table_args__ = (UniqueConstraint("entity_id", "alias", name="uq_entity_alias"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"), index=True)
    alias: Mapped[str] = mapped_column(String(200))


class ArticleEntity(Base):
    """記事 × 物。記事が消えれば一緒に消える。"""

    __tablename__ = "article_entities"

    article_id: Mapped[int] = mapped_column(ForeignKey("news_items.id", ondelete="CASCADE"), primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True, index=True)


class Claim(Base):
    """記事が述べる個々の事実（facts）。ai_extracted=True は LLM が取り出したもの（確かめた事実ではない。画面に「AI の取り出し」の印を出す）。
    source_article_id は出どころの記事（いまは取り出した記事と同じ。重複の代表へ寄せるのは後の段）。"""

    __tablename__ = "claims"

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("news_items.id", ondelete="CASCADE"), index=True)
    entity_id: Mapped[int | None] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(16), default="other")
    text: Mapped[str] = mapped_column(Text, default="")
    value: Mapped[str] = mapped_column(String(120), default="")
    source_article_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model: Mapped[str] = mapped_column(String(200), default="")
    prompt_version: Mapped[str] = mapped_column(String(16), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ai_extracted: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")


class NewsFetchLog(Base):
    """取得の履歴（ソースごと・1 回ごと。直近 200 回まで、古いものは消す）。画面「取得先の管理」の実績・成功率・履歴の元（app/news/fetchlog.py）。
    status は HTTP の状態（取れた = 200・失敗は読み取れた値。分からなければ NULL）。count は取れた記事の数、new は新着（初めて保存した記事）の数。"""

    __tablename__ = "news_fetch_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_key: Mapped[str] = mapped_column(String(600), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
    new: Mapped[int] = mapped_column(Integer, default=0)
    ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    skipped: Mapped[str] = mapped_column(Text, default="")


class AnaCount(Base):
    """分析の集計の表（analytics.py）: 記事の数。日ごと（grain=d）・時間ごと（grain=h）に、分け方（dim）ごとの数。
    t は地域の時刻の文字（日は YYYY-MM-DD、時間は YYYY-MM-DD HH）。n は記事の数、indep は近い重複を除いた数、img は画像のある記事の数。"""

    __tablename__ = "ana_count"
    __table_args__ = {"sqlite_with_rowid": False}
    grain: Mapped[str] = mapped_column(String(1), primary_key=True)
    dim: Mapped[str] = mapped_column(String(8), primary_key=True)
    t: Mapped[str] = mapped_column(String(13), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    lang: Mapped[str] = mapped_column(String(2), primary_key=True)
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    n: Mapped[int] = mapped_column(Integer, default=0)
    indep: Mapped[int] = mapped_column(Integer, default=0)
    img: Mapped[int] = mapped_column(Integer, default=0)
    # 発表から取得までの遅れ（秒）。0〜48 時間に収まる記事だけ（lag_n 件）。lag_fast は 1 時間以内の件数（新着の傾向 7.5）
    lag_sum: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    lag_n: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    lag_fast: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class AnaTopic(Base):
    """話題の語の数（日ごと d・時間ごと h）。term は小文字にそろえた語、label は表示の表記（最後に見た表記）。"""

    __tablename__ = "ana_topic"
    __table_args__ = {"sqlite_with_rowid": False}
    grain: Mapped[str] = mapped_column(String(1), primary_key=True)
    t: Mapped[str] = mapped_column(String(13), primary_key=True)
    term: Mapped[str] = mapped_column(String(80), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    lang: Mapped[str] = mapped_column(String(2), primary_key=True)
    channel: Mapped[str] = mapped_column(String(30), primary_key=True)
    n: Mapped[int] = mapped_column(Integer, default=0)
    indep: Mapped[int] = mapped_column(Integer, default=0)
    label: Mapped[str] = mapped_column(String(80), default="")


class AnaTopicSrc(Base):
    """語ごとの出どころ（取得先）の組。時間ごとだけ・直近 72 時間だけ持つ（独立した出どころの数を数えるため）。"""

    __tablename__ = "ana_topic_src"
    __table_args__ = {"sqlite_with_rowid": False}
    t: Mapped[str] = mapped_column(String(13), primary_key=True)
    term: Mapped[str] = mapped_column(String(80), primary_key=True)
    source_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    lang: Mapped[str] = mapped_column(String(2), primary_key=True)


class AnaFetch(Base):
    """取得先ごと・日ごとの取得の成否。runs は取得した回数（見送りは含めない）、ok は成功、s304 は 304（変更なし）、new は新着の記事の数。"""

    __tablename__ = "ana_fetch"
    __table_args__ = {"sqlite_with_rowid": False}
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    source_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    runs: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[int] = mapped_column(Integer, default=0)
    s304: Mapped[int] = mapped_column(Integer, default=0)
    new: Mapped[int] = mapped_column(Integer, default=0)


class AnaEntity(Base):
    """物の日ごと（d）・時間ごと（h）の記事の数（物のホットトピック・新着の傾向）。n は記事、indep は近い重複を除いた数。取り出しが保存されるたびに増減を足す。"""

    __tablename__ = "ana_entity"
    __table_args__ = {"sqlite_with_rowid": False}
    grain: Mapped[str] = mapped_column(String(1), primary_key=True)
    t: Mapped[str] = mapped_column(String(13), primary_key=True)
    entity_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    n: Mapped[int] = mapped_column(Integer, default=0)
    indep: Mapped[int] = mapped_column(Integer, default=0)


class Event(Base):
    """イベント＝同じ出来事を報じる 2 件以上の記事のまとまり（events.py。ARCHITECTURE 4 章）。1 件だけの記事は行にしない。
    rep_id は代表の記事（一次情報があればその最初、無ければ最初の記事）で、title はその題の控え。summary は LLM の要約（記事 3 件以上・出どころ 2 以上のとき 1 回）。
    entities は物の上位（JSON の配列 [{id, name, type, n}]）。"""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    rep_id: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(400), default="")
    n_articles: Mapped[int] = mapped_column(Integer, default=0)
    n_sources: Mapped[int] = mapped_column(Integer, default=0)
    has_primary: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    first_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    entities: Mapped[str] = mapped_column(Text, default="[]")
    summary: Mapped[str] = mapped_column(Text, default="")
    summary_model: Mapped[str] = mapped_column(String(200), default="")
    summary_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    summary_tries: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class EventArticle(Base):
    """イベント × 記事（1 つの記事は 1 つのイベントだけ）。記事・イベントが消えれば一緒に消える。"""

    __tablename__ = "event_articles"

    article_id: Mapped[int] = mapped_column(ForeignKey("news_items.id", ondelete="CASCADE"), primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)


class TrendNote(Base):
    """「今日の傾向」の解説（LLM。trend.py）。day は地域の日付、slot は 1 日の何回目か（0・1）。lines は検査を通った行（JSON: text・refs）、stats は材料にした数字の控え。"""

    __tablename__ = "trend_notes"
    __table_args__ = (UniqueConstraint("day", "slot", name="uq_trend_note"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[str] = mapped_column(String(10), index=True)
    slot: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    model: Mapped[str] = mapped_column(String(200), default="")
    lines: Mapped[str] = mapped_column(Text, default="[]")
    stats: Mapped[str] = mapped_column(Text, default="{}")


class ChangeLog(Base):
    """差分の同期（changes.py）の変更番号。記事・物・イベントの追加・更新・削除を、DB の trigger が 1 つの物につき 1 行（最新の変更）に記録する。
    seq は AUTOINCREMENT（単調。消えた番号を再利用しない）。同じ（kind, ref_id）が変わると古い行を消して新しい seq で入れ直す（表は物の数を超えない）。
    op は upsert か delete（削除の印は tombstone として一定期間だけ残す）。at は変更の時刻（UTC・文字）。"""

    __tablename__ = "change_log"
    __table_args__ = (UniqueConstraint("kind", "ref_id", name="uq_change_log"), {"sqlite_autoincrement": True})

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(8))
    ref_id: Mapped[int] = mapped_column(Integer)
    op: Mapped[str] = mapped_column(String(8), default="upsert")
    at: Mapped[str] = mapped_column(String(20), default="")


class Job(Base):
    """仕事の列（jobs.py。ARCHITECTURE 5 章）。dedup_key（種類・対象・入力の版）は 1 つだけ＝同じ仕事を二重に作らない。
    status: queued（借りられる）・leased（lease_until まで借りている）・done・dead（max_tries 回やって終わらなかった）。tries は借りた回数。"""

    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("dedup_key", name="uq_job_dedup"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    ref_id: Mapped[int] = mapped_column(Integer, default=0)
    input_v: Mapped[str] = mapped_column(String(32), default="")
    dedup_key: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(8), default="queued")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_by: Mapped[int] = mapped_column(Integer, default=0)
    tries: Mapped[int] = mapped_column(Integer, default=0)
    max_tries: Mapped[int] = mapped_column(Integer, default=3)
    model: Mapped[str] = mapped_column(String(200), default="")
    result: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


Index("ix_jobs_pick", Job.status, Job.kind, Job.id)
Index("ix_jobs_ref", Job.kind, Job.ref_id)


class Embedding(Base):
    """埋め込み（INT8。ControlDeck が埋め込み、仕事の結果として返す）。vec は dim バイト（符号つき 8bit）、scale は元の値 ≒ vec × scale。受け取って保存するだけ（使う検索は v0.6）。"""

    __tablename__ = "embeddings"

    kind: Mapped[str] = mapped_column(String(8), primary_key=True)      # item・event
    ref_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model: Mapped[str] = mapped_column(String(80), primary_key=True)
    dim: Mapped[int] = mapped_column(Integer)
    scale: Mapped[float] = mapped_column(Float)
    vec: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Artifact(Base):
    """ControlDeck から受け取った生成物（画像）。sha256 が主キー＝同じ中身は 1 つだけ保存する（重複管理）。ファイルは data_dir/artifacts/images/<sha256>.<ext>。"""

    __tablename__ = "artifacts"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(8), default="image")
    ext: Mapped[str] = mapped_column(String(5))
    size: Mapped[int] = mapped_column(Integer)
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    peer_id: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ResearchRecord(Base):
    """調査の記録（ControlDeck の Web 調査: テーマ・出典・確かめた事実・結論）。いまは保存だけ（使うのは v0.6）。digest（内容の sha256）が同じなら二重に入れない。"""

    __tablename__ = "research_records"
    __table_args__ = (UniqueConstraint("digest", name="uq_research_digest"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    peer_id: Mapped[int] = mapped_column(Integer, default=0)
    theme: Mapped[str] = mapped_column(String(200))
    sources: Mapped[str] = mapped_column(Text, default="[]")
    facts: Mapped[str] = mapped_column(Text, default="[]")
    conclusion: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AnaMeta(Base):
    """集計の表の状態（sig: 作ったときのチャンネル・出どころの設定の指紋、built_at）。"""

    __tablename__ = "ana_meta"
    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


# 後から足した列（既にあるファイルにも足す。そのファイルを初めて開いたとき 1 回）。（表, 列, 宣言）
_LATE_COLUMNS: list[tuple[str, str, str]] = [("news_items", "image_tried", "BOOLEAN NOT NULL DEFAULT 0"), ("news_items", "summary_ja", "TEXT DEFAULT ''"),
                                        ("news_items", "img_prompt", "TEXT DEFAULT ''"), ("news_items", "prep_tries", "INTEGER NOT NULL DEFAULT 0"),
                                        ("news_items", "canon_url", "VARCHAR(1000) DEFAULT ''"), ("news_items", "guid", "VARCHAR(500) DEFAULT ''"),
                                        ("news_items", "simhash", "BIGINT"), ("news_items", "dup_of", "INTEGER"),
                                        ("news_items", "extract_v", "INTEGER NOT NULL DEFAULT 0"), ("news_items", "image_ai", "BOOLEAN NOT NULL DEFAULT 0"),
                                        ("ana_count", "lag_sum", "BIGINT NOT NULL DEFAULT 0"), ("ana_count", "lag_n", "INTEGER NOT NULL DEFAULT 0"),
                                        ("ana_count", "lag_fast", "INTEGER NOT NULL DEFAULT 0"),
                                        ("news_items", "body_state", "INTEGER NOT NULL DEFAULT 0"), ("news_items", "cold", "INTEGER NOT NULL DEFAULT 0")]
# 後から足した索引（既にあるファイルにも足す）
_LATE_INDEXES = [("news_items", "canon_url"), ("news_items", "dup_of"), ("news_items", "fetched_at")]

_engines: dict[str, Engine] = {}
_lock = threading.Lock()


def path() -> Path:
    return data_dir() / "news" / "news.sqlite3"


def _add_missing_columns(conn) -> None:
    have = inspect(conn)
    for table, column, decl in _LATE_COLUMNS:
        if column not in {c["name"] for c in have.get_columns(table)}:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {decl}"))
    have = inspect(conn)
    for table, column in _LATE_INDEXES:
        if column in {c["name"] for c in have.get_columns(table)}:
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{table}_{column} ON {table} ({column})"))


def _cache_kb() -> int:
    """SQLite のページのキャッシュの上限（KiB）。既定 256MB。NEWSHUB_SQLITE_CACHE_MB で変える（使った分だけ確保される）。"""
    try:
        return max(2, int(os.environ.get("NEWSHUB_SQLITE_CACHE_MB", "256"))) * 1024
    except ValueError:
        return 256 * 1024


def _open(file: Path) -> Engine:
    file.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{file}", connect_args={"timeout": 10, "check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi, _record):  # noqa: ANN001
        cur = dbapi.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA wal_autocheckpoint=0")      # checkpoint は Write Coordinator が、まとめ書きの直後にだけ行う
        cur.execute("PRAGMA busy_timeout=10000")
        cur.execute(f"PRAGMA cache_size=-{_cache_kb()}")      # ページのキャッシュ（ARCHITECTURE 9: 256〜512MB が目安。使った分だけ確保される）
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    from qnapnewshub.searchindex import ensure as ensure_search

    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        _add_missing_columns(conn)
        from qnapnewshub import changes

        changes.install(conn)
        try:
            ensure_search(conn)
        except Exception as exc:  # noqa: BLE001 - 検索は LIKE に落ちる
            logger.warning("全文検索の索引を用意できません: %s", exc)
    return engine


def engine() -> Engine:
    """いまの data_dir のニュース DB。ファイルが消えた（作り直した）ら開き直す。"""
    file = path()
    key = str(file)
    with _lock:
        found = _engines.get(key)
        if found is not None and not file.exists():
            found.dispose()
            found = None
        if found is None:
            found = _engines[key] = _open(file)
        return found


def session() -> Session:
    return sessionmaker(bind=engine(), expire_on_commit=True)()


def get_news_db() -> Iterator[Session]:
    """FastAPI の依存（本体の get_db のニュース版）。"""
    with session() as db:
        yield db
