# QAR-FTM ESP32-S3 单链路实验工程（中文版）

**目标**：两块 ESP32-S3-DevKitC-1U 先把单链路 FTM 测距的数据采得完整、可靠，再判断电磁信道 CSI 特征是否能预测多径引起的测距偏差。暂不实现多 Anchor 定位。

**实事求是的限制**：本包是一套**可在 ESP-IDF 中构建、烧录的源代码工程**，不是预先构建好的 `.bin`，因为作者运行环境没有 ESP-IDF 工具链或两块 ESP32-S3 实体板，没有做过真实 `idf.py build`/flash/hardware verification。如果你的 ESP-IDF 安装或端口不同，按编译器反馈修正。不要把本包视为已经实测达到米级精度的产品。

## 1. 和 Wi-PRO 的区别

Wi-PRO：为获得精确配对的 CSI/FTM 时间戳而修改 ESP-IDF；相位校正、子载波 L1 恢复、上采样 CIR、Leading Edge Detection；**直接修正 ToF**。GitHub: https://github.com/ucsdwcsng/wi-pro-esp32-ftm

QAR-FTM（当前工程）：使用标准 ESP-IDF 的公开 FTM 与 CSI API，**不改 Wi-Fi 射频底层驱动**；保留原始 FTM、逐帧 RTT 和四时间戳；在 FTM 前用 UDP echo 提取同链路的普通数据包 CSI；记录实际带宽；通过 PC 的 Python 对频域幅度起伏、相对 CIR 和 RTT 系统偏差进行诊断。现在**没有**将 CSI 前沿换算为绝对距离，也**不把未经验证的补偿结果伪装成实测结果**。

因此两者不是同一算法。当前程序的 CSI **不能**当作与 FTM 管理帧高精度时间戳配对的 CSI。

## 2. 文件说明

- `anchor/`：AP + FTM Responder，SSID=RTT_CIR_LAB，WPA2 密码=12345678，信道 6，UDP echo 端口 3333。
- `tag/`：STA + FTM Initiator；默认 32 帧，记录单帧 `t1/t2/t3/t4`、RTT 和 FTM 总结果；CSI 回调只进队列，串口由单独任务输出。
- `tools/record_serial_2min.py`：兼容你原始 CSV 协议，新增 `ftm_detail.csv`；仍保留 `ftm.csv`、`csi.csv`、`ftm_entries.csv`、`probes.csv`。
- `tools/analyze_recordings.py`：对旧 ZIP 或新采集的 recordings 统计距离误差和**相对** CSI 特征。不会训练不可信的小样本模型。
- `example_results/`：程序在你已上传的 `recordings(2).zip` 上计算出的汇总结果。

## 3. 构建与烧录（Windows，ESP-IDF PowerShell）

先把本 ZIP 解压到 `C:\\Users\\YOUR_USER\Desktop\QAR_FTM_S3`（也可以在其他路径，但以下命令必须对应）。

**Anchor（先烧 COM10）：**

```powershell
cd C:\\Users\\YOUR_USER\Desktop\QAR_FTM_S3\anchor
idf.py set-target esp32s3
idf.py build
idf.py -p COM10 flash monitor
```

烧完后可退出 `idf.py monitor`（按 `Ctrl+]`），**Anchor 保持通电**。

**Tag（后烧 COM12）：**

```powershell
cd C:\\Users\\YOUR_USER\Desktop\QAR_FTM_S3\tag
idf.py set-target esp32s3
idf.py build
idf.py -p COM12 flash monitor
```

亦可用 `build_flash_anchor_COM10.ps1` / `build_flash_tag_COM12.ps1` 脚本（必须在 ESP-IDF PowerShell Terminal 中运行）。

如果出现 `idf.py is not recognized`，你用的是普通 PowerShell，而不是 ESP-IDF PowerShell。烧录 COM 口以你当前设备管理器为准。

