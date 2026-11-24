"""要素の取り出し（entities・facts・relations）の形の検査と整え。下ごしらえ（prep.py）の 1 回の呼び出しの返事から、記事ごとに取り出す。

- 返事は信用しない（記事の中の命令に従わされた出力・壊れた JSON もありうる）: JSON Schema（下の *_SCHEMA）で 1 件ずつ検査し、合わない物・事実だけを捨てる。
  記事そのものを落とさない（概要 ja が良ければ、その記事の下ごしらえは済みにする）。
- 件数の上限: entities は記事ごとに最大 8、facts は最大 5（スキーマの maxItems。超えた分は捨てる）。
- **原文に無い数字を含む fact は捨てる**: fact の text と value に出る数字は、LLM に送った原文（題と抜粋）に同じ数字がある物だけ（global な幻覚の防止）。
  日本語の漢数字・単位の言い換え（1.5 billion → 15 億）は照合できないので、安全側で捨てる。
- **型つきの関係（v0.5）**: 同じ 1 回の呼び出しで relations（最大 5。{"subject", "type", "object", "quote"}）も返させる。subject・object は物の名前、quote は原文（題・抜粋）から写した短い引用。
  引用が原文にそのまま（空白・全角半角を寄せて）ある関係だけ grounded=True（kg が「原文に根拠のある取り出し」として入れる）。引用が無い・原文に無い関係は grounded=False（「推測」として確かさを低く入れ、確定にしない）。
  物の名前が空・主語と目的語が同じ・型が違う関係は捨てる。一緒に出る（co_occurs）は LLM に作らせない（集計で作る）。
- 正規化のキー（normalize_key）: 全角半角（NFKC）を寄せ・小文字にして・記号と空白を除く（"GPT-4o"・"ＧＰＴ 4o"・"gpt4o" は同じ物）。
"""
from __future__ import annotations

import re
import unicodedata

PROMPT_VERSION = "ex2"        # claims.prompt_version に残す。プロンプトの取り出しの指示を変えたら上げる
EXTRACT_VERSION = 1           # news_items.extract_v。0 は未
MAX_ENTITIES = 8
MAX_FACTS = 5
MAX_RELATIONS = 5
QUOTE_MIN, QUOTE_MAX = 4, 60
REL_TYPES = ("uses", "supports", "replaces", "competes", "part_of", "implements", "cites", "contradicts", "other")      # LLM が作れる型。co_occurs は集計だけが作る
ENTITY_TYPES = ("model", "software", "org", "person", "tech", "product", "game", "policy", "place", "other")
FACT_KINDS = ("release", "benchmark", "price", "date", "claim", "other")

ENTITY_SCHEMA: dict = {
    "type": "object", "required": ["name", "type"],
    "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 80}, "type": {"type": "string", "enum": list(ENTITY_TYPES)}},
}
FACT_SCHEMA: dict = {
    "type": "object", "required": ["about", "kind", "text"],
    "properties": {"about": {"type": "string", "minLength": 1, "maxLength": 80}, "kind": {"type": "string", "enum": list(FACT_KINDS)},
                   "text": {"type": "string", "minLength": 1, "maxLength": 200}, "value": {"type": "string", "maxLength": 40}},
}
RELATION_SCHEMA: dict = {
    "type": "object", "required": ["subject", "type", "object"],
    "properties": {"subject": {"type": "string", "minLength": 1, "maxLength": 80}, "type": {"type": "string", "enum": list(REL_TYPES)},
                   "object": {"type": "string", "minLength": 1, "maxLength": 80}, "quote": {"type": "string", "maxLength": QUOTE_MAX}},
}
ARTICLE_SCHEMA: dict = {      # 記事 1 件ぶんの取り出し（プロンプトの「形」と同じ。LLM へは文章で伝える）
    "type": "object",
    "properties": {"entities": {"type": "array", "maxItems": MAX_ENTITIES, "items": ENTITY_SCHEMA},
                   "facts": {"type": "array", "maxItems": MAX_FACTS, "items": FACT_SCHEMA},
                   "relations": {"type": "array", "maxItems": MAX_RELATIONS, "items": RELATION_SCHEMA}},
}

_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def validate(value: object, schema: dict) -> bool:
    """JSON Schema の部分集合（type・enum・required・properties・items・minLength・maxLength・maxItems）で検査する。依存を増やさない小さな実装。"""
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            return False
        if any(k not in value for k in schema.get("required", ())):
            return False
        return all(validate(value[k], sub) for k, sub in schema.get("properties", {}).items() if k in value)
    if kind == "array":
        if not isinstance(value, list) or len(value) > schema.get("maxItems", 10**9):
            return False
        return all(validate(v, schema["items"]) for v in value) if "items" in schema else True
    if kind == "string":
        if not isinstance(value, str) or not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 10**9):
            return False
        return "enum" not in schema or value in schema["enum"]
    return True


