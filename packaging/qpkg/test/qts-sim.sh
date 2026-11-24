#!/bin/sh
# QTS を模したコンテナの中で QPKG を入れる・起こす・止める・入れ直す（docs/QPKG.md「確かめ方」）。
# 使い方（リポジトリの根から）: packaging/qpkg/test/run-sim.sh
set -e
# QTS の busybox sh は「. qpkg.cfg」を今の場所から読む。CentOS の bash（sh）は PATH からしか探さないので、模擬でだけ . を足す
export PATH=".:$PATH"
QPKG=/pkg/$(ls /pkg | grep '\.qpkg$' | head -n 1)
# QDK のインストーラは塊を 1KB 単位で切り出すので、data.tar.gz の末尾に余りが付く。QTS の busybox の gzip は無視するが、
# CentOS 7 の GNU gzip は警告（終了コード 2）にして tar が失敗する。模擬でだけ、gzip の警告（2）を成功として扱う
if [ ! -x /bin/gzip.real ]; then
  mv /bin/gzip /bin/gzip.real
  printf '#!/bin/bash\n/bin/gzip.real "$@"; r=$?; [ $r = 2 ] && exit 0; exit $r\n' > /bin/gzip; chmod +x /bin/gzip
fi
cp /sim/getcfg /sim/setcfg /sim/log_tool /sbin/
mkdir -p /etc/config /etc/default_config /share/CACHEDEV1_DATA/Public /etc/init.d /etc/rcS.d /etc/rcK.d /var/log /home/httpd/cgi-bin/apps
printf '[Public]\npath = /share/CACHEDEV1_DATA/Public\n' > /etc/config/smb.conf
printf '[System]\nVersion = 5.1.0\nBuild Number = 20240101\nModel = TS-253Be\n' > /etc/config/uLinux.conf
cp /etc/config/uLinux.conf /etc/default_config/uLinux.conf
: > /etc/config/qpkg.conf; : > /etc/platform.conf
printf '[SHARE_DEF]\ndefVolMP = /share/CACHEDEV1_DATA\n' > /etc/config/def_share.info
ln -sfn /share/CACHEDEV1_DATA /share/MD0_DATA 2>/dev/null || true

step() { echo; echo "=== $*"; }
health() { i=0; while [ $i -lt 60 ]; do curl -fsS http://127.0.0.1:8787/api/v1/health 2>/dev/null && return 0; sleep 1; i=$((i+1)); done; return 1; }

step "1. 入れる"
rc=0; sh "$QPKG" > /tmp/install1.log 2>&1 || rc=$?
[ $rc = 10 ] || { echo "終了コード $rc（QDK の成功は 10）"; tail -30 /tmp/install1.log; exit 1; }   # QDK の QPKG は成功で 10 を返す
tail -3 /tmp/install1.log
ROOT=$(/sbin/getcfg QnapNewsHub Install_Path -f /etc/config/qpkg.conf)
DATA=$(/sbin/getcfg QnapNewsHub Data_Dir -f /etc/config/qpkg.conf)
echo "Install_Path=$ROOT Data_Dir=$DATA Enable=$(/sbin/getcfg QnapNewsHub Enable -f /etc/config/qpkg.conf) Version=$(/sbin/getcfg QnapNewsHub Version -f /etc/config/qpkg.conf)"
"$ROOT/QnapNewsHub.sh" status >/dev/null 2>&1 || "$ROOT/QnapNewsHub.sh" start

step "2. 起きる（glibc $(ldd --version | head -n1 | awk '{print $NF}')）"
health; echo
curl -fsS -o /dev/null -w "画面 / → %{http_code}\n" http://127.0.0.1:8787/
curl -fsS -o /dev/null -w "manifest → %{http_code}\n" http://127.0.0.1:8787/manifest.webmanifest
curl -fsS http://127.0.0.1:8787/api/v1/auth/status; echo
sleep 3
echo "システムのログ:"; cat /var/log/qts-system.log 2>/dev/null | grep QnapNewsHub || echo "（なし）"
TOKEN=$(head -n1 "$DATA/setup-token.txt")
stat -c "setup-token.txt の権限 %a" "$DATA/setup-token.txt"
echo "番号なしの setup → $(curl -s -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -d '{"username":"admin","password":"correct-horse-battery"}' http://127.0.0.1:8787/api/v1/auth/setup)"
echo "番号ありの setup → $(curl -s -c /tmp/cj -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -d "{\"username\":\"admin\",\"password\":\"correct-horse-battery\",\"setup_token\":\"$TOKEN\"}" http://127.0.0.1:8787/api/v1/auth/setup)"
echo "番号のファイル: $([ -f "$DATA/setup-token.txt" ] && echo 残っている || echo 消えた)"
echo "同梱の Python で HTTPS（証明書・TLS）: $(env -i PYTHONPATH="$ROOT/app:$ROOT/site" SSL_CERT_FILE="$ROOT/site/certifi/cacert.pem" "$ROOT/python/bin/python3" -c 'import httpx; print(httpx.get("https://hnrss.org/newest?points=300", timeout=20).status_code)' 2>&1 | tail -n1)"
echo "QPKG の置き場に動いている間に書いた .pyc: $(find "$ROOT" -newer /tmp/install1.log -name '*.pyc' | wc -l) 件"

step "3. 止める（SIGTERM で書き切る）"
PID=$(cat /var/run/qnapnewshub.pid)
"$ROOT/QnapNewsHub.sh" stop
kill -0 "$PID" 2>/dev/null && echo "まだ動いている" || echo "止まった"
ls "$DATA" | tr '\n' ' '; echo

step "4. 入れ直す（データが残る）"
rc=0; sh "$QPKG" > /tmp/install2.log 2>&1 || rc=$?
[ $rc = 10 ] || { echo "終了コード $rc"; tail -30 /tmp/install2.log; exit 1; }
"$ROOT/QnapNewsHub.sh" status >/dev/null 2>&1 || "$ROOT/QnapNewsHub.sh" start
health; echo
echo "管理者でログイン → $(curl -s -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -d '{"username":"admin","password":"correct-horse-battery"}' http://127.0.0.1:8787/api/v1/auth/login)"
curl -fsS http://127.0.0.1:8787/api/v1/auth/status; echo
"$ROOT/QnapNewsHub.sh" stop
echo; echo "=== すべて済み"
