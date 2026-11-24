"""ニュースの専用 DB（data_dir/news/news.sqlite3）。本体の DB・知識基盤の DB とは別のファイル。

- 記事・操作・全文検索（FTS5）・まとめを持つ。ニュースの量（最大 5 万件）と作り直しやすさのため、本体の移行（Alembic）に載せない。
- 知識基盤（app/knowledge/store.py）に倣い、開くとき表・索引を作り（冪等）、後から足した列はそのファイルを最初に開いたとき 1 回足す。
- user_id は整数のまま持つ（本体の users への外部キーは張らない。ファイルをまたぐ制約は作らない）。
  利用者は削除されない（無効にするだけ）ので、行の掃除は要らない。存在しない利用者の行は、その人の id で引かない限り誰にも見えない。
- 同期の SQLAlchemy。async から呼ぶときは asyncio.to_thread を使う（AGENTS.md 絶対ルール 9）。
"""
from __future__ import annotations

import json

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
    # 機密の区分（kg.py）: 公開の文書に出たことがある物は public。機密の文書だけから作られた物は confidential（AI_USE は local_only 以上）
    sensitivity: Mapped[str] = mapped_column(String(12), default="public", server_default="public")
    ai_use: Mapped[str] = mapped_column(String(12), default="allowed", server_default="allowed")


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
    # ニュース以外の文書（v0.5 の documents）から作った主張は article_id が NULL で document_id だけ（既存の DB は _rebuild_claims で NULL を許す形へ 1 回だけ作り直す）
    article_id: Mapped[int | None] = mapped_column(ForeignKey("news_items.id", ondelete="CASCADE"), nullable=True, index=True)
    entity_id: Mapped[int | None] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(16), default="other")
    text: Mapped[str] = mapped_column(Text, default="")
    value: Mapped[str] = mapped_column(String(120), default="")
    source_article_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model: Mapped[str] = mapped_column(String(200), default="")
    prompt_version: Mapped[str] = mapped_column(String(16), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ai_extracted: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    # 出どころの文書（documents）と、そこから引き継いだ機密の区分（kg.inherit）。根拠は evidences を複数持てる
    document_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    sensitivity: Mapped[str] = mapped_column(String(12), default="public", server_default="public")
    ai_use: Mapped[str] = mapped_column(String(12), default="allowed", server_default="allowed")


class Document(Base):
    """文書（統合ナレッジ基盤の正本の単位。ARCHITECTURE 3 章・ControlDeck の PLAN-integrated-knowledge 4 章）。種類 kind は kg.DOC_KINDS。
    ident は正規化した識別子（kg.normalize_ident: url:・doi:・arxiv:・repo:（リポジトリ＋SHA）・patent:・standard:）で 1 つだけ。
    sensitivity（public・confidential）と ai_use（allowed・local_only・prohibited）は、この文書から作った主張・関係・カードへ引き継ぐ。ニュースの記事は news_documents で対応づける。"""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(12), index=True)
    ident: Mapped[str] = mapped_column(String(1100), unique=True)
    title: Mapped[str] = mapped_column(String(400), default="")
    url: Mapped[str] = mapped_column(String(1000), default="")
    source: Mapped[str] = mapped_column(String(120), default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    version: Mapped[str] = mapped_column(String(64), default="")
    sensitivity: Mapped[str] = mapped_column(String(12), default="confidential", server_default="confidential", index=True)
    ai_use: Mapped[str] = mapped_column(String(12), default="local_only", server_default="local_only")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # 保存の段（v0.7。docstore.py）: 0 発見（URL・題・日付・ID だけ）・1 概要・書誌・2 本文・3 物・主張・関係・4 詳しい分析。昇格の理由と時刻は最後の 1 回（履歴は document_promotions）
    tier: Mapped[int] = mapped_column(Integer, default=1, server_default="1", index=True)
    tier_reason: Mapped[str] = mapped_column(String(160), default="", server_default="")
    tier_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    abstract: Mapped[str] = mapped_column(Text, default="", server_default="")
    meta: Mapped[str] = mapped_column(Text, default="", server_default="")      # JSON（著者・引用の数・DOI・量子化の種類など。connector ごと）
    connector: Mapped[str] = mapped_column(String(40), default="", server_default="")


class DocumentAlias(Base):
    """文書の別の識別子（論文の DOI・arXiv ID・OpenAlex ID・Semantic Scholar ID は同じ論文を指す）。docstore.upsert が同じ論文を 1 件にまとめるのに使う。"""

    __tablename__ = "document_aliases"

    ident: Mapped[str] = mapped_column(String(1100), primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)


class DocumentPromotion(Base):
    """段の昇格の記録（理由・時刻）。上げるだけで、下げない。"""

    __tablename__ = "document_promotions"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    from_tier: Mapped[int] = mapped_column(Integer)
    to_tier: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(160), default="")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class DocumentLink(Base):
    """文書どうしの辺（論文の引用 cites など）。物どうしの relationships とは別（文書は物ではない）。"""

    __tablename__ = "document_links"
    __table_args__ = (UniqueConstraint("src_id", "dst_id", "type", name="uq_document_links"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    src_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    dst_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(16), default="cites")
    source: Mapped[str] = mapped_column(String(40), default="")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MetricPoint(Base):
    """数値の時系列（Metric）。物（subject: 例 repo:owner/name）・指標・値・単位・時刻・出どころ・条件。同じ（物・指標・時刻・出どころ・条件）は 1 点。"""

    __tablename__ = "metric_points"
    __table_args__ = (UniqueConstraint("subject", "metric", "at", "source", "cond", name="uq_metric_points"), Index("ix_metric_points_series", "subject", "metric", "at"))

    id: Mapped[int] = mapped_column(primary_key=True)
    subject: Mapped[str] = mapped_column(String(300))
    metric: Mapped[str] = mapped_column(String(60))
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(20), default="")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(40), default="")
    cond: Mapped[str] = mapped_column(String(200), default="")      # 条件（例 window=1d）
    document_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class NewsDocument(Base):
    """ニュースの記事 → 文書の対応（既存の news_items を壊さずに documents へ結ぶ。重複した記事は同じ文書を指す）。kg.sync_documents が冪等に作る。"""

    __tablename__ = "news_documents"

    news_item_id: Mapped[int] = mapped_column(ForeignKey("news_items.id", ondelete="CASCADE"), primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)


