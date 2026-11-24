#!/bin/sh
# QPKG を、glibc 2.17 の CentOS 7 のコンテナで QTS を模して試す（ネットワークはコンテナの中だけ）。
set -e
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
exec podman run --rm --tmpfs /mnt/HDA_ROOT -v "$ROOT/dist:/pkg:ro,Z" -v "$ROOT/packaging/qpkg/test:/sim:ro,Z" docker.io/library/centos:7 sh /sim/qts-sim.sh