def clean_text(value: object, limit: int) -> str:
    """制御文字（改行を含む）を空白にし、続く空白を 1 つにして、前後を切る。文字数が上限を超えたら切る（見出しの語などの長すぎる返事の対策）。"""
    if value is None or isinstance(value, (dict, list)):
        return ""
    text = re.sub(r"\s+", " ", _CONTROL.sub(" ", str(value))).strip()
    return text[:limit]


def normalize_key(name: str) -> str:
    """物の正規化のキー。NFKC で全角半角を寄せ、小文字にし、文字と数字だけを残す（記号・空白を除く）。空になる名前は ""。"""
    text = unicodedata.normalize("NFKC", str(name or "")).casefold()
    return "".join(ch for ch in text if ch.isalnum())[:200]


def numbers_in(text: str) -> set[str]:
    """文中の数字の並び（NFKC で全角を半角に。桁区切りのカンマは除く）。"1,000" → "1000"・"3.5" → "3.5"。"""
    return {m.replace(",", "") for m in _NUMBER.findall(unicodedata.normalize("NFKC", str(text or "")))}


def numbers_grounded(fact_text: str, value: str, source_text: str) -> bool:
    """fact に出る数字が、すべて原文にあるか。数字が無い fact は通す。"""
    wanted = numbers_in(f"{fact_text} {value}")
    return not wanted or wanted <= numbers_in(source_text)


def fold(text: str) -> str:
    """引用の照合用: NFKC・小文字・空白を除く。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text or "")).casefold())


def quote_grounded(quote: str, source_text: str) -> bool:
    """引用が原文（LLM に送った題と抜粋）にそのまま出るか。短すぎる引用（QUOTE_MIN 文字未満）は根拠にしない。"""
    q = fold(quote)
    return len(q) >= QUOTE_MIN and q in fold(source_text)


def clean_relations(rows: object, source_text: str) -> list[dict]:
    """relations を 1 件ずつ検査して整える（最大 MAX_RELATIONS）。同じ（主語・型・目的語）は 1 つ。"""
    out: list[dict] = []
    seen: set[tuple] = set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        item = {"subject": clean_text(row.get("subject"), 200), "type": clean_text(row.get("type"), 16).lower(), "object": clean_text(row.get("object"), 200),
                "quote": clean_text(row.get("quote"), QUOTE_MAX + 1)}
        a, b = normalize_key(item["subject"]), normalize_key(item["object"])
        if not validate(item, RELATION_SCHEMA) or not a or not b or a == b or (a, item["type"], b) in seen:
            continue
        seen.add((a, item["type"], b))
        grounded = quote_grounded(item["quote"], source_text)
        out.append({**item, "quote": item["quote"] if grounded else "", "grounded": grounded})
        if len(out) >= MAX_RELATIONS:
            break
    return out


def clean(raw: object, source_text: str) -> dict | None:
    """記事 1 件の返事（dict）から {"entities": [...], "facts": [...]} を作る。entities も facts も無い（キー自体が無い・形が違う）なら None（取り出せなかった）。
    物は name・type が合う物だけ、同じ正規化のキーは 1 つにして、最大 8。fact は about・kind・text が合い、数字が原文にある物だけ、最大 5。"""
    if not isinstance(raw, dict):
        return None
    ents_raw, facts_raw = raw.get("entities"), raw.get("facts")
    if not isinstance(ents_raw, list) and not isinstance(facts_raw, list):
        return None
    entities: list[dict] = []
    seen: set[str] = set()
    for row in ents_raw if isinstance(ents_raw, list) else []:
        if not isinstance(row, dict):
            continue
        item = {"name": clean_text(row.get("name"), 200), "type": clean_text(row.get("type"), 16).lower()}
        key = normalize_key(item["name"])
        if not key or key in seen or not validate(item, ENTITY_SCHEMA):
            continue
        seen.add(key)
        entities.append(item)
        if len(entities) >= MAX_ENTITIES:
            break
    facts: list[dict] = []
    for row in facts_raw if isinstance(facts_raw, list) else []:
        if not isinstance(row, dict):
            continue
        value = row.get("value")
        item = {"about": clean_text(row.get("about"), 200), "kind": clean_text(row.get("kind"), 16).lower(), "text": clean_text(row.get("text"), 400),
                "value": clean_text(value, 40) if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""}
        if not validate(item, FACT_SCHEMA) or not normalize_key(item["about"]):
            continue
        if not numbers_grounded(item["text"], item["value"], source_text):
            continue
        facts.append(item)
        if len(facts) >= MAX_FACTS:
            break
    return {"entities": entities, "facts": facts, "relations": clean_relations(raw.get("relations"), source_text)}