class Evidence(Base):
    """主張・関係の根拠。文書の中の位置（文字の範囲 start〜end・ページ page。分かるときだけ）と、引用の短い原文 quote・その hash。
    claim_id か relationship_id のどちらかに付く（1 つの主張・関係が根拠を複数持てる）。grounded は原文に照合できたか、confidence はその根拠の確かさ。"""

    __tablename__ = "evidences"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    claim_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    relationship_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quote: Mapped[str] = mapped_column(String(300), default="")
    quote_hash: Mapped[str] = mapped_column(String(64), default="")
    grounded: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    confidence: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Relationship(Base):
    """物どうしの関係。type は kg.REL_TYPES。directed=False（co_occurs・competes・contradicts）は subject_id < object_id にそろえる。同じ（主語・目的語・型）は 1 行。
    status: extracted（原文に引用で照合できた AI の取り出し）・inferred（根拠の照合が無い推測。確かさは低く、確定にしない）・statistical（一緒に出る集計）・confirmed（人が根拠つきで確認）。
    activity_*（W3C PROV の最小）は、最後に生成した活動の model・版・人。stat_ratio は co_occurs の期待値の比。sensitivity・ai_use は根拠の文書から引き継ぐ。"""

    __tablename__ = "relationships"
    __table_args__ = (UniqueConstraint("subject_id", "object_id", "type", name="uq_relationship"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    subject_id: Mapped[int] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"))
    object_id: Mapped[int] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(String(12))
    directed: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(12), default="inferred")
    evidence_count: Mapped[int] = mapped_column(Integer, default=0)
    stat_n: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    stat_ratio: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    first_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activity_model: Mapped[str] = mapped_column(String(200), default="")
    activity_version: Mapped[str] = mapped_column(String(16), default="")
    activity_by: Mapped[str] = mapped_column(String(80), default="")
    sensitivity: Mapped[str] = mapped_column(String(12), default="public", server_default="public")
    ai_use: Mapped[str] = mapped_column(String(12), default="allowed", server_default="allowed")


# 地図の段ごとのまとめ取り（neighbors）は、物から出る辺・入る辺を確かさの順に上限つきで読む
Index("ix_rel_subject", Relationship.subject_id, Relationship.confidence)
Index("ix_rel_object", Relationship.object_id, Relationship.confidence)
Index("ix_rel_type", Relationship.type)
# 次数・最後の辺の日時を索引だけで数える（v0.6。点の次数を返す・ハブを見分ける）。既にあるファイルには _LATE_INDEX_SQL が足す
Index("ix_rel_subj_deg", Relationship.subject_id, Relationship.sensitivity, Relationship.last_at)
Index("ix_rel_obj_deg", Relationship.object_id, Relationship.sensitivity, Relationship.last_at)


class EntityNote(Base):
    """利用者のメモ（物ごと）。AI の部分（entity_cards）とは別の表に持ち、再分析・カードの作り直しでは触れない。保存は即時。"""

    __tablename__ = "entity_notes"

    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True)
    body: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_by: Mapped[int] = mapped_column(Integer, default=0)


