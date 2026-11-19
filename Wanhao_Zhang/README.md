# Selective correction and positioning with Wi-PRO

Code accompanying Wanhao Zhang's ELEC4713 Thesis B, University of Sydney.

This package combines the upstream ESP32-S3 Wi-PRO firmware with host-side range correction, guarded positioning and causal Kalman replay. The host method selects a LOS-offset branch or a feature-based Ridge branch using a frozen logistic gate. Positioning checks range validity, retains original measurement age and uses geometry weights with a Huber loss.

The Wi-PRO ranging algorithm is credited to Hunter, Ayyalasomayajula and Bharadia. The included Rust source is based on upstream revision `288311fd40cfdf629780ebc3604bc52671feec69`. See [third-party notices](THIRD_PARTY_NOTICES.md) and [firmware instructions](docs/FIRMWARE.md).

## Contents

| Path | Contents |
| --- | --- |
| `src/live_route.py` | Browser dashboard and serial recording |
| `src/live_automatic.py` | Exact-event joining and frozen range correction |
| `src/guarded_localization.py` | Age, validity and geometry checks, weighted robust solve |
| `src/cv_kalman.py` | Fixed-parameter, causal constant-velocity Kalman filter |
| `src/offline_replay.py` | Native reconstruction and position replay examples |
| `src/automatic_branch_runtime/` | Frozen model, helpers and transform assets |
| `firmware/` | Upstream Wi-PRO Rust source and provided IDFT matrices |
| `examples/` | Anonymous reconstruction fixture and deployment examples |
| `tests/` | Software checks |

## Install

Use Python 3.11. Run commands from this directory.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

On Linux, activate with `source .venv/bin/activate`. The runtime uses NumPy, SciPy and pyserial. The requirements include time-zone data for Windows. Matplotlib is optional for additional plotting.

## Run the offline examples

```powershell
python src/offline_replay.py --input examples/native_reconstruction.json --out output/example
python src/offline_replay.py --positions examples/positions.csv --out output/kalman
```

The first command reconstructs 12 recorded CSI events and compares them with the supplied native Wi-PRO outputs. It writes `event_predictions.csv` and `summary.json`. The fixture has no board identities or ground-truth distances.

The second command replays timestamped positions through the fixed Kalman filter. Its CSV input has `t`, `x` and `y` columns, with time in seconds and coordinates in metres. An optional `new_information` column accepts `true`, `false`, `1` or `0`. When it is false, blank coordinates represent a prediction-only event. Outputs are `kalman_replay.csv`, `kalman_replay.json` and `summary.json`. Use a new output directory for each run.

## Record a deployment

1. Build and flash the firmware using [FIRMWARE.md](docs/FIRMWARE.md).
2. Copy `examples/nodes.example.json` to `wipro_nodes.json`.
3. Replace the example MACs, anchor coordinates and antenna heights with independently measured values. The example MACs and geometry are placeholders. Use full MAC addresses.
4. Register the application image used for the deployment.

```powershell
python scripts/register_firmware.py --registry wipro_nodes.json --image firmware/bin/firmware.bin
python src/live_route.py --registry wipro_nodes.json --port COMx
```

Replace `COMx` with the A0 native USB serial port, or use a device such as `/dev/ttyACM0` on Linux. Keep A1–A6 powered as responders. Open `http://127.0.0.1:8765/`. Starting a recording opens the serial port, stopping it mutes the initiator. MARK records a reference observation and never supplies a position to the solver. Only one program should hold the serial port.

Each recording creates a separate directory under `captures/live_route/`. It contains raw records, `track.csv`, `marks.csv`, `automatic_ranges.csv`, `guarded_diagnostics.jsonl`, `session.json`, configuration copies and a `provenance/` source snapshot. Original and corrected ranges remain available for comparison. The historical global-offset control and the automatic model's LOS offset are separate candidates.

## Checks and reproduction scope

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

The frozen runtime verifies model, helper and transform hashes. Keep `src/automatic_branch_runtime/` unchanged. The manifest records relative labels for the original research files. A historical benchmark-only path remains in a frozen helper, it is not used by the inference entry point. Candidate metadata records its original archive hash and the removal of personal path labels and a radio identity.

This distribution contains a small reconstruction fixture and example positions. It does not contain the full thesis capture archive or reproduce every reported result. The supplied model was fitted in one deployment, transfer to new boards or environments requires independent evaluation. Kalman step statistics describe continuity, a reference trajectory is needed to measure positioning accuracy. The offline commands use fixed parameters and do not refit the correction model.
