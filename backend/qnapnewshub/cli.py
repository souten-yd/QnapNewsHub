"""コマンド: ./hub.sh create-admin（最初の管理者）・./hub.sh run（待ち受けの設定を読んで起動）・./hub.sh set-openrouter-key・./hub.sh llm-try（取り出しを 1 回だけ流す）。"""
from __future__ import annotations

import argparse
import getpass
import sys


def _create_admin(args: argparse.Namespace) -> int:
    from qnapnewshub import auth

    username = args.username or input("利用者名: ").strip()
    password = getpass.getpass("パスワード（12 文字以上）: ")
    if password != getpass.getpass("もう一度: "):
        print("パスワードが一致しません", file=sys.stderr)
        return 2
    try:
        auth.create_admin(username, password)
    except (ValueError, PermissionError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"管理者 {username} を作りました")
    return 0


def _set_key(args: argparse.Namespace) -> int:
    from qnapnewshub import llm

    try:
        llm.set_key(getpass.getpass("OpenRouter の鍵: "))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("鍵を暗号化して保存しました")
    return 0


def _llm_try(args: argparse.Namespace) -> int:
    from qnapnewshub import llm_try

    return llm_try.run(args.provider, args.base, args.model, args.items)


def _numeric_try(args: argparse.Namespace) -> int:
    from qnapnewshub import numeric_try

    return numeric_try.run(args.provider, args.base, args.model, args.items)


def _map_layout(args: argparse.Namespace) -> int:
    from qnapnewshub import mapgrav, writer

    info = mapgrav.compute_and_save()
    writer.shutdown()
    print(f"地図の配置を保存しました: {info['nodes']} 点・{info['seconds']} 秒・版 {info['version']}")
    return 0


def _run(args: argparse.Namespace) -> int:
    import uvicorn

    from qnapnewshub import config

    from qnapnewshub import settings

    host, port = config.listen()
    access = bool(settings.load()["storage"]["access_log"])      # アクセスのログは既定で切（書き込みを増やさない）
    print(f"待ち受け: http://{host}:{port}")
    uvicorn.run("qnapnewshub.main:app", host=host, port=port, log_level="warning", access_log=access)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hub.sh")
    sub = parser.add_subparsers(dest="cmd", required=True)
    admin = sub.add_parser("create-admin", help="最初の管理者を作る")
    admin.add_argument("--username", default="")
    admin.set_defaults(func=_create_admin)
    sub.add_parser("set-openrouter-key", help="OpenRouter の鍵を暗号化して保存する").set_defaults(func=_set_key)
    try_p = sub.add_parser("llm-try", help="取り出しを 1 回だけ流す（本物の LLM の評価用。鍵は環境変数 NEWSHUB_LLM_TRY_KEY）")
    try_p.add_argument("--provider", choices=("gateway", "openrouter"), default="gateway")
    try_p.add_argument("--base", default="", help="ゲートウェイの基準の URL")
    try_p.add_argument("--model", required=True)
    try_p.add_argument("--items", type=int, default=10)
    try_p.set_defaults(func=_llm_try)
    nt = sub.add_parser("numeric-try", help="数値パスを流して歩留まりを見る（何も書かない。鍵は環境変数 NEWSHUB_LLM_TRY_KEY か置き場の secretbox）")
    nt.add_argument("--provider", choices=("gateway", "openrouter"), default="gateway")
    nt.add_argument("--base", default="", help="ゲートウェイの基準の URL（省略すると設定の値）")
    nt.add_argument("--model", required=True)
    nt.add_argument("--items", type=int, default=30, help="読む記事の件数（新しい順。候補の文のある記事だけを送る）")
    nt.set_defaults(func=_numeric_try)
    sub.add_parser("map-layout", help="知識の地図の配置（ForceAtlas2）と重要度を今すぐ計算して保存する（夜の仕事と同じ）").set_defaults(func=_map_layout)
    sub.add_parser("run", help="起動する").set_defaults(func=_run)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
