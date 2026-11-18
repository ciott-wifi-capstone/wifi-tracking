# QAR-FTM v5 — ESP32-S3 FTM 响应帧 CSI 身份配对版

## 这次改什么？
**只修改 Tag（COM12），Anchor（COM10）维持原本已正常工作的 FTM Responder，不必再烧录。**

**它不是 Wi-PRO 的完整复制**。我们只借鉴“使用 FTM 期间对应包的 CSI，而不是 UDP 前后近邻包”的思路。定位和多径补偿仍未激活；先核实同帧数据。依赖 ESP-IDF 6.0.2 标准 CSI 回调中的 `hdr/payload/rx_seq`，解析 **802.11 Public Action, category=4, action=33 (FTM response)**。如果固件实际没有把 FTM Action 帧 CSI 交给 callback，`CSI_FTM_FRAME` 可能为 0；绝不以近邻普通 CSI 冒充。

### 数据层级
- `CSI`: 兼容旧格式，普通 FTM 会话窗口 CSI，**不等于**逐帧匹配。
- `CSI_FTM_FRAME`: 回调头部明确显示 Action33 才会输出。包含 `round_id, cpu_us, hw_rx_ns, timestamp_precision, src_mac, dialog_token, followup_token, mac_seq_ctl, rx_seq, cwb, rssi, csi_len, csi_hex`。
- `FTM_DETAIL`: 保持旧格式，包含每个报告的 `dialog_token` 与 `t1–t4`。
- 配对工具严格按 `round_id + dialog_token` 唯一对应；有重复或数据缺失则失败。
- 只有可选纳秒时间戳补丁启用、每一轮 >=4 个唯一匹配且共享时钟偏移残差较小时，标为 `TOKEN_UNIQUE_TIMING_COHERENT_PRELIMINARY`。这仍**不代表时钟域已经独立标定**、不等于已实现 Wi-PRO 测距修正。

## 1. 编译 / 烧录 Tag

下载 ZIP，解压到桌面，保证存在：
`C:\\Users\\YOUR_USER\Desktop\QAR_FTM_S3_v5_FRAME_MATCH\tag\main\app_main.c`

用 **ESP-IDF v6.0.2 PowerShell**：

```powershell
cd C:\\Users\\YOUR_USER\Desktop\QAR_FTM_S3_v5_FRAME_MATCH\tag
idf.py set-target esp32s3
idf.py build
idf.py -p COM12 flash monitor
```

Anchor 保持 COM10 供电，**不要再烧**。

预期持续出现 `FTM_DETAIL`。如果固件接收到 FTM 响应帧 CSI，则额外出现 `CSI_FTM_FRAME`。否则可能只有 `CSI` / `CSI_FTM_HW`，这表示尚未取得真正配对所需的 CSI。

## 2. 直接记录到 CSV，自动配对（不必重做大规模实验）

在 Tag 串口 Monitor 内按 `Ctrl + ]` 退出，再在 Windows PowerShell：

```powershell
cd C:\\Users\\YOUR_USER\Desktop\QAR_FTM_S3_v5_FRAME_MATCH
python -m pip install -r requirements.txt
python .\tools\record_serial_2min.py --port COM12 --condition LOS --distance-m 2 --trial FTM_FRAME_V5_01 --seconds 120
python .\tools\pair_ftm_action33.py .\recordings\FTM_FRAME_V5_01
```

这里只是**读取一次现有测距过程**来确认是否有 Action33 同帧 CSI，不要求再换位置或做一整套实验。

如果配对工具显示 `No Action33 CSI appeared`，请不要做距离补偿；此时需要更底层驱动支持获取 FTM 管理帧 CSI，而非强行假设它可用。

## 3. 可选：高精度硬件接收时间元数据补丁（建议先不执行）

`tools/patch_idf_6_0_2_rx_timestamp.py` 借鉴 Wi-PRO 公开补丁利用 ESP32-S3 RX 保留位的原理；会对**ESP-IDF 安装目录里的一个 C 头文件**进行就地修改。只有在确认 Action33 CSI 确实可捕获后，才值得进行这一步。补丁默认 dry-run，不执行任何写入。

```powershell
cd C:\\Users\\YOUR_USER\Desktop\QAR_FTM_S3_v5_FRAME_MATCH
python .\tools\patch_idf_6_0_2_rx_timestamp.py --idf-path "$env:IDF_PATH"
# 仅当上一步确认是 v6.0.2 且输出差异合理后，再运行：
python .\tools\patch_idf_6_0_2_rx_timestamp.py --idf-path "$env:IDF_PATH" --apply
cd .\tag
idf.py fullclean
idf.py build
idf.py -p COM12 flash monitor
```

脚本只在严格匹配到预期字段时才修改，会生成 `.qar_backup` 备份。**不改变 Wi-Fi 闭源固件；运行成功不代表时间戳一定在硬件上有效。** 如果 IDF 在另一目录，使用实际 `IDF_PATH`；如果 git 版本或字段不符，脚本会安全中止。

## 4. 不要误认为准确测距已经完成

这个工程要解决的是第一个瓶颈：**是否能可靠取得 FTM 响应帧自身的 CSI 和配对信息**。
你的已有数据在实际距离 2m 时曾分别给出约 9.375m 与 4.8m 的中位数，两种位置误差相差很大。普通 RTT 平滑和固定偏置减法不能保证未知距离准确。我们在取得同帧 CSI 并核实时间参考之前，不输出伪造的 `corrected_distance`。

下一阶段才是 **硬件残余偏差估计 + CSI 相位校正 + CIR 重建 + 多径稳健补偿**，并必须检查所得结果能否合理泛化。

## 来源和差异
- Wi-PRO 源码： https://github.com/ucsdwcsng/wi-pro-esp32-ftm
- Espressif ESP-IDF FTM 示例： https://github.com/espressif/esp-idf/tree/master/examples/wifi/ftm
- 对齐的帧格式：802.11 public action FTM request=32，FTM response=33。

版本状态：Python 已本地语法和模拟测试；C 固件和头文件修改尚未在 ESP-IDF v6.0.2 + 实体 ESP32-S3 上编译/烧录验证；如果 `idf.py build` 失败，请提供**第一条 error**，不要直接修改旧的可用固件。两套工程独立，原有工程未被覆盖。
