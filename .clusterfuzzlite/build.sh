#!/bin/bash -eu
# Standard library only, so there is nothing to pip install first.
for fuzzer in fuzz/fuzz_*.py; do
  # The modules read these files at import time, relative to their own path.
  compile_python_fuzzer "$fuzzer" \
    --paths "$SRC/devstream" \
    --add-data "data:data" \
    --add-data "scripts/blocklist.txt:scripts"
done
