# QAR-FTM v4 | FTM 期间同步捕获 CSI（不是精准配对成品）

## 重要区别
- 原版先 UDP Probe 获得 CSI，再发起 FTM；数据按 `round_id` 近邻关联。
- **v4 在 FTM 会话期间开启 CSI 缓冲**，关闭 UDP Probe，只有 FTM 窗口内收到的 Anchor 来源 CSI 被保存。
- 每条 CSI 额外输出 `CSI_FTM_HW`，包含驱动的 `rx_ctrl.timestamp`（默认单位为微秒）以及精度标记。
- 旧 FTM/FTM_DETAIL/CSI 格式不变，采集脚本新增 `csi_ftm_hw.csv`。
- 时间窗口并不保证 CSI 来源是 802.11mc FTM Action 帧。通常的普通 CSI 回调也不含完整管理帧负载。若 FTM 窗口的 CSI 为零，不应伪造匹配结果。
- 绝对**不要**凭同一 round_id 或 `esp_timer_get_time()` 关联后使用 Wi-PRO 的纳秒级补偿。

## 为什么不能声称完全精确配对？
普通 ESP-IDF v6.0.2 公开的 `wifi_pkt_rx_ctrl_t.timestamp` 是微秒级、通常标记包接收完成。与 FTM 报告的皮秒单位 `t2` 相比，它不足以提供 Wi-PRO 所需的精确对应。Wi-PRO 使用适用于 ESP-IDF 5.5 的私有补丁，暴露 `rxstart_time_cyc` 和 `rxstart_time_cyc_dec` 来得到高精度 RX 起始时间。这一补丁**未经移植到 v6.0.2 验证**，不要直接强行 `git apply` 到当前 Windows 安装。

如未来成功将兼容的宏 `WIFI_PKT_RX_TIMESTAMP_NSEC` 实装到当前 ESP-IDF 版本，Tag 会在编译期检测它并令 `hw_timestamp_precise=1`；但仍须核对 FTM 和 CSI 是否确实属于同一帧，而不是两帧恰好时间相近。建议增加管理帧序列号、BSSID 和 TX/RX 标识进行交叉检查，并验证硬件时钟来源。

## 操作（只需升级 Tag）
将 tag 文件夹放到你新的独立目录，使用 ESP-IDF v6.0.2 PowerShell：

```powershell
cd C:\\Users\\YOUR_USER\Desktop\QAR_FTM_S3_v4_FTM_CSI_SYNC\tag
idf.py set-target esp32s3
idf.py build
idf.py -p COM12 flash monitor
```

Anchor(COM10)沿用已知可用版。由于没有实体 ESP32 和本地 ESP-IDF 工具链，本工程尚未实机编译，须在 Windows 编译后才能确认。

正常应见 `FTM_SYNC_START`、`CSI`（如果支持相关 FTM 帧采集）、`CSI_FTM_HW`、`FTM_DETAIL`、`FTM_SYNC_END`。如果 `CSI_FTM_HW` 长期为空，不表明 CSI 已经自动和 FTM 配对，说明当前驱动未把所需 CSI 暴露出来，需要移植底层补丁。

## 串口记录及候选匹配
```powershell
python .\tools\record_serial_2min.py --port COM12 --condition LOS --distance-m 2.0 --trial SYNC_2m_01 --seconds 120
python .\tools\pair_ftm_csi_candidates.py .\recordings\SYNC_2m_01
```
这个工具只做诊断，微秒级时间戳一律输出 `COARSE_US_ONLY_NOT_EXACT`，不会宣称精确配对，更不会输出伪造的修正距离。

## 下一步真正的物理层精确补偿
1. 基于适配当前目标 ESP-IDF 的补丁，获取纳秒级接收起始时间戳；不要只用 `esp_timer_get_time()`。
2. 验证 `t2` 与 CSI 的硬件时钟域及时间参考点一致。
3. 结合帧 MAC/序列号/类型唯一匹配，并记录匹配率。
4. 确定为同一帧且 CSI 子载波有效之后，才启用相位修正、CIR 重建和多径补偿。

本实验阶段**不保证更高测距精度**，只修复测量设计中“CSI 和 FTM 先后采集”的问题。