**USB 输出设置**：`sdkconfig.defaults` 默认尝试 USB Serial/JTAG 控制台，适合 ESP32-S3 原生 USB。如果你实际连接 CP210x UART0 而不是原生 USB 口，可执行 `idf.py menuconfig` 调整 `ESP System Settings > Channel for console output` 为 UART 并重新 build。每个工程要独立配置。

第一次生成 `sdkconfig` 后，更改 `sdkconfig.defaults` 不会自动覆盖现有 sdkconfig。必要时用 `idf.py menuconfig` 检查，或在备份后运行 `idf.py fullclean` 并重新配置。

**特别注意**：两个工程 `main/lab_config.h` 必须保持一致。默认 `LAB_USE_HT40 0`，先重复旧的 HT20 基线；完成后再**两边同时**改成 1 并重新编译烧录，进行 HT40 对照。不能用 `LAB_USE_HT40 1` 就声称真实空口已经是 40 MHz。须检查串口 `RADIO_*` 和 CSV `cwb`、`len_bytes`。

**FTM offset**：Anchor 的 `LAB_FTM_OFFSET_CM=0`；没有独立标定之前禁止改动它。测试时不能把统计补偿和驱动偏移叠加两遍。

## 4. 串口记录格式

Tag 串口示例（数值仅举例）：

```
TARGET_READY,bssid=aa:bb:cc:dd:ee:ff,channel=6
RADIO_TAG,requested_ht40=0,reported_bw_mhz=20,primary=6,secondary=0
ROUND_START,1,1234567
CSI,1,1235000,aa:bb:cc:dd:ee:ff,-51,-94,6,0,0,0,128,0,....HEX....
PROBE_SUMMARY,1,16,16,15,0
FTM_ENTRY,1,0,55000,-54
FTM_DETAIL,1,0,3,55000,-54,120000000,120000040,120000060,120000110
FTM,1,1400000,0,55,55,825,15,-54.0
ROUND_END,1,1450000
```

`FTM` 距离字段 `dist_est_cm` 为**厘米**，`rtt_raw_ns` / `rtt_est_ns` 为**纳秒**，单帧 `rtt` 和 `t1`–`t4` 为**皮秒**。`CSI` 内容是**AP 原生数据包**，不是 FTM Action Frame CSI。通信条件或串口设置会导致 CSI 数量变化。

你原来的采集器可以继续识别 `FTM`/`FTM_ENTRY`/`CSI`/`PROBE_SUMMARY`。本包工具额外保存 `ftm_detail.csv`。

## 5. 每组采集 120 秒

先退出 COM12 的 `idf.py monitor`（`Ctrl+]`），再打开**普通 Windows PowerShell**：

```powershell
cd C:\\Users\\YOUR_USER\Desktop\QAR_FTM_S3
python -m pip install -r requirements.txt
python .\tools\record_serial_2min.py --port COM12 --condition LOS --distance-m 2.0 --trial LOS_2m_v2_01 --seconds 120
python .\tools\record_serial_2min.py --port COM12 --condition LOS --distance-m 3.0 --trial LOS_3m_v2_01 --seconds 120
```

每次命令结束后再运行下一个。保持 Anchor 通电，移动 Tag 时更新真实距离。在首次验证时推荐 **2m → 3m → 2m → 3m**，独立重复摆放至少 3 次，再试 6m LOS 和 6m NLOS。新采集须使用新的 `trial` 名，不要覆盖旧数据。

**从既有 ZIP 开始做分析：**

```powershell
python .\tools\analyze_recordings.py --input "C:\\Users\\YOUR_USER\Desktop\recordings(2).zip" --out results_old
```

**分析新采集的 recordings 文件夹：**

```powershell
python .\tools\analyze_recordings.py --input recordings --out results_new
```

输出两个表：`ranging_summary.csv`、`csi_relative_features.csv`。

## 6. 物理理论与合理边界

在多径电磁信道里，接收电场是多个复数波贡献的矢量和：

`E_rx(f) = Σ A_k(f) · exp(-j2π f τ_k)`。

