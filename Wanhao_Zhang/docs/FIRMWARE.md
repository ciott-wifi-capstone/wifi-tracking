# Wi-PRO firmware

## Source and target

The `firmware/` Rust source comes from [ucsdwcsng/wi-pro-esp32-ftm](https://github.com/ucsdwcsng/wi-pro-esp32-ftm), revision `288311fd40cfdf629780ebc3604bc52671feec69`. It targets ESP32-S3 with ESP-IDF v5.5. The local configuration points to the supplied `patch/` directory. The native Wi-PRO algorithm is unchanged.

The project additions run on the host. The logistic gate, Ridge correction, range fallback, guarded positioning and Kalman filter are not changes to `firmware/src/wipro.rs`.

## Build environment

Build under Linux or WSL with the ESP Rust toolchain installed by `espup`. Required tools include `ldproxy`, `espflash`, Python 3 and the usual ESP-IDF build dependencies, including CMake, Ninja and a C compiler. The firmware's `rust-toolchain.toml` selects the `esp` toolchain.

Typical tool installation commands are:

```bash
cargo install espup ldproxy espflash
espup install
source "$HOME/export-esp.sh"
```

Install the system dependencies listed in the upstream Wi-PRO build instructions for your Linux distribution. The first build downloads and compiles ESP-IDF and its dependencies.

From the package root:

```bash
bash build_firmware.sh
```

The wrapper builds the application and uses the provided matrices in `firmware/data/`. Keep those matrices for native-reference reconstruction. Regenerating an SVD can change its numerical basis and the resulting file hashes. `scripts/create_idft_mat.py` is retained as the upstream generator for deliberate algorithm or transform changes.

The application ELF is written to `firmware/target/xtensa-esp32s3-espidf/release/firmware`. Generate an application binary for firmware registration using esptool:

```bash
python -m pip install esptool
mkdir -p firmware/bin
python -m esptool --chip esp32s3 elf2image firmware/target/xtensa-esp32s3-espidf/release/firmware --output firmware/bin/firmware.bin
```

## Flash and register

Connect the ESP32-S3 through its native USB port. Flash the generated application ELF using `espflash`, substituting the actual serial device:

```bash
espflash flash --port /dev/ttyACM0 firmware/target/xtensa-esp32s3-espidf/release/firmware
```

Use the same application build across the deployment. Board names A0–A6 are assigned by full MAC address in the host registry. The firmware does not encode those labels.

After creating the deployment registry, register the generated application binary from the package root:

```bash
python scripts/register_firmware.py --registry wipro_nodes.json --image firmware/bin/firmware.bin
```

Registration records the generated application image's SHA-256 in the registry for the host's firmware check. Use the binary generated from the same ELF that was flashed. Registration does not flash the board or verify its physical identity. Re-register after rebuilding and flashing a different image.

## Operation

The upstream firmware exposes commands such as `id`, `help`, `mute`, `interval` and `burst`. Responders should remain powered and discoverable. The dashboard manages the A0 initiator during acquisition. Close other serial monitors before starting it.

No board programming or hardware measurement is performed by the offline example or software tests. A successful software test does not verify a newly built firmware image on hardware.
