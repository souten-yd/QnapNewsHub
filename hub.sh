#!/usr/bin/env bash
# QNAP ニュースハブの起動と試験の台本。
#   ./hub.sh setup          .venv を作って依存を入れる（版は backend/pyproject.toml で固定）
#   ./hub.sh test [args]    試験（pytest）
#   ./hub.sh run            127.0.0.1:8787 で起動（NEWSHUB_HOST / NEWSHUB_PORT か設定の API で変える）
#   ./hub.sh create-admin   最初の管理者を作る（パスワードは対話で入力）
#   ./hub.sh set-openrouter-key  OpenRouter の鍵を暗号化して保存
#   ./hub.sh <cli のコマンド>  python -m qnapnewshub.cli へそのまま渡す
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$ROOT/.venv"
PY="$VENV/bin/python"

need_venv() {
  [ -x "$PY" ] || { echo "先に ./hub.sh setup を実行してください" >&2; exit 1; }
}

cmd="${1:-help}"
shift || true
case "$cmd" in
  setup)
    python3.12 -m venv "$VENV" 2>/dev/null || python3 -m venv "$VENV"
    "$PY" -m pip install --quiet --upgrade pip
    "$PY" -m pip install --quiet -e "$ROOT/backend[dev]"
    echo "準備ができました: $VENV"
    ;;
  test)
    need_venv
    cd "$ROOT/backend"
    exec "$PY" -m pytest "$@"
    ;;
  run)
    need_venv
    cd "$ROOT/backend"
    exec "$PY" -m qnapnewshub.cli run
    ;;
  help|-h|--help)
    sed -n '2,10p' "${BASH_SOURCE[0]}"
    ;;
  *)
    need_venv
    cd "$ROOT/backend"
    exec "$PY" -m qnapnewshub.cli "$cmd" "$@"
    ;;
esac
