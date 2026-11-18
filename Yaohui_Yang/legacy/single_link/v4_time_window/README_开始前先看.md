# QAR-FTM v4：CSI 与 FTM 同步窗口采集

请先阅读 **SYNC_DESIGN_必读.md**。这一版与旧版的核心区别是：**不再先通过 UDP 探针采集 CSI 再发起 FTM**，而是在 `esp_wifi_ftm_initiate_session()` 前开启专用 CSI 窗口，在该 FTM 会话结束后关闭。

**重要限制：** ESP-IDF 6.0.2 公共 CSI 接收时间戳仅有微秒精度，因此 v4 是“同步采集 + 候选对应”，不是 Wi-PRO 论文级的“同一帧高精度硬件配对”，也不会伪称已完成高精度距离补偿。真正严格配对需要修改/验证底层时间戳 API，并复核 FTM 帧身份。Wi-PRO 官方仓库的相应补丁针对 ESP-IDF v5.5，不能未经移植就保证用于 v6.0.2。

保留 Anchor 当前已成功工作的固件。只需将新目录中的 `tag` 按 ESP-IDF v6.0.2 编译烧录到 COM12。`tools/record_serial_2min.py` 新增 `csi_ftm_hw.csv`。如果该文件没有记录，当前驱动就没有公开所需的同会话 CSI，不能进行物理层配对和补偿。
