"""ControlDeck から受け取る生成物（画像）と調査の記録。ARCHITECTURE 5 章・13 章。

- **画像の検査**（外から来るバイト列は信じない）: 種類は webp・png・jpeg だけ（Content-Type と中身の先頭が合うこと）・512KB 以下・構造を最後まで見る
  （PNG は各 chunk の長さ・CRC・IDAT・IEND、JPEG は SOI〜EOI と SOF の寸法、WebP は RIFF の長さと VP8/VP8L/VP8X の寸法）・幅と高さは 64〜4096。
  SVG・GIF・その他は断る。画像をデコードしない（依存を増やさず、デコード爆弾にもならない）。
- **hash で重複管理**: 中身の sha256 が名前。同じ中身は 1 つだけ保存（表 artifacts）。ファイルは data_dir/artifacts/images/<sha256>.<ext>（書き込みは Write Coordinator）。
- **記事の画像の差し替え**: 記事に元の画像（AI 生成でないもの）があるときは断る（replace=true で上書き）。差し替えたら image_url を内部の道（/api/v1/artifacts/images/…）にし、
  image_ai=1（画面に「AI 生成のイメージ」の印）。保存してから返す（バッファに残さない）。
- 配る道（GET /artifacts/images/<sha256>.<ext>）は名前を正規表現で固定し、resolve() が置き場の中かを確かめ、nosniff・固定の Content-Type で返す。
- 調査の記録は保存だけ（検索・再利用は v0.6）。大きさを制限し、内容の sha256 で二重に入れない。
"""
from __future__ import annotations

import hashlib
import json
import re
import struct
import zlib
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from qnapnewshub import db as news_db
from qnapnewshub import writer
from qnapnewshub.config import data_dir
from qnapnewshub.db import Artifact, NewsItem, ResearchRecord

MAX_IMAGE_BYTES = 512 * 1024
MIN_SIDE, MAX_SIDE = 64, 4096
URL_PREFIX = "/api/v1/artifacts/images/"
TYPES = {"image/webp": "webp", "image/png": "png", "image/jpeg": "jpg"}
NAME = re.compile(r"^([0-9a-f]{64})\.(webp|png|jpg)$")
MEDIA = {"webp": "image/webp", "png": "image/png", "jpg": "image/jpeg"}


class ArtifactError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def images_dir() -> Path:
    return data_dir() / "artifacts" / "images"


# ---- 画像の構造の検査 ----

def _png(data: bytes) -> tuple[int, int]:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("PNG の印がありません")
    pos, width, height, idat, first = 8, 0, 0, False, True
    while pos + 12 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        end = pos + 12 + length
        if end > len(data):
            raise ValueError("PNG の chunk が途中で切れています")
        body = data[pos + 8:pos + 8 + length]
        if struct.unpack(">I", data[end - 4:end])[0] != zlib.crc32(kind + body):
            raise ValueError("PNG の CRC が合いません")
        if first:
            if kind != b"IHDR" or length != 13:
                raise ValueError("PNG の最初が IHDR ではありません")
            width, height = struct.unpack(">II", body[:8])
            first = False
        elif kind == b"IDAT":
            idat = True
        elif kind == b"IEND":
            if end != len(data) or not idat:
                raise ValueError("PNG の終わりが正しくありません")
            return width, height
        pos = end
    raise ValueError("PNG に IEND がありません")


