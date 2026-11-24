#!/usr/bin/env bash
# QNAP ニュースハブの起動と試験の台本。
#   ./hub.sh setup          .venv を作って依存を入れる（版は backend/pyproject.toml で固定）
#   ./hub.sh test [args]    試験（pytest）
#   ./hub.sh build          画面を build する（frontend で npm ci → tsc → vite build。できた frontend/dist をバックエンドが / 以下で配る）
#   ./hub.sh icons          アイコン（frontend/icons/*.svg → frontend/public/*.png・packaging/qpkg/icons/*.png）を作り直す。PLAYWRIGHT_BROWSERS_PATH のブラウザで描く
#   ./hub.sh e2e [args]     画面の E2E（Playwright）。一時の置き場でバックエンドを起こして流す（本物のデータには触れない）。
#                           ブラウザは PLAYWRIGHT_BROWSERS_PATH のものを使う（install はしない）。撮影は NEWSHUB_SHOTS_DIR にも写す
#   ./hub.sh qpkg           QNAP の QPKG（x86_64）を作る → dist/QnapNewsHub_<版>_x86_64.qpkg（packaging/qpkg/build.sh。podman と QDK を使う）
#   ./hub.sh release <版> [--prerelease] [--notes ノートのファイル] [--dry-run]
#                           版の書き換えの確認 → test → build → qpkg → 署名（packaging/release/sign.py。秘密鍵は NEWSHUB_SIGNING_KEY・既定 ~/.keys/qnapnewshub-release.pem）
#                           → gh release create。--dry-run は最後の gh release create を表示するだけ（docs/UPDATE.md）
#   ./hub.sh run            127.0.0.1:8787 で起動（NEWSHUB_HOST / NEWSHUB_PORT か設定の API で変える）
#   ./hub.sh install-service   systemd のユーザーの単位 qnapnewshub.service を ~/.config/systemd/user に書いて有効にする（root は使わない。停電のあと自動で起きる）
#   ./hub.sh uninstall-service 止めて、無効にして、単位を消す（データの置き場は消さない）
#   ./hub.sh print-service     単位の中身だけを表示する（何も書かない）
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
  build)
    command -v npm >/dev/null || { echo "npm が要ります（Node.js 22）" >&2; exit 1; }
    cd "$ROOT/frontend"
    npm ci --no-audit --no-fund
    exec npm run build
    ;;
  qpkg)
    need_venv
    exec "$ROOT/packaging/qpkg/build.sh"
    ;;
  icons)
    cd "$ROOT/frontend"
    [ -d node_modules ] || npm ci --no-audit --no-fund
    exec node scripts/make-icons.mjs
    ;;
  e2e)
    need_venv
    command -v npm >/dev/null || { echo "npm が要ります（Node.js 22）" >&2; exit 1; }
    "$0" build
    cd "$ROOT/frontend"
    npx tsc -p tsconfig.e2e.json
    E2E_PORT="${NEWSHUB_E2E_PORT:-18787}"
    E2E_TMP="$(mktemp -d "${TMPDIR:-/tmp}/newshub-e2e-XXXXXX")"
    [ -n "$E2E_TMP" ] && [ -d "$E2E_TMP" ] || { echo "一時の置き場を作れませんでした" >&2; exit 1; }
    SERVER_PID=""
    cleanup() {
      if [ -n "$SERVER_PID" ]; then
        kill "$SERVER_PID" 2>/dev/null || true
        for _ in $(seq 1 20); do kill -0 "$SERVER_PID" 2>/dev/null || break; sleep 0.25; done
        kill -9 "$SERVER_PID" 2>/dev/null || true      # 止まらなければ強制（次の実行のポートを塞がない）
      fi
      rm -rf -- "${E2E_TMP:?}"
    }
    trap cleanup EXIT
    trap 'exit 130' INT TERM HUP PIPE      # 途中で切られても、起こしたバックエンドと一時の置き場を残さない
    if curl -fsS "http://127.0.0.1:$E2E_PORT/api/v1/health" >/dev/null 2>&1; then
      echo "ポート $E2E_PORT はすでに使われています（NEWSHUB_E2E_PORT で変えられます）" >&2
      exit 1
    fi
    # 本番とは別の置き場・別のポートで起こす（外への取得の見張りは動かさない）
    export NEWSHUB_DATA="$E2E_TMP/data" NEWSHUB_AUTO=0 NEWSHUB_HOST=127.0.0.1 NEWSHUB_PORT="$E2E_PORT"
    (cd "$ROOT/backend" && "$PY" "$ROOT/frontend/e2e/seed_backend.py")
    cd "$ROOT/backend"
    "$PY" -m qnapnewshub.cli run >"$E2E_TMP/server.log" 2>&1 &
    SERVER_PID=$!
    cd "$ROOT/frontend"
    for _ in $(seq 1 60); do
      curl -fsS "http://127.0.0.1:$E2E_PORT/api/v1/health" >/dev/null 2>&1 && break
      sleep 0.5
    done
    curl -fsS "http://127.0.0.1:$E2E_PORT/api/v1/health" >/dev/null || { echo "試験用のバックエンドが起きませんでした" >&2; cat "$E2E_TMP/server.log" >&2; exit 1; }
    set +e
    NEWSHUB_E2E_URL="http://127.0.0.1:$E2E_PORT" npx playwright test "$@"
    status=$?
    set -e
    exit $status
    ;;
  release)
    need_venv
    VER="${1:-}"; shift || true
    [ -n "$VER" ] || { echo "使い方: ./hub.sh release <版> [--prerelease] [--notes ファイル] [--dry-run]" >&2; exit 1; }
    echo "$VER" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$' || { echo "版の形が正しくありません（例 0.1.1・0.2.0-rc1）: $VER" >&2; exit 1; }
    PRE=""; NOTES=""; DRY=0
    while [ $# -gt 0 ]; do
      case "$1" in
        --prerelease) PRE="--prerelease"; shift ;;
        --notes) NOTES="${2:-}"; [ -f "$NOTES" ] || { echo "ノートのファイルがありません: $NOTES" >&2; exit 1; }; shift 2 ;;
        --dry-run) DRY=1; shift ;;
        *) echo "知らない引数です: $1" >&2; exit 1 ;;
      esac
    done
    # 1. 版の書き換えの確認（コード・パッケージ・画面の版がそろっていること。QPKG の版は build が __version__ から作る）
    CODE_VER="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/backend/qnapnewshub/__init__.py")"
    PROJ_VER="$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/backend/pyproject.toml" | head -n 1)"
    WEB_VER="$(sed -n 's/^  "version": "\(.*\)",/\1/p' "$ROOT/frontend/package.json" | head -n 1)"
    for v in "$CODE_VER" "$PROJ_VER" "$WEB_VER"; do
      [ "$v" = "$VER" ] || { echo "版が合いません: 指定 $VER・__init__.py $CODE_VER・pyproject.toml $PROJ_VER・package.json $WEB_VER（先に書き換えてコミットする）" >&2; exit 1; }
    done
    [ -z "$(git -C "$ROOT" status --porcelain)" ] || { echo "コミットされていない変更があります。コミットしてから Release を作ってください" >&2; exit 1; }
    # 2. 試験（不合格なら止まる）→ 3. build → qpkg → 4. 署名
    "$0" test
    "$0" build
    "$0" qpkg
    Q="$ROOT/dist/QnapNewsHub_${VER}_x86_64.qpkg"
    [ -f "$Q" ] || { echo ".qpkg ができていません: $Q" >&2; exit 1; }
    "$PY" "$ROOT/packaging/release/sign.py" "$Q"
    # 5. Release（ノートは日本語・更新の方法と戻し方・確かめていないこと）
    ARGS=(release create "v$VER" "$Q" "$Q.sha256" "$Q.manifest.json" "$Q.manifest.sig" -R souten-yd/QnapNewsHub --title "QNAP ニュースハブ $VER")
    [ -z "$PRE" ] || ARGS+=("$PRE")
    if [ -n "$NOTES" ]; then ARGS+=(--notes-file "$NOTES"); else ARGS+=(--notes "QNAP ニュースハブ $VER"); fi
    if [ "$DRY" = 1 ]; then echo "（dry-run）gh ${ARGS[*]}"; exit 0; fi
    command -v gh >/dev/null || { echo "gh が要ります" >&2; exit 1; }
    exec gh "${ARGS[@]}"
    ;;
  run)
    need_venv
    cd "$ROOT/backend"
    exec "$PY" -m qnapnewshub.cli run
    ;;
  print-service|install-service|uninstall-service)
    UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    UNIT="$UNIT_DIR/qnapnewshub.service"
    unit_text() {
      # 停止の合図（SIGTERM）でバッファを書き切る時間を TimeoutStopSec に取る。ログは journal へ（標準出力は捨てる）
      cat <<UNIT_EOF
[Unit]
Description=QNAP News Hub
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$ROOT/backend
ExecStart=$PY -m qnapnewshub.cli run
Environment=${NEWSHUB_DATA:+"NEWSHUB_DATA=$NEWSHUB_DATA"}
Restart=on-failure
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=120
StandardOutput=null
StandardError=journal
NoNewPrivileges=yes
PrivateTmp=yes

[Install]
WantedBy=default.target
UNIT_EOF
    }
    case "$cmd" in
      print-service) unit_text | sed '/^Environment=$/d' ;;
      install-service)
        need_venv
        command -v systemctl >/dev/null || { echo "systemd が見つかりません" >&2; exit 1; }
        [ "$(id -u)" -ne 0 ] || { echo "root では入れません（ふつうの利用者で実行してください）" >&2; exit 1; }
        mkdir -p "$UNIT_DIR"
        unit_text | sed '/^Environment=$/d' > "$UNIT"
        systemctl --user daemon-reload
        systemctl --user enable --now qnapnewshub.service
        echo "入れました: $UNIT"
        echo "ログイン前から動かすには: loginctl enable-linger \$USER（あとで利用者が実行）。状態: systemctl --user status qnapnewshub.service"
        ;;
      uninstall-service)
        systemctl --user disable --now qnapnewshub.service 2>/dev/null || true
        rm -f -- "$UNIT"
        systemctl --user daemon-reload 2>/dev/null || true
        echo "外しました（データの置き場は残しています）"
        ;;
    esac
    ;;
  help|-h|--help)
    sed -n '2,19p' "${BASH_SOURCE[0]}"
    ;;
  *)
    need_venv
    cd "$ROOT/backend"
    exec "$PY" -m qnapnewshub.cli "$cmd" "$@"
    ;;
esac
