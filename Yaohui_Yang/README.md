# ESP32-S3 Wi-Fi indoor positioning

**ELEC5021 Capstone, University of Sydney** · **Contributor: Yaohui Yang**

This project uses Wi-Fi Fine Timing Measurement (FTM) to estimate distances between ESP32-S3 boards. The single-link pipeline converts channel state information (CSI) to a channel impulse response (CIR) and uses its features to correct the raw range. The six-anchor pipelines combine ranges with surveyed anchor coordinates to estimate a moving tag's 2D position.

| Directory | Purpose |
| --- | --- |
| [single_link/frame_match_firmware](single_link/frame_match_firmware/) | ESP-IDF anchor and tag, serial capture, CSI-to-CIR preprocessing, and range correction fitting. The numbered tag selects one link at a time. |
| [single_link/live_ranging](single_link/live_ranging/) | Live single-link display and an evaluator for completed captures. |
| [single_link/offline_analysis](single_link/offline_analysis/) | Scripts used for the eight-distance static ranging analysis. The original data and model files are local inputs. |
| [multi_anchor/six_anchor_2d](multi_anchor/six_anchor_2d/) | A1–A6 firmware, scan-based weighted least squares, asynchronous range tracking, calibration and ground-truth evaluation. |
| [multi_anchor/gru_temporal](multi_anchor/gru_temporal/) | Separate GRU V2 experiment: temporal training, live residual correction, robust weighted least squares and a constant-velocity Kalman filter. |
| [tools/floor_plan_click](tools/floor_plan_click/) | Image coordinate picker for use with a local floor plan. |
| [legacy](legacy/) | Earlier prototypes, diagnostics and runtime variants kept separate from the main entry points. |

Use each firmware with the matching host program. The single-link serial stream, the six-anchor scan protocol and the GRU experiment's MFUSED stream use different formats. The archived GRU experiment also uses different anchor IDs from the A1–A6 ground-truth project.

## Requirements

- ESP32-S3 boards (two for single-link tests; six fixed anchors and a tag for the A1–A6 project).
- ESP-IDF v6.0.2 for the numbered frame-match and A1–A6 firmware. Use the ESP-IDF terminal on Windows and select the board's actual COM port. For other snapshots, see their own notes.
- Python 3.10 or later; offline checks used Python 3.12. Install the requirements for the module you run. The GRU training and live GUI also need PyTorch and the GUI packages in the GRU requirements file.

## Quick check without hardware or measured data

From this directory, in PowerShell:

~~~powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\multi_anchor\six_anchor_2d\requirements.txt

Push-Location .\single_link\frame_match_firmware
..\..\.venv\Scripts\python.exe -m unittest discover -s tools -p "test_*.py" -v
Pop-Location

Push-Location .\multi_anchor\six_anchor_2d
..\..\.venv\Scripts\python.exe -m unittest discover -s tests -v
Pop-Location

Push-Location .\multi_anchor\gru_temporal
..\..\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_fused_3d_mapper.py" -v
..\..\.venv\Scripts\python.exe .\host_tools\multi_anchor_3d\fused_3d_mapper.py --simulate --rounds 40 --output .\results\simulation
Pop-Location
~~~

The simulation produces positions and link CSVs from synthetic observations. GRU inference is disabled in the example config, so the simulation checks the solver and tracker without needing model weights. The single-link replay tests skip if the original recordings and model files are absent.

## Single-link ranging

In [single_link/frame_match_firmware](single_link/frame_match_firmware/), choose a local test Wi-Fi passphrase and set it in both projects' main/lab_config.h files before flashing. From the ESP-IDF terminal, dry-run the numbered flash helper for the anchor, then the tag; replace COM11 with the port of the board being programmed. Remove the dry-run flag only when ready to flash.

~~~powershell
python .\tools\flash_numbered.py --role anchor --id A1 --port COM11 --dry-run
python .\tools\flash_numbered.py --role tag --id A1 --port COM11 --dry-run
~~~

Close any serial monitor before host capture. Use the following sequence with your own data. The collector asks for the distances and conditions. A is the fitting partition, B selects the regularization setting and reports validation, and C is an optional final test collected after freezing the fit.