def _jpeg(data: bytes) -> tuple[int, int]:
    if data[:2] != b"\xff\xd8" or data[-2:] != b"\xff\xd9":
        raise ValueError("JPEG の SOI/EOI がありません")
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            raise ValueError("JPEG のマーカーが壊れています")
        marker = data[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        if marker in (0x01, *range(0xD0, 0xD8)):
            pos += 2
            continue
        length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        if length < 2 or pos + 2 + length > len(data):
            raise ValueError("JPEG の区間が途中で切れています")
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height, width = struct.unpack(">HH", data[pos + 5:pos + 9])
            return width, height
        if marker == 0xDA:
            break
        pos += 2 + length
    raise ValueError("JPEG の寸法が見つかりません")


def _webp(data: bytes) -> tuple[int, int]:
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP" or struct.unpack("<I", data[4:8])[0] + 8 != len(data):
        raise ValueError("WebP の RIFF が正しくありません")
    kind, body = data[12:16], data[20:]
    if kind == b"VP8 ":
        if body[3:6] != b"\x9d\x01\x2a":
            raise ValueError("WebP（VP8）の印がありません")
        w, h = struct.unpack("<HH", body[6:10])
        return w & 0x3FFF, h & 0x3FFF
    if kind == b"VP8L":
        if body[0] != 0x2F:
            raise ValueError("WebP（VP8L）の印がありません")
        bits = struct.unpack("<I", body[1:5])[0]
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if kind == b"VP8X":
        return int.from_bytes(body[4:7], "little") + 1, int.from_bytes(body[7:10], "little") + 1
    raise ValueError("WebP の種類が正しくありません")


def inspect_image(data: bytes, content_type: str) -> dict:
    """{"ext", "width", "height", "sha256", "size"}。合わなければ ArtifactError（415・413・422）。"""
    ext = TYPES.get((content_type or "").split(";")[0].strip().lower())
    if ext is None:
        raise ArtifactError(415, "画像は webp・png・jpeg だけです")
    if not data:
        raise ArtifactError(422, "画像が空です")
    if len(data) > MAX_IMAGE_BYTES:
        raise ArtifactError(413, "画像は 512KB までです")
    try:
        width, height = {"png": _png, "jpg": _jpeg, "webp": _webp}[ext](data)
    except (ValueError, struct.error, IndexError) as exc:
        raise ArtifactError(422, f"画像の形が正しくありません（{exc}）") from None
    if not (MIN_SIDE <= width <= MAX_SIDE and MIN_SIDE <= height <= MAX_SIDE):
        raise ArtifactError(422, f"画像の幅・高さは {MIN_SIDE}〜{MAX_SIDE} です（{width}x{height}）")
    return {"ext": ext, "width": width, "height": height, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def store_image(item_id: int, data: bytes, content_type: str, peer_id: int, replace: bool = False) -> dict:
    info = inspect_image(data, content_type)
    sha, ext = info["sha256"], info["ext"]
    path = images_dir() / f"{sha}.{ext}"
    url = f"{URL_PREFIX}{sha}.{ext}"
    coord = writer.get()
    if coord.has_pending_id(item_id):      # まだ書いていない新しい記事: 先に保存する（外部キー・差分の保存済みだけ）
        writer.flush("action")
    with news_db.session() as db:
        row = db.get(NewsItem, item_id)
        if row is None or row.kind == "event":
            raise ArtifactError(404, "その記事はありません")
        if row.image_url and not row.image_ai and not replace:
            raise ArtifactError(409, "元の画像があります（replace=true で差し替えます）")
        same = row.image_url == url
    duplicate = path.exists() and path.stat().st_size == len(data)
    if not duplicate:
        writer.write_file(path, data, name="artifact.image", mode=0o644)

    def record() -> None:
        with news_db.session() as db:
            if db.get(Artifact, sha) is None:
                db.add(Artifact(sha256=sha, kind="image", ext=ext, size=len(data), width=info["width"], height=info["height"], peer_id=peer_id, created_at=datetime.now(timezone.utc)))
                db.commit()
    writer.run("news.artifact", record, user=False)
    if not same:
        coord.patch_item(item_id, set={"image_url": url, "image_ai": True, "image_tried": True})
        writer.flush("action")      # 差し替えは保存してから返す
    return {"ok": True, "sha256": sha, "url": url, "width": info["width"], "height": info["height"], "bytes": len(data), "duplicate": duplicate and same}


def image_path(name: str) -> tuple[Path, str] | None:
    """配る画像のファイル（名前が正規表現に合い、置き場の中にあるときだけ）。"""
    m = NAME.match(name or "")
    if not m:
        return None
    root = images_dir().resolve()
    path = (root / name).resolve()
    if root not in path.parents or not path.is_file():
        return None
    return path, MEDIA[m.group(2)]


# ---- 調査の記録 ----

LIMITS = {"theme": 200, "conclusion": 8000, "sources": 50, "facts": 100, "fact": 500, "title": 200, "url": 1000}


def save_research(peer_id: int, theme: str, sources: list[dict], facts: list[str], conclusion: str) -> dict:
    body = json.dumps({"theme": theme, "sources": sources, "facts": facts, "conclusion": conclusion}, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()

    def do() -> dict:
        with news_db.session() as db:
            found = db.scalar(select(ResearchRecord).where(ResearchRecord.digest == digest))
            if found is not None:
                return {"ok": True, "id": found.id, "duplicate": True}
            rec = ResearchRecord(digest=digest, peer_id=peer_id, theme=theme, sources=json.dumps(sources, ensure_ascii=False), facts=json.dumps(facts, ensure_ascii=False),
                                 conclusion=conclusion)
            db.add(rec)
            db.commit()
            return {"ok": True, "id": rec.id, "duplicate": False}
    return writer.run("news.research", do, user=False)
