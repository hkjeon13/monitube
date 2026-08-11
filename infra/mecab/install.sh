#!/usr/bin/env bash
set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SOURCE_ARCHIVE="${SCRIPT_DIR}/vendor/mecab-0.996-ko-0.9.2.tar.gz"
readonly DICTIONARY_ARCHIVE="${SCRIPT_DIR}/vendor/mecab-ko-dic-2.1.1-20180720.tar.gz"
readonly SOURCE_SHA256="d0e0f696fc33c2183307d4eb87ec3b17845f90b81bf843bd0981e574ee3c38cb"
readonly DICTIONARY_SHA256="fd62d3d6d8fa85145528065fabad4d7cb20f6b2201e71be4081a4e9701a5b330"

verify_archive() {
  local expected="$1"
  local archive="$2"
  local actual
  actual="$(sha256sum "$archive" | awk '{print $1}')"
  [[ "$actual" == "$expected" ]]
}

verify_archive "$SOURCE_SHA256" "$SOURCE_ARCHIVE"
verify_archive "$DICTIONARY_SHA256" "$DICTIONARY_ARCHIVE"

build_dir="$(mktemp -d)"
trap 'rm -rf "$build_dir"' EXIT

tar -xzf "$SOURCE_ARCHIVE" -C "$build_dir"
pushd "$build_dir/mecab-0.996-ko-0.9.2" >/dev/null
./configure --enable-utf8-only
make -j"$(nproc)"
make check
make install
popd >/dev/null
ldconfig

tar -xzf "$DICTIONARY_ARCHIVE" -C "$build_dir"
pushd "$build_dir/mecab-ko-dic-2.1.1-20180720" >/dev/null
./configure \
  --with-charset=utf8 \
  --with-mecab-config=/usr/local/bin/mecab-config
make -j"$(nproc)"
make install
popd >/dev/null
ldconfig

test -x /usr/local/bin/mecab
test -f /usr/local/lib/mecab/dic/mecab-ko-dic/sys.dic
printf '영상 분석\n' | /usr/local/bin/mecab | grep -q $'영상\tNNG'
