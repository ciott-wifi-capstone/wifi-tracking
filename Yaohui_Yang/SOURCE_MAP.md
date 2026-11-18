# Source map

The table maps each directory to the earlier archive used for that snapshot. A folder can combine the matching firmware, host utilities and a documented overlay from the same project family.

For this contribution, the source and its project configuration are included; generated build products, original experimental captures, trained model artifacts, lab floor-plan images and the separate partner firmware are left out. Lab passphrases and local paths are replaced by placeholders. The active anchor JSON files use example coordinates. Original backup scripts may still require the experimental constants noted in their own source.

| Folder | Input archive(s) |
| --- | --- |
| [multi_anchor/gru_temporal](multi_anchor/gru_temporal/) | `代码.zip` |
| [multi_anchor/gru_temporal/host_tools/multi_anchor_3d](multi_anchor/gru_temporal/host_tools/multi_anchor_3d/) | `代码.zip / gru_v2_first_stable_restore.zip` |
| [multi_anchor/six_anchor_2d](multi_anchor/six_anchor_2d/) | `Multi_Anchor_2D_GT(1).zip` |
| [single_link/frame_match_firmware](single_link/frame_match_firmware/) | `QAR_FTM_S3_v5_FRAME_MATCH.zip` |
| [single_link/live_ranging](single_link/live_ranging/) | `Single_Link_Live_Ranging(1).zip` |
| [single_link/offline_analysis](single_link/offline_analysis/) | `Single_Link_Paper_20261021.zip` |
| [tools/floor_plan_click](tools/floor_plan_click/) | `click_to_pos(2).zip` |
| [legacy/floor_plan_click](legacy/floor_plan_click/) | `click_to_pos(2).zip / click_to_pos.zip` |
| [legacy/multi_anchor/adaptive_kalman](legacy/multi_anchor/adaptive_kalman/) | `ftm_csi_adaptive_kf_modified.zip` |
| [legacy/multi_anchor/eight_anchor_firmware](legacy/multi_anchor/eight_anchor_firmware/) | `all_8_anchors_and_tag_ESP32S3.zip` |
| [legacy/multi_anchor/epoch_dashboard](legacy/multi_anchor/epoch_dashboard/) | `esp32s3_7anchor_epoch_dashboard.zip` |
| [legacy/multi_anchor/epoch_firmware](legacy/multi_anchor/epoch_firmware/) | `esp32s3_multi_anchor_epoch_firmware.zip` |
| [legacy/multi_anchor/expandable_3d_archive](legacy/multi_anchor/expandable_3d_archive/) | `04_multi_anchor_3d(1).zip` |
| [legacy/multi_anchor/expandable_3d_development](legacy/multi_anchor/expandable_3d_development/) | `代码.zip` |
| [legacy/multi_anchor/expandable_3d_early](legacy/multi_anchor/expandable_3d_early/) | `04_multi_anchor_3d.zip` |
| [legacy/multi_anchor/five_anchor_3d](legacy/multi_anchor/five_anchor_3d/) | `多AP室内定位.zip` |
| [legacy/multi_anchor/mobile_runtime](legacy/multi_anchor/mobile_runtime/) | `多AP室内定位.zip` |
| [legacy/multi_anchor/seven_anchor_csi_weight](legacy/multi_anchor/seven_anchor_csi_weight/) | `esp32s3_7anchor_csi_weight_v1(1).zip` |
| [legacy/multi_anchor/seven_anchor_rtt_cir](legacy/multi_anchor/seven_anchor_rtt_cir/) | `ESP32S3_七Anchor_RTT_CIR_实验室改造版.zip` |
| [legacy/multi_anchor/seven_anchor_tag_without_a7](legacy/multi_anchor/seven_anchor_tag_without_a7/) | `tag_7anchors_no_A7_ESP32S3.zip` |
| [legacy/multi_anchor/seven_anchor_weight_v2](legacy/multi_anchor/seven_anchor_weight_v2/) | `esp32s3_7anchor_weight_v2.zip` |
| [legacy/multi_anchor/six_anchor_firmware_early](legacy/multi_anchor/six_anchor_firmware_early/) | `all_6_anchors_ESP32S3.zip` |
| [legacy/report_reproduction](legacy/report_reproduction/) | `Thesis_Figures_and_Data.zip` |
| [legacy/runtime_variants/fast_csi_ftm_realtime_patch](legacy/runtime_variants/fast_csi_ftm_realtime_patch/) | `代码.zip / fast_csi_ftm_realtime_patch.zip` |
| [legacy/runtime_variants/fast_csi_ftm_realtime_patch_fixed](legacy/runtime_variants/fast_csi_ftm_realtime_patch_fixed/) | `代码.zip / fast_csi_ftm_realtime_patch_fixed.zip` |
| [legacy/runtime_variants/gru_v2_balanced_low_latency_patch](legacy/runtime_variants/gru_v2_balanced_low_latency_patch/) | `代码.zip / gru_v2_balanced_low_latency_patch.zip` |
| [legacy/runtime_variants/gru_v2_fast_response_patch](legacy/runtime_variants/gru_v2_fast_response_patch/) | `代码.zip / gru_v2_fast_response_patch.zip` |
| [legacy/runtime_variants/gru_v2_live_patch](legacy/runtime_variants/gru_v2_live_patch/) | `代码.zip / gru_v2_live_patch.zip` |
| [legacy/runtime_variants/gru_v2_ultra_low_latency_patch](legacy/runtime_variants/gru_v2_ultra_low_latency_patch/) | `代码.zip / gru_v2_ultra_low_latency_patch.zip` |
| [legacy/single_link/anchor_snapshot](legacy/single_link/anchor_snapshot/) | `anchor.zip` |
| [legacy/single_link/cir_diagnostics](legacy/single_link/cir_diagnostics/) | `QAR_FTM_CIR_Analysis(1).zip` |
| [legacy/single_link/cir_fit](legacy/single_link/cir_fit/) | `QAR_FTM_S3_CIR_FIT.zip` |
| [legacy/single_link/clock_diagnostics](legacy/single_link/clock_diagnostics/) | `diagnose_ftm_csi_clocks.py` |
| [legacy/single_link/early_build](legacy/single_link/early_build/) | `QAR_FTM_S3_ready_to_build.zip` |
| [legacy/single_link/first_two_board_experiment](legacy/single_link/first_two_board_experiment/) | `ESP32S3初次实验代码及数据.zip` |
| [legacy/single_link/frame_match_build_fix](legacy/single_link/frame_match_build_fix/) | `QAR_FTM_S3_v5_FRAME_MATCH FIXED.zip` |
| [legacy/single_link/frame_match_original](legacy/single_link/frame_match_original/) | `QAR_FTM_S3_v5_FRAME_MATCH (2).zip` |
| [legacy/single_link/idf_6_0_2](legacy/single_link/idf_6_0_2/) | `QAR_FTM_S3_IDF6_0_2_fixed.zip` |
| [legacy/single_link/los_nlos_capture](legacy/single_link/los_nlos_capture/) | `ESP32S3_SingleLink_LOS_NLOS_Anchor_Target_采集代码.zip` |
| [legacy/single_link/python_runner](legacy/single_link/python_runner/) | `Single_Link_Python_Runner.zip` |
| [legacy/single_link/ranging_audit](legacy/single_link/ranging_audit/) | `V5_Ranging_Audit.zip` |
| [legacy/single_link/two_board_baseline](legacy/single_link/two_board_baseline/) | `多AP室内定位.zip` |
| [legacy/single_link/v4_time_window](legacy/single_link/v4_time_window/) | `QAR_FTM_S3_v4_FTM_CSI_SYNC.zip` |
| [legacy/single_link/standalone_plot](legacy/single_link/standalone_plot/) | `ftm_live_plot.py` |

Individual anchor packages that match the eight-anchor bundle are represented by the copy in that bundle. Two uploads of Multi_Anchor_2D_GT and of Single_Link_Live_Ranging had identical source payloads, so each is represented once. The standalone anchor_main.c matches the first two-board anchor after replacing its Wi-Fi passphrase. The two standalone record_serial_2min uploads match the copy under first_two_board_experiment. The two click-to-position script revisions are kept separately.

The GRU V2 entry point combines the ESP-IDF projects and training utilities in 代码.zip with the stable GRU runtime overlay in the same archive. The default smoke-test config disables inference until a locally supplied checkpoint is configured. The A1–A6 scan-based project is kept separate.

The main frame-match test suite skips its private replay tests when their files are missing. The six-anchor tests build synthetic model objects at run time instead of requiring the experimental models. These changes affect test setup, rather than the measurement or solver algorithms.
