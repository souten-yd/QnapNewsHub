"""物（Things）の機械の除外（LLM なし）。ARCHITECTURE 6 章（取り出しの精度・2026-11-25）。

目的: 50 記事の測定（2026-11-25）で、取り出した物の 21% が誤りだった（取得元・媒体の名前 5%・一般語や断片 11%・版や別名 5%）。LLM を増やさず、登録のとき・夜の掃除で外す。
- **取得元・媒体の名前**: その記事の出どころ（`NewsItem.source`）と同名の物、または媒体名の一覧（`MEDIA`。技術の組織を含まない純粋な媒体だけ）。記事の題に（末尾の「 - 出どころ」を除いて）出る物は残す。
- **プラットフォーム**（GitHub・Hugging Face・YouTube など）: 題に出ないときは物にしない。関係の相手にも使わない（「Scrapling cites GitHub」）。
- **一般語・断片**（`GENERIC`・`reject`）: 「AI coding agent」「browser」「Mac tool」「financial firm」「algorithm」「取締役会」「不安定な爆薬」、版だけの名前（v0.5.22・3.13）、1 文字・数だけ・文のような日本語。
  迷うもの（固有名詞らしい）は残す。名前が頭文字の大文字・数字・記号を含む固有名らしさを持つ物は一般語として落とさない（種類が org・person・place・model の物は一覧に載る語でない限り残す）。
- 掃除（`retire_sweep`・夜）: 既にある物のうち、文脈なしで落とせるもの（媒体名・一般語・断片）を外す。外す物の記録は `entity_retirements`（名前・種類・理由・記事の数。元に戻す道は、規則をゆるめて取り出し直すこと）。
  人がメモを書いた物・機密の物は触らない。1 回の上限あり。**Entity を消すのはここと entity_merge.merge だけ**。
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from qnapnewshub.db import (ArticleEntity, Claim, Entity, EntityAlias, EntityCard, EntityCommunity, EntityNote, EntityRetirement, Evidence, MentionCandidate, NewsItem,
                            Relationship)
from qnapnewshub.extract import normalize_key

_UTC = timezone.utc
MAX_RETIRE_PER_RUN = 400

# 純粋な媒体・取得元（技術の組織を含まない）。キーは normalize_key。
MEDIA_NAMES = ("XDA", "XDA Developers", "Android Authority", "The Register", "Ars Technica", "Phoronix", "Towards Data Science", "Towards AI", "DevelopersIO", "Hacker News", "Engadget",
               "Gizmodo", "9to5Mac", "9to5Google", "GIGAZINE", "WIRED", "WIRED Japan", "Qiita", "Zenn", "Infoseek", "Infoseek News", "ITmedia", "Impress Watch", "TechCrunch", "The Verge",
               "VentureBeat", "ZDNet", "CNET", "Hackaday", "Slashdot", "Reddit", "Techmeme", "MacRumors", "Neowin", "OMG Ubuntu", "Tom's Hardware", "AnandTech", "Wccftech",
               "Business Insider", "Forbes", "Reuters", "Bloomberg", "Yahoo!ニュース", "Yahoo News", "はてなブックマーク", "note", "Publickey", "ASCII.jp", "PC Watch", "Gadget Watch",
               "GetNavi", "Gigazine", "Lifehacker", "Mashable", "9to5Linux", "Linux Today", "LWN.net", "InfoQ", "The New Stack", "SiliconANGLE", "Dev.to", "Medium", "Substack",
               "国内企業テックブログ統合", "level1techs", "Level1Techs Forum")
MEDIA = frozenset(normalize_key(n) for n in MEDIA_NAMES) - {""}
# note は一般語としても短いので、種類が media らしいときだけでなく無条件に落とす（物としての note は他の語と区別が付かない）。

# 題に出ないときは物にしないプラットフォーム
PLATFORM_NAMES = ("GitHub", "GitLab", "Hugging Face", "HuggingFace", "YouTube", "Twitter", "X", "Facebook", "Bluesky", "Mastodon", "Discord", "Telegram", "LinkedIn", "Stack Overflow",
                  "npm", "PyPI", "Docker Hub", "Product Hunt", "Wikipedia", "arXiv", "Google Play", "App Store", "Steam")
PLATFORMS = frozenset(normalize_key(n) for n in PLATFORM_NAMES) - {""}

GENERIC_EN = ("AI", "ML", "LLM", "LLMs", "AGI", "API", "APIs", "SDK", "CLI", "GUI", "UI", "UX", "OS", "PC", "GPU", "CPU", "RAM", "SSD", "OSS", "AI agent", "AI agents", "agent", "agents",
              "AI coding agent", "coding agent", "AI model", "AI models", "language model", "language models", "large language model", "large language models", "browser", "browsers",
              "tool", "tools", "Mac tool", "algorithm", "algorithms", "model", "models", "software", "hardware", "app", "apps", "application", "startup", "startups", "company", "companies",
              "firm", "financial firm", "ransomware", "malware", "virus", "security", "cloud", "chip", "chips", "data", "dataset", "open source", "open-source", "framework", "library",
              "platform", "service", "system", "robot", "robots", "phone", "smartphone", "laptop", "device", "devices", "feature", "update", "release", "version", "bug", "vulnerability",
              "exploit", "attack", "hacker", "hackers", "developer", "developers", "user", "users", "customer", "customers", "government", "regulators", "researchers", "scientists", "study",
              "paper", "report", "survey", "market", "industry", "technology", "internet", "web", "website", "email", "password", "code", "program", "programming", "server", "network",
              "database", "game", "games", "gaming", "console", "battery", "screen", "display", "camera", "price", "deal", "sale", "event", "conference", "news", "blog", "article")
GENERIC_JA = ("取締役会", "マネジメントコンソール", "不安定な爆薬", "リーガルAI", "ランサムウェア", "マルウェア", "ウイルス", "セキュリティ", "クラウド", "サーバー", "ネットワーク", "データベース",
              "アルゴリズム", "ブラウザ", "ブラウザー", "アプリ", "ソフトウェア", "ハードウェア", "スタートアップ", "企業", "会社", "政府", "研究者", "開発者", "ユーザー", "利用者", "サービス", "システム",
              "プラットフォーム", "フレームワーク", "ライブラリ", "モデル", "AIエージェント", "AIモデル", "生成AI", "人工知能", "機械学習", "大規模言語モデル", "半導体", "スマートフォン", "スマホ",
              "パソコン", "ノートPC", "ゲーム", "ニュース", "ブログ", "記事", "論文", "調査", "発表", "新製品", "新機能", "アップデート", "脆弱性", "攻撃", "ハッカー", "個人情報", "オープンソース")
GENERIC = frozenset(normalize_key(n) for n in (*GENERIC_EN, *GENERIC_JA)) - {""}

# 「修飾語＋一般名詞」の組（Mac tool・financial firm・AI coding agent）。語の全部が修飾語か一般名詞で、最後が一般名詞のとき一般語とする。
_HEADS = frozenset("agent agents tool tools firm firms company companies model models app apps framework library platform service services system systems startup startups browser "
                   "assistant assistants chatbot chatbots robot robots device devices chip chips algorithm algorithms feature features library libraries".split())
_MODS = frozenset("ai ml llm mac windows linux android ios web cloud local open source coding code financial legal medical enterprise new free popular major leading large small "
                  "security data smart mobile desktop online digital generative autonomous agentic".split())
_VERSION_ONLY = re.compile(r"^(?:v|ver\.?|version)?\s*\d+(?:[.\-_]\d+)*[a-z]?(?:\s*(?:rc|beta|alpha|preview)\s*\d*)?$", re.I)
_QUANTITY = re.compile(r"^(?=.*\d)[\s\d.,~〜～\-–/:第年月日週回件人円ドル倍%％時間分秒]+$|^[\d.,]+\s*(?:weeks?|days?|hours?|minutes?|months?|years?|percent|dollars?|times)$", re.I)      # 日付・回数・量（「24日」「第 4回」「30.7 weeks」）
_PARTICLE = re.compile(r"[のなをがはにでとやもへ]|から|まで|より|という|した|する|される|れた")
_CJK = re.compile(r"[぀-ヿ一-鿿]")
_TAIL = re.compile(r"\s*[-–—|:｜]\s*[^-–—|:｜]{1,60}$")


_MEDIA_WORD = re.compile(r"watch|news|blog|online|times|journal|today|magazine|review|report|forum|digest|tribune|post|media|network|press|room|\.(?:com|jp|net|org|io)\b|"
                         r"ニュース|ブログ|新聞|放送|通信|ウォッチ|オンライン|マガジン|フォーラム|情報|編集部|速報|テレビ", re.I)


def _core(name: str) -> str:
    return normalize_key(name)


def _title_head(title: str) -> str:
    """題から末尾の「 - 出どころ」を除く（出どころの名前が題の最後に付く記事が多い）。"""
    return _TAIL.sub("", unicodedata.normalize("NFKC", str(title or "")), count=1)


def _in_title(name: str, title: str) -> bool:
    k = normalize_key(name)
    return bool(k) and k in normalize_key(_title_head(title))


_SOURCE_SUFFIX = re.compile(r"\b(developers?|blog|news|magazine|official|japan|japanese|jp|forum|forums|community|team|press|online|network)\b|公式|ブログ|ニュース|編集部", re.I)


def source_cores(source: str) -> set[str]:
    """出どころの名前から、同名の物とみなす鍵（そのまま・語尾の Developers/Blog/News/公式 を除いたもの・ドメインの語幹）。"""
    out: set[str] = set()
    s = unicodedata.normalize("NFKC", str(source or "")).strip()
    if not s:
        return out
    out.add(normalize_key(s))
    out.add(normalize_key(_SOURCE_SUFFIX.sub(" ", s)))
    if "." in s and " " not in s:      # forum.level1techs.com → level1techs
        parts = [p for p in s.lower().split(".") if p not in {"www", "com", "org", "net", "jp", "co", "io", "forum", "discuss", "blog", "news"}]
        out.update(normalize_key(p) for p in parts)
    out.discard("")
    return {k for k in out if len(k) >= 3}


def is_platform(name: str) -> bool:
    return normalize_key(name) in PLATFORMS


def is_media(name: str) -> bool:
    return normalize_key(name) in MEDIA


def is_generic(name: str, kind: str = "other") -> bool:
    """一般語・断片（文脈なしで言える分）。固有名らしい物（org・person・place・model など具体の種類で、一覧に無い語）は落とさない。"""
    key = normalize_key(name)
    if not key:
        return True
    if key in GENERIC:
        return True
    nfkc = unicodedata.normalize("NFKC", str(name or "")).strip()
    if len(key) < 2 and not _CJK.search(nfkc):
        return True
    if _VERSION_ONLY.match(nfkc) or key.isdigit() or _QUANTITY.match(nfkc):
        return True
    tokens = [t for t in re.split(r"[\s\-_/]+", nfkc.casefold()) if t]
    if len(tokens) >= 2 and tokens[-1] in _HEADS and all(t in _MODS or t in _HEADS for t in tokens):
        return True
    if _CJK.search(nfkc) and not nfkc.isascii() and kind in ("other", "tech", "product") and len(nfkc) >= 6 and _PARTICLE.search(nfkc) and not re.search(r"[A-Za-z0-9]", nfkc):
        return True      # 「不安定な爆薬」のような文の断片（助詞を含む日本語。固有名はふつう助詞を含まない）
    return False


def reject(name: str, kind: str = "other", *, source: str = "", title: str = "", context: bool = True) -> str:
    """物にしない理由（"" は残す）。media・source・platform・generic。context=False は記事の文脈（出どころ・題）なしで言える分だけ（夜の掃除）。"""
    if is_media(name):
        return "media"
    if is_generic(name, kind):
        return "generic"
    if not context:
        return ""
    key = normalize_key(name)
    if source and kind in ("org", "other") and key in source_cores(source) and not _in_title(name, title) and (kind == "other" or _MEDIA_WORD.search(unicodedata.normalize("NFKC", name))):
        return "source"      # 出どころの名前。発表する側の組織・人・ソフト（自分の記事を出す物）は残す
    if key in PLATFORMS and not _in_title(name, title):
        return "platform"
    return ""


# ---- 夜の掃除 ----

def retire(db: Session, ent: Entity, reason: str) -> bool:
    """物 1 つを外す（主張・関係・記事の結び付き・まとまり・カードごと）。人のメモ・機密の物は触らない。集計（ana_entity）と検索の索引を直す。外せたら True。コミットは呼ぶ側。"""
    from qnapnewshub import analytics, searchindex

    if ent.sensitivity != "public" or (db.get(EntityNote, ent.id) is not None and db.get(EntityNote, ent.id).body):
        return False
    eid = ent.id
    arts = [a for (a,) in db.execute(select(ArticleEntity.article_id).where(ArticleEntity.entity_id == eid))]
    meta = {r[0]: r[1:] for r in db.execute(select(NewsItem.id, NewsItem.published_at, NewsItem.dup_of, NewsItem.kind, NewsItem.user_id, NewsItem.source_key).where(NewsItem.id.in_(arts or [0])))}
    rels = [r for (r,) in db.execute(select(Relationship.id).where((Relationship.subject_id == eid) | (Relationship.object_id == eid)))]
    claims = [c for (c,) in db.execute(select(Claim.id).where(Claim.entity_id == eid))]
    if rels:
        db.execute(delete(Evidence).where(Evidence.relationship_id.in_(rels)))
        db.execute(delete(Relationship).where(Relationship.id.in_(rels)))
    if claims:
        db.execute(delete(Evidence).where(Evidence.claim_id.in_(claims)))
        db.execute(delete(Claim).where(Claim.id.in_(claims)))
    for table in (ArticleEntity, MentionCandidate, EntityCommunity, EntityCard, EntityAlias, EntityNote):
        db.execute(delete(table).where(table.entity_id == eid))
    analytics.apply_entity_deltas(db, [(eid, *meta[a], -1) for a in arts if a in meta])
    db.add(EntityRetirement(name=ent.name[:200], key=ent.key, type=ent.type, reason=reason[:20], article_count=len(arts), claim_count=len(claims), relation_count=len(rels),
                            retired_at=datetime.now(_UTC)))
    db.execute(delete(Entity).where(Entity.id == eid))
    db.flush()
    searchindex.reindex_ids(db.connection(), [a for a in arts if a in meta])
    return True


def _source_named(db: Session) -> dict[int, str]:
    """出どころの名前と同名の物のうち、結び付いた記事がすべて「その出どころの記事で、題に名前が出ない」もの（取得元が物になったもの）。{物の id: "source"}。
    実在の物（OpenAI など）は他の出どころの記事にも出るので残る。"""
    cores: dict[str, set[str]] = {}
    for (src,) in db.execute(select(NewsItem.source).distinct()):
        for k in source_cores(src or ""):
            cores.setdefault(k, set()).add(src)
    if not cores:
        return {}
    out: dict[int, str] = {}
    for ent in db.scalars(select(Entity).where(Entity.key.in_(list(cores)), Entity.sensitivity == "public", Entity.type.in_(("org", "other")))):      # 人・ソフト・モデルなどは、出どころと同名でも実在の物（自分のブログを持つ）
        if not _MEDIA_WORD.search(unicodedata.normalize("NFKC", ent.name)) and ent.type == "org":
            continue      # 媒体らしい語（Watch・News・Blog・新聞…）の無い組織は、自分の発表の出どころでも実在の物（発表する側の組織）。残す
        rows = db.execute(select(NewsItem.source, NewsItem.title).join(ArticleEntity, ArticleEntity.article_id == NewsItem.id).where(ArticleEntity.entity_id == ent.id)).all()
        if rows and all(ent.key in source_cores(src or "") and not _in_title(ent.name, title or "") for src, title in rows):
            out[ent.id] = "source"
    return out


def retire_sweep(db: Session, limit: int = MAX_RETIRE_PER_RUN) -> dict:
    """夜: 文脈なしで落とせる物（媒体名・一般語・断片）と、出どころの名前になっている物を外す。記事の少ない物から（外しても被害が小さい）。{"retired", "by_reason"}。"""
    done = 0
    by: dict[str, int] = {}
    sourced = _source_named(db)
    for ent in db.scalars(select(Entity).where(Entity.sensitivity == "public").order_by(Entity.article_count, Entity.id)):
        if done >= limit:
            break
        why = reject(ent.name, ent.type, context=False) or sourced.get(ent.id, "")
        if why and retire(db, ent, why):
            done += 1
            by[why] = by.get(why, 0) + 1
    return {"retired": done, "by_reason": by}


def summary(db: Session) -> dict:
    rows = db.execute(select(EntityRetirement.reason, func.count()).group_by(EntityRetirement.reason)).all()
    return {"retired": int(sum(n for _, n in rows)), "by_reason": {r: int(n) for r, n in rows}}