反射系数受材料介电常数、入射角和极化方式影响。不同传播路径在不同频率上的相长/相消干涉，导致 **CSI 幅度在子载波上起伏**。可用幅度变异系数、子载波幅度起伏率、相对 CIR 熵作为**可检验的信道状态特征**。

`ifft(CSI)` 的时间零点不一定对齐电磁波真实首径。特别是普通 20 MHz 数据，分辨率很有限，不能把 CIR 峰值的 index 当成传播距离。`csi_relative_features.csv` 只能用来比较无线信道状态；**不能声称已实现真实 ToF 修正或已经测准**。

FTM 利用双向时间戳测量传播延迟；如果硬件时间戳受多径、接收门限和 RF 延迟影响，常会发生系统性偏差。普通均值/中位数降低随机误差，不能消除共同偏差。先诊断偏差，再做带独立验证的补偿。

## 7. 试验阶段与升级路线

1. **V0（本包）**：验证固件输出，保留 20MHz 原始基线，排查 2m 测成 9m 的异常，记录 FTM 四时间戳与 CSI。
2. **V1**：在多个独立重复摆放的 LOS 数据上判断 FTM 偏差是否受天线、方向和硬件配置主导；比较原始均值、逐帧中位数与稳健回归校准。
3. **V2**：对照 HT20/HT40（必须查实空口数据），提取 CSI 的频域起伏/相对 CIR 等特征，独立验证其对偏差和失败风险的预测增益。
4. **V3**：仅在能够精确配对 FTM 与 CSI 底层接收时戳的条件下，研究物理级 ToF 校正；此路线将涉及更深驱动能力，需要独立项目，不属于本包。

**不要**仅在相同 120s 测量的随机帧上训练/验证并宣称泛化。要按独立**摆放轮次、距离、房间**拆分测试；同时报告 MAE、P90 和不可靠结果比例。当前的十组是十次位置/场景配置而非大量独立环境，尚不适合高容量模型训练。

## 8. 常见报错

- `FTM status !=0`：确认 `CONFIG_ESP_WIFI_FTM_ENABLE=y`，Anchor 的 `ap.ftm_responder=true`，双方 Wi-Fi 连接正常。
- `No CSI`：检查 `CONFIG_ESP_WIFI_CSI_ENABLED=y`、成功连接、UDP echo 回包及输出队列是否出现丢弃。
- `NO RADIO_TAG`：Tag 未完成与目标 AP 的关联或未获取 BSSID；先确认 SSID/密码一致。
- `cwb=0`：说明那条 CSI 帧是 20MHz，不能仅依据 `RADIO_TAG` 的配置值宣称 HT40 数据到手。
- `COM12 access denied`：退出正在占用串口的 `idf.py monitor`。
- 串口不可见/无输出：检查板上原生 USB 与 CP210x 口差异、线材、Console 配置。

---

源代码面向 ESP-IDF 5.5.x；与 ESP-IDF 6.0 不保证完全二进制或编译兼容。使用和改动前请备份原始可用的 Anchor/Tag 工程。Wi-PRO 并未被复制进本包。

## 9. `RANGE_DIAG`（新增的设备端稳健诊断）

Tag 还会输出：`RANGE_DIAG,round_id,raw_m,p25_m,median_m,mad_m,nvalid`，来自本次 FTM 单帧 RTT 的设备端排序统计。

- `raw_m`：ESP-IDF 官方会话距离。
- `p25_m`：单帧距离低四分位数候选值；有时可减少明显偏大的尾部观测，但并不保证无偏。
- `median_m`：单帧距离的中位数。
- `mad_m`：单帧距离的 MAD，描述同一会话的离散程度。
- `nvalid`：有效单帧数。

这些数据也保留在 `serial_raw.log` 中。**它们不是已经完成硬件偏差修正的真实距离。** 由于你的 3m LOS 测得约 8.25m 的主要问题是系统性偏差，不能指望 MAD/中位数消除约 5m 的偏差。
