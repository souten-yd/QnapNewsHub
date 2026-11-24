#!/bin/sh
# QnapNewsHub の起動・停止（QTS の App Center と /etc/init.d から呼ばれる）。
# データの置き場は qpkg.conf の Data_Dir（既定 .qpkg/QnapNewsHub-data。QPKG を更新・削除しても残る）。
CONF=${QNH_QPKG_CONF:-/etc/config/qpkg.conf}
GETCFG=${QNH_GETCFG:-/sbin/getcfg}
LOG_TOOL=${QNH_LOG_TOOL:-/sbin/log_tool}
RUN_DIR=${QNH_RUN_DIR:-/var/run}
QPKG_NAME=QnapNewsHub
QPKG_ROOT=$($GETCFG $QPKG_NAME Install_Path -f "$CONF")
DATA=$($GETCFG $QPKG_NAME Data_Dir -d "" -f "$CONF")
[ -n "$DATA" ] || DATA="${QPKG_ROOT%/*}/QnapNewsHub-data"
PORT=$($GETCFG $QPKG_NAME Web_Port -d 8787 -f "$CONF")
PIDFILE="$RUN_DIR/qnapnewshub.pid"
TOKEN_NOTED="$RUN_DIR/qnapnewshub.token-noted"
PY="$QPKG_ROOT/python/bin/python3"
export QNAP_QPKG=$QPKG_NAME

running() {
    [ -f "$PIDFILE" ] || return 1
    pid=$(cat "$PIDFILE" 2>/dev/null)
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

note_setup_token() {
    # 管理者がまだいなければ、一回限りの番号を QTS のシステムのログ（管理者だけが見られる）に出す。1 つの番号につき 1 回だけ
    i=0
    while [ $i -lt 30 ]; do
        [ -f "$DATA/setup-token.txt" ] && break
        running || return 0
        sleep 1; i=$((i + 1))
    done
    [ -f "$DATA/setup-token.txt" ] || return 0
    token=$(head -n 1 "$DATA/setup-token.txt")
    [ -f "$TOKEN_NOTED" ] && [ "$(cat "$TOKEN_NOTED")" = "$token" ] && return 0
    if [ -x "$LOG_TOOL" ]; then
        "$LOG_TOOL" -t 0 -a "[QnapNewsHub] 最初の管理者を作る一回限りの番号: $token （http://<NAS の IP>:$PORT/ を開いて入力）"
    fi
    echo "$token" > "$TOKEN_NOTED"
}

case "$1" in
  start)
    ENABLED=$($GETCFG $QPKG_NAME Enable -u -d FALSE -f "$CONF")
    if [ "$ENABLED" != "TRUE" ]; then
        echo "$QPKG_NAME is disabled."
        exit 1
    fi
    if running; then
        echo "$QPKG_NAME is already running."
        exit 0
    fi
    mkdir -p "$DATA" && chmod 700 "$DATA"
    # 待ち受けは LAN（0.0.0.0）。最初の管理者は一回限りの番号を知っている人だけが作れる（NEWSHUB_SETUP_TOKEN=1）
    # .pyc は build のときに作ってある。動いている間に QPKG の置き場へ書かない（PYTHONDONTWRITEBYTECODE）
    env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin HOME="$DATA" LANG=C.UTF-8 TZ="${TZ:-}" \
        NEWSHUB_DATA="$DATA" NEWSHUB_HOST="${QNH_HOST:-0.0.0.0}" NEWSHUB_PORT="$PORT" NEWSHUB_WEB="$QPKG_ROOT/web" \
        NEWSHUB_SETUP_TOKEN=1 PYTHONPATH="$QPKG_ROOT/app:$QPKG_ROOT/site" PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 \
        SSL_CERT_FILE="$QPKG_ROOT/site/certifi/cacert.pem" \
        "$PY" -m qnapnewshub.cli run >/dev/null 2>&1 &
    echo $! > "$PIDFILE"
    note_setup_token &
    ;;

  stop)
    if running; then
        pid=$(cat "$PIDFILE")
        kill -TERM "$pid" 2>/dev/null
        # メモリに溜めた記事を書き切るのを待つ（最大 100 秒。超えたら強制終了）
        i=0
        while kill -0 "$pid" 2>/dev/null && [ $i -lt 100 ]; do sleep 1; i=$((i + 1)); done
        kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null
    fi
    rm -f "$PIDFILE"
    ;;

  restart)
    $0 stop
    $0 start
    ;;

  status)
    if running; then echo "running"; exit 0; else echo "stopped"; exit 1; fi
    ;;

  remove)
    ;;

  *)
    echo "Usage: $0 {start|stop|restart|status|remove}"
    exit 1
esac

exit 0