class EntityCard(Base):
    """知識カードの AI の部分（概要）。sig は概要を作ったときの材料の指紋（変化があったときだけ作り直す）。tried_sig・tries は、作れなかった材料の指紋と失敗の回数（同じ材料では 3 回で諦める）。古い概要は、新しいものが作れるまで残す。"""

    __tablename__ = "entity_cards"

    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(200), default="")
    sig: Mapped[str] = mapped_column(String(40), default="")
    tried_sig: Mapped[str] = mapped_column(String(40), default="")
    tries: Mapped[int] = mapped_column(Integer, default=0)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Things の脳（2026-11-25）: 1 行の定義・今の状態（official・experimental・deprecated・unknown）・未解決の問い・反対の見方（JSON の文字列の配列）。材料に無い事実は検査で捨てる
    definition: Mapped[str] = mapped_column(Text, default="", server_default="")
    state: Mapped[str] = mapped_column(String(12), default="", server_default="")
    questions: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    opposing: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")


class Community(Base):
    """主題のまとまり（夜に関係から計算。things_graph.communities）。public の物だけで計算する（機密の物は入れない）。top_ids は代表の物の id（JSON の配列・次数の多い順）。"""

    __tablename__ = "communities"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    rep_id: Mapped[int] = mapped_column(Integer, default=0)
    top_ids: Mapped[str] = mapped_column(Text, default="[]")
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EntityCommunity(Base):
    __tablename__ = "entity_communities"

    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True)
    community_id: Mapped[int] = mapped_column(Integer, index=True)


class MentionCandidate(Base):
    """リンクされていない言及: 物の名前・別名が記事の題・抜粋に出ているのに article_entities に無い記事（夜に計算）。status: candidate・confirmed（押して確定）・dismissed（違う）。"""

    __tablename__ = "mention_candidates"
    __table_args__ = (UniqueConstraint("entity_id", "article_id", name="uq_mention"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"), index=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("news_items.id", ondelete="CASCADE"), index=True)
    matched: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(10), default="candidate")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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
    theme: Mapped[str] = mapped_column(String(200), index=True)
    sources: Mapped[str] = mapped_column(Text, default="[]")
    facts: Mapped[str] = mapped_column(Text, default="[]")
    conclusion: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # 機密の区分（v0.6）。ControlDeck が送る調査の記録は公開の調査だけ（機密の会話の調査は ControlDeck の store に残す）なので、既定は public。読む API は区分で絞る
    sensitivity: Mapped[str] = mapped_column(String(12), default="public", server_default="public")


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
                                        ("news_items", "body_state", "INTEGER NOT NULL DEFAULT 0"), ("news_items", "cold", "INTEGER NOT NULL DEFAULT 0"),
                                        ("claims", "document_id", "INTEGER"), ("claims", "sensitivity", "VARCHAR(12) NOT NULL DEFAULT 'public'"),
                                        ("claims", "ai_use", "VARCHAR(12) NOT NULL DEFAULT 'allowed'"),
                                        ("entities", "sensitivity", "VARCHAR(12) NOT NULL DEFAULT 'public'"), ("entities", "ai_use", "VARCHAR(12) NOT NULL DEFAULT 'allowed'"),
                                        ("documents", "tier", "INTEGER NOT NULL DEFAULT 1"), ("documents", "tier_reason", "VARCHAR(160) NOT NULL DEFAULT ''"),
                                        ("documents", "tier_at", "DATETIME"), ("documents", "abstract", "TEXT NOT NULL DEFAULT ''"),
                                        ("documents", "meta", "TEXT NOT NULL DEFAULT ''"), ("documents", "connector", "VARCHAR(40) NOT NULL DEFAULT ''"),
                                        ("research_records", "sensitivity", "VARCHAR(12) NOT NULL DEFAULT 'public'"),
                                        ("entity_cards", "definition", "TEXT NOT NULL DEFAULT ''"), ("entity_cards", "state", "VARCHAR(12) NOT NULL DEFAULT ''"),
                                        ("entity_cards", "questions", "TEXT NOT NULL DEFAULT '[]'"), ("entity_cards", "opposing", "TEXT NOT NULL DEFAULT '[]'")]
