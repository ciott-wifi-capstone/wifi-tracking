#!/usr/bin/env bash
# Build the pinned ESP32-S3 reference with its supplied IDFT tables.
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
firmware_dir="$project_dir/firmware"
for asset in idft_L.bin idft_R.bin idft_meta.bin idft_drange.bin idft_sinc_rise_comp.bin; do
    test -f "$firmware_dir/data/$asset" || { echo "Missing IDFT table: $asset" >&2; exit 1; }
done
cd "$firmware_dir"
cargo build --release "$@"
echo "ELF: $firmware_dir/target/xtensa-esp32s3-espidf/release/firmware"
