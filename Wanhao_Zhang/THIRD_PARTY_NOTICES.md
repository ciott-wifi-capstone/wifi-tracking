# Third-party notices

## Wi-PRO

Upstream project: [ucsdwcsng/wi-pro-esp32-ftm](https://github.com/ucsdwcsng/wi-pro-esp32-ftm).

Source revision: `288311fd40cfdf629780ebc3604bc52671feec69`.

The upstream `firmware/` Rust source, transform-generation script and provided IDFT transform data are included for the ESP32-S3 native ranging reference. The firmware configuration's patch-directory path is adjusted for this package. The original ranging algorithm is retained. Upstream author and patch headers remain in their files.

Wi-PRO is distributed under the Apache License, Version 2.0. The license text is included in [LICENSE](LICENSE).

Reference:

W. Hunter, R. Ayyalasomayajula, and D. Bharadia, “Wi-PRO: Accurate indoor PNT using FTM with multipath suppression,” in *Proc. IEEE International Symposium on Spectrum Innovation (DySPAN)*, 2026, pp. 1–9, doi: [10.1109/DySPAN69846.2026.11571117](https://doi.org/10.1109/DySPAN69846.2026.11571117).

## ESP-IDF timestamp patch

`firmware/patch/0001-fix-wifi-Expose-Rx-pkt-timstamp-related-calculations.patch` is supplied through the upstream Wi-PRO project. Its header credits Nachiket Kukade of Espressif and retains the original description and authorship. ESP-IDF and its dependencies are obtained by the build toolchain and retain their respective licenses.

## Host implementation and frozen snapshot

The selective range correction, deployment recorder, guarded positioning and Kalman replay are the thesis host implementation. The native Wi-PRO reconstruction uses the upstream transform and detector definition, and is identified separately from the host correction.

`src/automatic_branch_runtime/` preserves the original frozen model, helper files and transform assets. Its manifest records relative historical source labels and the original SHA-256 values. Packaging changes affect manifest labels, not the model, helpers or transform bytes. The fixed-offset candidate records its original file hash, with personal path labels and a radio identity removed from its metadata. The offset and pair estimates are unchanged.

## Runtime dependencies

Python, NumPy, SciPy, pyserial, time-zone data, Matplotlib and pytest are installed separately. Their licenses and copyright notices remain with their distributions. Rust and ESP-IDF dependencies are declared in the firmware manifest and managed by their toolchains.