# 後から足した索引（既にあるファイルにも足す）
_LATE_INDEXES = [("news_items", "canon_url"), ("news_items", "dup_of"), ("news_items", "fetched_at"), ("claims", "document_id"), ("documents", "tier"), ("research_records", "theme")]
# 複数の列の索引で、あとから足したもの（既にあるファイルにも足す）
_LATE_INDEX_SQL = ["CREATE INDEX IF NOT EXISTS ix_rel_subj_deg ON relationships (subject_id, sensitivity, last_at)",
                   "CREATE INDEX IF NOT EXISTS ix_rel_obj_deg ON relationships (object_id, sensitivity, last_at)"]

_engines: dict[str, Engine] = {}
_lock = threading.Lock()


def path() -> Path:
    return data_dir() / "news" / "news.sqlite3"


def _rebuild_claims(conn) -> None:
    """claims.article_id の NOT NULL を外す（ニュース以外の文書の主張は記事が無い）。SQLite は列の制約を変えられないので、表を作り直して写す。
    作り直しは article_id が NOT NULL の古い形のときだけ（1 回だけ。冪等）。claims を参照する外部キーは無い（evidences.claim_id は整数だけ）。"""
    info = {r[1]: r for r in conn.execute(text("PRAGMA table_info(claims)")).all()}
    if not info or not info["article_id"][3]:
        return
    cols = [c for c in info if c in {"id", "article_id", "entity_id", "kind", "text", "value", "source_article_id", "model", "prompt_version", "created_at", "ai_extracted",
                                    "document_id", "sensitivity", "ai_use"}]
    conn.execute(text("ALTER TABLE claims RENAME TO claims_old"))
    conn.execute(text("DROP INDEX IF EXISTS ix_claims_article_id"))
    conn.execute(text("DROP INDEX IF EXISTS ix_claims_entity_id"))
    conn.execute(text("DROP INDEX IF EXISTS ix_claims_document_id"))
    Claim.__table__.create(conn)
    names = ", ".join(cols)
    conn.execute(text(f"INSERT INTO claims ({names}) SELECT {names} FROM claims_old"))
    conn.execute(text("DROP TABLE claims_old"))


def _add_missing_columns(conn) -> None:
    have = inspect(conn)
    for table, column, decl in _LATE_COLUMNS:
        if column not in {c["name"] for c in have.get_columns(table)}:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {decl}"))
    have = inspect(conn)
    for table, column in _LATE_INDEXES:
        if column in {c["name"] for c in have.get_columns(table)}:
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{table}_{column} ON {table} ({column})"))
    for sql in _LATE_INDEX_SQL:
        conn.execute(text(sql))
    _rebuild_claims(conn)


def _repair_split_topics(conn) -> None:
    """一度だけ: 話題の語に紛れ込んだ 1 文字の語を外し、集計を作り直させる。
    2026-11-25: 下ごしらえと一言要約が extract_topics(*語) と語を引数に展開していて、3 つ目の語が 1 文字ずつに分かれて
    保存され、ホットトピックの上位が「n・s・e・t」になった（直したのは prep.py・gist.py）。"""
    done = conn.exec_driver_sql("SELECT value FROM news_meta WHERE key = 'repair_topics_v1'").fetchone()
    if done:
        return
    cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(news_items)").fetchall()}
    if "topics" not in cols:      # 話題の列の無い古い版（崩れた語も無い）
        conn.exec_driver_sql("INSERT OR REPLACE INTO news_meta (key, value) VALUES ('repair_topics_v1', '0')")
        return
    fixed = 0
    rows = conn.exec_driver_sql("SELECT id, topics FROM news_items WHERE topics LIKE '[%'").fetchall()
    for item_id, raw in rows:
        try:
            words = json.loads(raw or "[]")
        except ValueError:
            continue
        if not isinstance(words, list):
            continue
        kept = [w for w in words if isinstance(w, str) and len(w.strip()) > 1]
        if len(kept) != len(words):
            conn.exec_driver_sql("UPDATE news_items SET topics = ? WHERE id = ?", (json.dumps(kept, ensure_ascii=False), item_id))
            fixed += 1
    if fixed:
        conn.exec_driver_sql("DELETE FROM ana_meta WHERE key = 'sig'")      # 次のまとめ書きで集計を作り直す
        logger.warning("話題の語の 1 文字の崩れを %d 件の記事で直しました（集計を作り直します）", fixed)
    conn.exec_driver_sql("INSERT OR REPLACE INTO news_meta (key, value) VALUES ('repair_topics_v1', ?)", (str(fixed),))


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
        _repair_split_topics(conn)
        from qnapnewshub import changes

        changes.install(conn)
        from qnapnewshub import docstore

        docstore.ensure(conn)
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