~~~powershell
python -m pip install -r requirements.txt
python .\tools\cir_experiment.py init --root .\experiments\static --distances 1 2 3 4 6 9 12 15
python .\tools\cir_experiment.py collect --root .\experiments\static --split train --port COM11
python .\tools\cir_experiment.py collect --root .\experiments\static --split validation --port COM11
python .\tools\cir_experiment.py fit --root .\experiments\static
~~~

Then, from [single_link/live_ranging](single_link/live_ranging/), use the models from that fit. The port must belong to the tag and the baud rate must match its firmware.

~~~powershell
python -m pip install -r requirements.txt
python .\live_distance.py --port COM11 --baud 115200 --models ..\frame_match_firmware\experiments\static\fit\models --model cir_residual --seconds 120
~~~

Input is the tag's UART FTM/CSI stream. Live inference writes a new measurement folder with raw and corrected ranges in metres and quality flags. The evaluation script takes a completed capture and a reference distance separately. For the historical paper scripts, see [offline_analysis](single_link/offline_analysis/); they require the matching original feature table, selection metadata and model files.

## A1–A6 positioning

Open [multi_anchor/six_anchor_2d](multi_anchor/six_anchor_2d/) and follow [the original setup guide](multi_anchor/six_anchor_2d/README_中文.md). Replace the example anchor positions in anchors.json with surveyed coordinates in metres. Set the same local test passphrase in the anchor and tag header files. Use the correct serial ports and baud rates.

~~~powershell
python .\tools\flash.py --role anchor --id A1 --port COM7 --dry-run
python .\tools\flash.py --role tag --port COM11 --dry-run
python .\live_position.py --port COM11 --baud 921600 --models .\models --output .\captures\walk1
~~~

Repeat the anchor setup with each ID A1–A6. The live command needs the three compatible local JSON models (ftm_affine, cir_direct and cir_residual); provide them via the models argument. After collecting calibration sessions, refit_cir.py and calibrate.py can fit site-specific models and offsets. Sessions record per-link ranges, accepted scan positions and ground-truth markers where available.

## GRU V2 positioning experiment

Use the ESP-IDF projects in [multi_anchor/gru_temporal/firmware/multi_anchor_3d](multi_anchor/gru_temporal/firmware/multi_anchor_3d/) with the dashboard in [host_tools/multi_anchor_3d](multi_anchor/gru_temporal/host_tools/multi_anchor_3d/). Set a distinct ID per anchor in menuconfig and match the tag's Wi-Fi channel, serial settings and physical anchor IDs to the configuration. This project uses numeric IDs 1, 2, 3, 5, 6 and 7 for the archived GRU V2 model.

The training program reads link_measurements_labeled.csv files by session from results/multi_anchor_3d_live/. The CSVs include timestamps, anchor IDs, baseline distance, reference distance and the features listed in temporal_fusion_model.py. For a new collection, update the route labeler's geometry and session list, and the training script's disjoint train, validation and test session lists. The scripts preserve the original sequence length and experiment defaults.

~~~powershell
# Run from multi_anchor\gru_temporal\host_tools\multi_anchor_3d
python -m pip install -r requirements.txt
python .\label_route_sessions_v2.py
python .\train_temporal_fusion_v2.py
~~~

Training writes temporal_fusion_v2.pt beside the scripts. Keep a local copy of anchors_3d.json, replace its example coordinates, set temporal_fusion.enabled to true and point model_path to the compatible checkpoint. The dashboard accepts a config and port:

~~~powershell
python .\fast_dashboard.py --config .\anchors_3d.local.json --port COM11
~~~

The GRU predicts a range adjustment and uncertainty per anchor. The solver uses weighted ranges to estimate XY, then the Kalman filter smooths the estimated positions. The dashboard logs link measurements and positions for later inspection.

## Limits and provenance

- Performance depends on the board, RF environment and anchor geometry. Do not use the included synthetic layout for a hardware accuracy claim. Calibrate and validate with separate captures before comparing methods.
- These are source snapshots. No measurement recordings, lab floor plan, model checkpoints or build products are bundled. The example configurations are for smoke tests; archived scripts may still have fixed experimental constants or expect local inputs.
- No boards were flashed for this package. The offline checks above cover preprocessing, serial parsing, range correction flow and solver behaviour; they do not verify RF accuracy or reproduce the thesis metrics.
- See [SOURCE_MAP.md](SOURCE_MAP.md) for archive origins and [SUBMISSION.md](SUBMISSION.md) for the repository workflow.
