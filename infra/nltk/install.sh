#!/usr/bin/env bash
set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly DATA_DIR="${NLTK_DATA:-/usr/local/share/nltk_data}"
readonly PUNKT_ARCHIVE="${SCRIPT_DIR}/vendor/punkt_tab.zip"
readonly TAGGER_ARCHIVE="${SCRIPT_DIR}/vendor/averaged_perceptron_tagger_eng.zip"
readonly PUNKT_SHA256="e57f64187974277726a3417ca6f181ec5403676c717672eef6a748a7b20e0106"
readonly TAGGER_SHA256="6025f530624335c67d6547d44757b357b4e79bae030a0383e9887a92c1718f0b"

python_bin="$(command -v python || command -v python3)"

[[ "$(sha256sum "$PUNKT_ARCHIVE" | awk '{print $1}')" == "$PUNKT_SHA256" ]]
[[ "$(sha256sum "$TAGGER_ARCHIVE" | awk '{print $1}')" == "$TAGGER_SHA256" ]]

mkdir -p "$DATA_DIR/tokenizers" "$DATA_DIR/taggers"
"$python_bin" -m zipfile -e "$PUNKT_ARCHIVE" "$DATA_DIR/tokenizers"
"$python_bin" -m zipfile -e "$TAGGER_ARCHIVE" "$DATA_DIR/taggers"

test -f "$DATA_DIR/tokenizers/punkt_tab/english/abbrev_types.txt"
test -f "$DATA_DIR/taggers/averaged_perceptron_tagger_eng/averaged_perceptron_tagger_eng.weights.json"
