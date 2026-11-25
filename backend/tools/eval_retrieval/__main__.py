"""`./hub.sh eval-retrieval --data <評価用の DB の置き場（コピー）> --questions <json> --out <md> [--build] [--per-type 10] [--raw <json>]`

--data の下の news/news.sqlite3 を使う（呼ぶ側がコピーしたもの。ここでは読むだけではなく、ハブを起動するので開いたときの更新は入る。本物の置き場を指さないこと）。
--build を付けると、--questions に問いの集めを作ってから流す。--build-bridge は橋渡しの問い（経路 2〜6 段）を、--build-short は 3 文字未満の語の問いを作る。外へ通信しない・LLM は呼ばない。
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="eval-retrieval")
    ap.add_argument("--data", required=True, help="評価用の置き場（news/news.sqlite3 を含む。本物の置き場は指さない）")
    ap.add_argument("--questions", required=True, help="問いの集めの JSON（--build で作る）")
    ap.add_argument("--out", required=True, help="結果の Markdown")
    ap.add_argument("--build", action="store_true", help="問いの集めを作ってから流す")
    ap.add_argument("--per-type", type=int, default=10)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--build-bridge", action="store_true", help="橋渡しの問い（経路 2〜6 段の 2 物。--questions に作る。評価の置き場へ。リポジトリに入れない）を作ってから流す")
    ap.add_argument("--build-tools", action="store_true", help="変化（watch）・条件つきの数値の比較（bench）の問いを作る（--questions に。評価の置き場へ。リポジトリに入れない）")
    ap.add_argument("--build-short", action="store_true", help="3 文字未満の語の問い（語だけ・別の語＋語。--per-type 問いずつ。--questions に。評価の置き場へ。リポジトリに入れない）を作る")
    ap.add_argument("--per-length", type=int, default=20, help="橋渡しの問いの、経路の長さごとの数")
    ap.add_argument("--meet-depth", default="auto", help="③' の会合の展開の深さ（auto＝問いで決める・1〜5＝固定）")
    ap.add_argument("--raw", default="", help="問いごとの生の点数の JSON の出力先（任意）")
    a = ap.parse_args(argv)
    data = Path(a.data).expanduser().resolve()
    db_path = data / "news" / "news.sqlite3"
    if not db_path.is_file():
        print(f"{db_path} がありません（--data は news/news.sqlite3 を含む置き場）", file=sys.stderr)
        return 2
    default = (Path.home() / ".local" / "share" / "qnapnewshub").resolve()
    if os.environ.get("NEWSHUB_ALLOW_REAL_DATA") != "1" and data == default:
        print("本物の置き場は指せません。コピーを --data に渡してください", file=sys.stderr)
        return 2
    os.environ["NEWSHUB_DATA"] = str(data)
    os.environ["NEWSHUB_AUTO"] = "0"      # 取得・まとめ書きの見張りは動かさない（外へ通信しない）
    qpath = Path(a.questions).expanduser()
    from . import build_questions, checks, report, runner

    if a.build_short:
        d = build_questions.main_short(db_path, qpath, a.per_type, a.seed)
        print("3 文字未満の語の問いを作りました:", d["counts"])
        checks.warn_dangling(db_path, d["questions"], "作るとき")
    elif a.build_tools:
        d = build_questions.main_tools(db_path, qpath, a.per_type, a.seed)
        print("変化・比較の問いを作りました:", d["counts"])
        checks.warn_dangling(db_path, d["questions"], "作るとき")
    elif a.build_bridge:
        d = build_questions.main_bridge(db_path, qpath, a.per_length, a.seed)
        print("橋渡しの問いを作りました:", d["counts"])
        checks.warn_dangling(db_path, d["questions"], "作るとき")
    elif a.build:
        d = build_questions.main(db_path, qpath, a.per_type, a.seed)
        print("問いを作りました:", d["counts"])
        checks.warn_dangling(db_path, d["questions"], "作るとき")
    questions = json.loads(qpath.read_text(encoding="utf-8"))["questions"]
    checks.warn_dangling(db_path, questions, "測るとき")

    import sqlite3
    from fastapi.testclient import TestClient
    from qnapnewshub.main import app

    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cnt = lambda t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0]  # noqa: E731
    info = {"db": f"記事 {cnt('news_items')}・物 {cnt('entities')}・主張 {cnt('claims')}・関係 {cnt('relationships')}・イベント {cnt('events')}"}
    c.close()
    with TestClient(app) as client:
        pw = "-".join(["ev", secrets.token_hex(8)])
        user = "evalbot-" + secrets.token_hex(3)
        r = client.post("/api/v1/auth/setup", json={"username": user, "password": pw})
        if r.status_code != 200:      # すでに管理者がいる置き場（コピーの使い回し）: 評価用の利用者を足してログインする
            from qnapnewshub import auth

            auth.create_user(user, pw)
            r = client.post("/api/v1/auth/login", json={"username": user, "password": pw})
            if r.status_code != 200:
                print("評価用の利用者でログインできません:", r.text[:200], file=sys.stderr)
                return 2
        meet_depth = None if a.meet_depth == "auto" else int(a.meet_depth)
        rows = runner.run_all(client, db_path, questions, meet_depth=meet_depth, progress=lambda i, n: print(f"\r{i}/{n}", end="", file=sys.stderr))
    print(file=sys.stderr)
    out = Path(a.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.render(rows, info), encoding="utf-8")
    if a.raw:
        Path(a.raw).expanduser().write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    print(f"結果: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
