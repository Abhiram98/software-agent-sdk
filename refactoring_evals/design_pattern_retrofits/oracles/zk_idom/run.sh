#!/usr/bin/env bash
# Differential oracle for the zk IDOMFactory reversal.
# Usage: run.sh <repo> <out_dir>
# Writes <out_dir>/dump.tsv (one hash per file x parser config) and
# <out_dir>/histogram.tsv (item classes built across the corpus).
set -euo pipefail

repo=$(cd "$1" && pwd)
out=$(mkdir -p "$2" && cd "$2" && pwd)
here=$(cd "$(dirname "$0")" && pwd)

cd "$repo"
git ls-files '*.zul' '*.xml' > "$out/corpus.txt"
cp=$(./gradlew -q -I "$here/classpath.gradle" :zcommon:printRuntimeClasspath 2>"$out/gradle.log" | tail -1)
javac -d "$out/classes" -cp "$cp" "$here/IdomDump.java"
java -cp "$out/classes:$cp" IdomDump "$out/corpus.txt" "$repo" "$out/histogram.tsv" > "$out/dump.tsv"
echo "files=$(wc -l < "$out/corpus.txt") results=$(wc -l < "$out/dump.tsv") errors=$(grep -c $'\terror ' "$out/dump.tsv" || true)"
