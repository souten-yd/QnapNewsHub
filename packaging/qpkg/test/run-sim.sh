#!/bin/sh
# QPKG を、glibc 2.17 の CentOS 7 のコンテナで QTS を模して試す（ネットワークはコンテナの中だけ）。
#   run-sim.sh          入れる・起こす・止める・入れ直す（docs/QPKG.md「確かめ方」）
#   run-sim.sh update   0.1.0 相当の古い QPKG を入れ、模擬の Release（試験用の一時の鍵で署名）から API 経由で 0.1.1 へ更新する（docs/UPDATE.md）
set -e
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
SIM_MODE="${1:-}"
if [ "$SIM_MODE" != update ]; then
  exec podman run --rm --tmpfs /mnt/HDA_ROOT -v "$ROOT/dist:/pkg:ro,Z" -v "$ROOT/packaging/qpkg/test:/sim:ro,Z" docker.io/library/centos:7 sh /sim/qts-sim.sh
fi

# ---- 更新の試験: 準備（手元の側） ----
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
NEW="$(ls "$ROOT"/dist/QnapNewsHub_*_x86_64.qpkg | head -n 1)"
[ -f "$NEW" ] || { echo "先に ./hub.sh qpkg で新しい版の QPKG を作ってください" >&2; exit 1; }
NEW_VER="$(basename "$NEW" | sed 's/^QnapNewsHub_\(.*\)_x86_64\.qpkg$/\1/')"
OLD_VER="0.1.0"
OLD_DIR="$ROOT/dist/sim-old"
if [ ! -f "$OLD_DIR/QnapNewsHub_${OLD_VER}_x86_64.qpkg" ]; then
  echo "古い版（$OLD_VER 相当）の QPKG を作ります"
  NEWSHUB_QPKG_VERSION="$OLD_VER" NEWSHUB_QPKG_OUT="$OLD_DIR" "$ROOT/packaging/qpkg/build.sh" >/dev/null
fi
FEED="$ROOT/packaging/.cache/sim-feed"
rm -rf "$FEED"; mkdir -p "$FEED/good" "$FEED/evil"
trap 'rm -rf "$FEED"' EXIT
# 試験用の一時の鍵（本物の秘密鍵は使わない）。good は正しい鍵で署名、evil は別の鍵で署名（公開鍵は good のものを渡す）
SIM_PUBKEY="$("$PY" - "$NEW" "$FEED" <<'PYEOF'
import base64, importlib.util, json, shutil, sys
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
spec = importlib.util.spec_from_file_location("sign", Path("packaging/release/sign.py"))
sign = importlib.util.module_from_spec(spec); spec.loader.exec_module(sign)
new, feed = Path(sys.argv[1]), Path(sys.argv[2])
ver = new.name.split("_")[1]
for which, key in (("good", Ed25519PrivateKey.generate()), ("evil", Ed25519PrivateKey.generate())):
    d = feed / which
    shutil.copy(new, d / new.name)
    sign.sign_qpkg(d / new.name, key)
    assets = [{"name": n, "size": (d / n).stat().st_size, "browser_download_url": n} for n in (new.name, new.name + ".manifest.json", new.name + ".manifest.sig")]
    (d / "releases.json").write_text(json.dumps([{"tag_name": "v" + ver, "prerelease": False, "draft": False, "body": "## 試験の Release\n- 模擬", "html_url": "https://github.com/souten-yd/QnapNewsHub/releases/tag/v" + ver, "published_at": "2026-11-24T00:00:00Z", "assets": assets}]))
    if which == "good":
        good_pub = base64.b64encode(key.public_key().public_bytes_raw()).decode()
print(good_pub)
PYEOF
)"
exec podman run --rm --tmpfs /mnt/HDA_ROOT -e SIM_PUBKEY="$SIM_PUBKEY" -e OLD_VER="$OLD_VER" -e NEW_VER="$NEW_VER" \
  -v "$ROOT/dist:/pkg:ro,Z" -v "$FEED/good:/feed-good:ro,Z" -v "$FEED/evil:/feed-evil:ro,Z" -v "$ROOT/packaging/qpkg/test:/sim:ro,Z" \
  docker.io/library/centos:7 sh /sim/qts-sim.sh update
