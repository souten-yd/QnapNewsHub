"""コマンド: ./hub.sh create-admin（最初の管理者）・./hub.sh run（待ち受けの設定を読んで起動）・./hub.sh set-openrouter-key。"""
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
    sub.add_parser("run", help="起動する").set_defaults(func=_run)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
