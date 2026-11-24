#!/usr/bin/env bash
# QnapNewsHub の QPKG（x86_64）を作る。./hub.sh qpkg から呼ぶ。
#   - Python は python-build-standalone（版と sha256 を固定）。glibc 2.17 以上で動く。
#   - 依存は requirements.lock の版を、manylinux2014（glibc 2.17）の wheel だけで入れる（QTS の古い glibc でも動くように）。
#   - .pyc は build のときに作る（QNAP では QPKG の置き場へ書かない）。
#   - QPKG にまとめるのは公式の QDK 2.5.3（qbuild）。手元の機械を汚さないよう podman のコンテナの中で使う。
# 取得物は packaging/.cache/（.gitignore）。できたものは dist/QnapNewsHub_<版>_x86_64.qpkg。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HERE="$ROOT/packaging/qpkg"
CACHE="$ROOT/packaging/.cache"
STAGE="$CACHE/stage"
OUT="$ROOT/dist"

PY_TAG="20261110"
PY_NAME="cpython-3.12.16+${PY_TAG}-x86_64-unknown-linux-gnu-install_only.tar.gz"
PY_SHA="c2f87d4b4e8db3abddb0f116c6ccfc503d0cbdbb9ee8faacddad1b679ec12e99"
PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_TAG}/${PY_NAME//+/%2B}"
QDK_VER="2.5.3"
QDK_DEB="qdk_${QDK_VER}_amd64.deb"
QDK_SHA="17b3841b7d4590a4ee025844ba583304b5e3c497d9fa8934d5175131d3908022"
QDK_URL="https://github.com/qnap-dev/QDK/releases/download/v${QDK_VER}/${QDK_DEB}"
BASE_IMAGE="docker.io/library/debian:bookworm-slim"
IMAGE="localhost/qnapnewshub-qdk:${QDK_VER}"

fetch() {   # fetch <url> <file> <sha256>
  local url="$1" file="$CACHE/$2" sha="$3"
  if [ ! -f "$file" ] || ! echo "$sha  $file" | sha256sum -c --status; then
    echo "取得: $2"
    curl -fsSL --retry 3 -o "$file.part" "$url"
    mv "$file.part" "$file"
  fi
  echo "$sha  $file" | sha256sum -c --status || { echo "sha256 が合いません: $2" >&2; exit 1; }
}

VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/backend/qnapnewshub/__init__.py")"
[ -n "$VERSION" ] || { echo "版が読めません" >&2; exit 1; }
[ -f "$ROOT/frontend/dist/index.html" ] || { echo "先に ./hub.sh build で画面を build してください" >&2; exit 1; }
[ -x "$ROOT/.venv/bin/python" ] || { echo "先に ./hub.sh setup を実行してください" >&2; exit 1; }
command -v podman >/dev/null || { echo "podman が要ります（QDK をコンテナの中で使うため）" >&2; exit 1; }

mkdir -p "$CACHE" "$OUT"
fetch "$PY_URL" "$PY_NAME" "$PY_SHA"
fetch "$QDK_URL" "$QDK_DEB" "$QDK_SHA"

echo "組み立て: $STAGE"
rm -rf "$STAGE"
mkdir -p "$STAGE/QnapNewsHub/x86_64" "$STAGE/QnapNewsHub/shared" "$STAGE/QnapNewsHub/icons"
P="$STAGE/QnapNewsHub"
sed "s/^QPKG_VER=.*/QPKG_VER=\"$VERSION\"/" "$HERE/qpkg.cfg" > "$P/qpkg.cfg"
cp "$HERE/package_routines" "$P/package_routines"
cp "$HERE/shared/QnapNewsHub.sh" "$P/shared/QnapNewsHub.sh"
chmod 755 "$P/shared/QnapNewsHub.sh"

X="$P/x86_64"
tar -xzf "$CACHE/$PY_NAME" -C "$X"                 # → x86_64/python
rm -rf "$X/python/lib/python3.12/test" "$X/python/lib/python3.12/idlelib" "$X/python/lib/python3.12/tkinter" "$X/python/lib/python3.12/turtledemo" \
       "$X/python/lib/python3.12/ensurepip" "$X/python/lib/python3.12/lib2to3" "$X/python/include"
"$ROOT/.venv/bin/python" -m pip install --quiet --no-deps --target "$X/site" \
  --platform manylinux2014_x86_64 --platform manylinux_2_17_x86_64 --python-version 3.12 --implementation cp --only-binary=:all: \
  -r "$HERE/requirements.lock"
rm -rf "$X/site/bin" "$X/site/greenlet/tests"
mkdir -p "$X/app"
cp -r "$ROOT/backend/qnapnewshub" "$X/app/qnapnewshub"
find "$X/app" -name "__pycache__" -type d -prune -exec rm -rf {} +
cp -r "$ROOT/frontend/dist" "$X/web"
# .pyc は同梱の Python で作る（版が合う）。中身の hash で照合する形にして、時刻のずれで無効にならないように
"$X/python/bin/python3" -m compileall -q -j 0 --invalidation-mode unchecked-hash "$X/app" "$X/site" "$X/python/lib/python3.12" >/dev/null || true

echo "QPKG にまとめる（podman・QDK $QDK_VER）"
if ! podman image exists "$IMAGE"; then
  podman build -q -t "$IMAGE" -f - "$CACHE" <<EOF
FROM $BASE_IMAGE
COPY $QDK_DEB /tmp/$QDK_DEB
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends /tmp/$QDK_DEB imagemagick >/dev/null && rm -rf /var/lib/apt/lists/* /tmp/$QDK_DEB
EOF
fi
# アイコン（QDK は GIF: <名前>.gif 64・<名前>_80.gif・<名前>_gray.gif）
cp "$HERE/icons/icon-64.png" "$HERE/icons/icon-80.png" "$HERE/icons/icon-64-gray.png" "$STAGE/"
podman run --rm -v "$STAGE:/work:Z" -w /work "$IMAGE" sh -euc '
  convert icon-64.png QnapNewsHub/icons/QnapNewsHub.gif
  convert icon-80.png QnapNewsHub/icons/QnapNewsHub_80.gif
  convert icon-64-gray.png QnapNewsHub/icons/QnapNewsHub_gray.gif
  rm -f icon-*.png
  cd QnapNewsHub && qbuild --build-arch x86_64 >/dev/null
  chown -R '"$(id -u):$(id -g)"' /work 2>/dev/null || true
'
built="$(ls "$P"/build/*.qpkg | head -n 1)"
[ -f "$built" ] || { echo "qbuild が QPKG を作れませんでした" >&2; exit 1; }
cp "$built" "$OUT/QnapNewsHub_${VERSION}_x86_64.qpkg"
( cd "$OUT" && sha256sum "QnapNewsHub_${VERSION}_x86_64.qpkg" > "QnapNewsHub_${VERSION}_x86_64.qpkg.sha256" )
ls -la "$OUT/QnapNewsHub_${VERSION}_x86_64.qpkg"
echo "できました: dist/QnapNewsHub_${VERSION}_x86_64.qpkg"
