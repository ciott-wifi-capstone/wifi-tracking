# ESP32-S3 单链路 RTT + 原始 CSI 数据采集 | LOS / NLOS

## 1. 项目用途

先用两块 **ESP32-S3-DevKitC-1U** 完成基础数据采集，不涉及七 Anchor、WLS、GRU、卡尔曼。

- **Anchor**：AP + FTM Responder，固定不动，SSID `RTT_CIR_LAB`，密码 `12345678`，信道 6、20 MHz；UDP echo 服务 192.168.4.1:3333。
- **Target**：STA + FTM Initiator，连接 Anchor，发 UDP 探测包，接收 AP 下行帧 CSI，再做 FTM 测距；Target 串口输出与 `round_id` 关联的 FTM/CSI 记录。
- PC：`record_serial.py` 读取 **Target 的串口**，自动按 LOS/NLOS 和真实距离生成 CSV；`analyze_basic.py` 统计测距误差；`extract_cir.py` 离线计算相对 CIR 特征。

**重要**：CIR 是对 Target 收到的 **Anchor → Target 下行数据包的 CSI** 进行 IFFT 得到的，不是 FTM 管理帧专属 CSI。CSI 在 FTM 会话前的短时间窗口采集，用 `round_id` 关联，代表相近时间内的同一条无线链路。

## 2. 下载和烧录

请用 **ESP-IDF 5.5.x** 优先测试（项目兼容 ESP-IDF 6 的主要 API 变动，但未在真实开发板或本机 IDF 工具链上编译验证）。Windows + VS Code ESP-IDF Extension / ESP-IDF PowerShell 均可。

每块开发板都必须是 `esp32s3` target，且需要在菜单中启用 Wi-Fi FTM。Target 还需要 CSI 支持。项目 `sdkconfig.defaults` 已给出相应选项。如果目录中曾经生成过 `sdkconfig`，重新检查 `idf.py menuconfig` 或删除旧配置再生成。

### 2.1 烧录 Anchor（先）

在 ESP-IDF PowerShell Terminal 输入：

```powershell
cd "<解压路径>\ESP32S3_SingleLink_LOS_NLOS\anchor"
idf.py set-target esp32s3
idf.py build
idf.py -p COM4 flash monitor
```

预期串口有（MAC 会因设备而异）：

```
ANCHOR_READY,ssid=RTT_CIR_LAB,channel=6,bssid=XX:XX:XX:XX:XX:XX,ip=192.168.4.1
```

Anchor 只要 **持续供电**；开始正式实验后不一定需要一直打开 Anchor 的 monitor。

### 2.2 烧录 Target

另开一个 ESP-IDF PowerShell Terminal：

```powershell
cd "<解压路径>\ESP32S3_SingleLink_LOS_NLOS\target"
idf.py set-target esp32s3
idf.py build
idf.py -p COM5 flash monitor
```

预期打印：

```
TARGET_READY,bssid=XX:XX:XX:XX:XX:XX,channel=6
ROUND_START,1,1234567
CSI,1, ... , HEX_BYTES
PROBE_SUMMARY,1,16,16,15,0
FTM_ENTRY,1,0,....
FTM,1,12345678,0,50,35,525,15,-55.2
ROUND_END,1,12345699
```

以上数字只是格式举例。`FTM` 的 `dist_est_cm` 是 **厘米**；`rtt_raw`/`rtt_est` 是 **纳秒**。`FTM_ENTRY` 的逐帧 RTT 是 **皮秒**，这三种单位不可混用。

如果在 monitor 中看到连接成功且有 `FTM`、`CSI`，说明采集链路初步正常。

**重要**：下一步 PC Python 串口采集时必须先按 `Ctrl+]` 退出 `idf.py monitor`，否则 COM5 可能被占用。Windows 中 COM4、COM5 只是例子，请换成你的实际端口。

### 2.3 开始 PC 数据采集

在普通 Windows PowerShell 或其他 Python 终端中：

```powershell
cd "<解压路径>\ESP32S3_SingleLink_LOS_NLOS"
python -m pip install -r requirements.txt
python tools/record_serial.py --port COM5 --condition LOS --distance-m 2.0 --trial LOS_2m_01 --rounds 100
```

Target 持续自动测量；Python 等收到 100 个 `ROUND_END` 后停止并保存。每一次命令代表 **一个固定位置、一个固定遮挡条件**。切换位置/遮挡条件时先结束上一轮记录、记录真实距离、再用一个新的 trial ID 启动。

若想测试 4 m 的人体遮挡：

```powershell
python tools/record_serial.py --port COM5 --condition NLOS --distance-m 4.0 --blocker person --trial NLOS_person_4m_01 --rounds 100
```

如需持续采集到 Ctrl+C，指定 `--rounds 0`。

`recordings/<trial>/` 会生成：

| 文件 | 内容 |
| --- | --- |
| `metadata.json` | 试验编号、LOS/NLOS、真实距离、遮挡类型、备注 |
| `serial_raw.log` | Target 原始串口完整日志（用于排查） |
| `ftm.csv` | 测距时间、状态、原始RTT、估计RTT、测距距离、真实距离、有符号和绝对误差 |
| `ftm_entries.csv` | 每一次 FTM 会话的逐帧 RTT（ps）与 RSSI |
| `csi.csv` | 原始复数 CSI（HEX），时间戳、MAC、RSSI、噪声、信道、LLTF 长度、invalid 标志 |
| `probes.csv` | 每轮 UDP echo 返回数、CSI 记录数、队列丢包数 |

## 3. LOS / NLOS 实验如何做

### 3.1 第一轮建议

先做 LOS **2 m、4 m、6 m**，每个距离 100 轮，至少两次重复。再保持设备摆位和距离相同，加入 NLOS：

- NLOS-PERSON：用人体挡住两天线之间的直接路径；
- NLOS-FURNITURE：用固定家具阻挡；
- NLOS-WALL：若隔墙两侧能测得准确真实直线距离，再测墙体遮挡。

实验记录**天线中心到天线中心的三维直线距离**。如果高度相同，水平距离近似等于真实距离；如果高度不同，使用勾股定理修正。

每个点测完再移动 Target。保持 Anchor 位置、天线朝向、高度、功率、信道和固件版本不变。NLOS 的挡板要真正阻断直线路径，而不只是放在边缘。每种条件重复 2~3 组，交换部分测量顺序以避免时间漂移和环境变化偏差。

### 3.2 建议记录表

| trial | condition | blocker | true_distance_m | rounds | 备注 |
| --- | --- | --- | --- | --- | --- |
| LOS_2m_01 | LOS | none | 2.00 | 100 | 同高度、天线方向固定 |
| LOS_4m_01 | LOS | none | 4.00 | 100 | ... |
| NLOS_person_4m_01 | NLOS | person | 4.00 | 100 | 站立人体位于链路中间 |

如果发现 RTT/CSI 丢失，也 **保留失败轮次**，因为定位可用性同样重要。`probes.csv` 的 `echo_received` 和 `csi_rows` 反映采集情况。不要为提高准确率而只挑较好的数据。

## 4. 测距误差统计

```powershell
python tools/analyze_basic.py --root recordings --output results/basic_error_summary.csv
```

每个 trial 输出成功率、平均有符号误差（bias）、平均绝对误差（MAE）、均方根误差（RMSE）、中位绝对误差、P90 绝对误差、CSI 包数量。

关键公式：

- 有符号误差 `e = dist_est_cm / 100 - true_distance_m`
- `MAE = mean(abs(e))`
- `RMSE = sqrt(mean(e*e))`
- `P90 = percentile(abs(e), 90)`

LOS 和 NLOS 的对比应该在**相同真实距离**下进行，避免把距离差异误判为遮挡影响。

## 5. 离线 CIR 特征

```powershell
python tools/extract_cir.py --root recordings --output results/cir_features.csv
```

处理流程：

1. 每 2 字节表示 1 个复数 CSI，顺序是 `[imag, real]`，按 `real + j*imag` 复原。
2. 只接收 20 MHz LLTF、128 字节的 64 个复数子载波；不符合长度的帧暂不处理。
3. 官方 LLTF 子载波输出顺序 `0~31,-32~-1` 与标准 64 点 IFFT 的频率 bin 顺序匹配，可做 `ifft(H, 64)`。
4. `first_word_invalid` 为真时，前 4 字节（2 个复数子载波）无效，代码将它们清零而不是错位移动。
5. 计算 CIR 峰值能量比、相对 RMS 时延扩展、尾部能量比例和能量熵。

**限制**：这些特征是基于相对 CIR 的描述，未校准硬件相位/时延，不能把 CIR 峰值位置当作传播时延真值。20 MHz 的时延栅格是 50 ns，对应理想长度差约 15 m，无法清晰分辨相隔几米的室内路径。不要把 50 ns 当作 RTT 定位精度。

## 6. 常见故障

1. **Target 找不到 AP**：Anchor 是否已烧录并供电，两个 SSID/密码是否一致，注意不要连到了实验室公共 Wi-Fi。
2. **FTM status 非 0**：检查双方 `CONFIG_ESP_WIFI_FTM_ENABLE=y`，且 AP `.ftm_responder = true`；信号太弱或会话超时也会造成失败。
3. **没有 CSI**：Target 是否已启用 `CONFIG_ESP_WIFI_CSI_ENABLED=y`；观察 `PROBE_SUMMARY` 里 UDP 回包和 CSI 计数，检查 CSI 回调 BSSID 过滤、20 MHz、正确日志端口。建议先在无遮挡 1~2 m 验证。
4. **CSI 数量少**：当前程序每轮发 16 个 UDP 探测并回包，另可能采集到 AP 的其它帧；可以适当增加 `PROBES_PER_ROUND`，同时注意串口带宽和缓存丢失。
5. **FTM / CSI 混杂**：两种数据用同一个 `round_id`，但采集窗口不同；记录 `t_us` 便于精细筛选，且用 `PROBE_SUMMARY` 验证采样是否足量。
6. **烧录完成后 Python 打不开 COM 口**：先退出 `idf.py monitor`，并确认 Target USB 端口。
7. **`sdkconfig.defaults` 修改后没有生效**：打开 `idf.py menuconfig` 检查已经生成的 `sdkconfig`；不要只改 defaults 却保留旧配置。
8. **烧录后程序重启**：贴出完整启动和 panic 日志；如果是 USB console 配置，确认烧录/日志使用同一个正确的 COM 口。

## 7. 版本与验证声明

- API 依据：ESP-IDF 官方 ESP32-S3 FTM 和 CSI 文档、官方 ESP-IDF FTM 示例。
- 已检查：Python 串口行解析、误差统计、CSI IFFT 特征计算（使用模拟日志的自动化测试），工程文件和 C 源结构。
- **尚未完成**：真实 ESP32-S3 上的 `idf.py build`、烧录、双板实测；若你的 ESP-IDF 版本具体字段不同，需要参考报错针对性微调。

项目刻意**不嵌入 LOS/NLOS 标签到固件**，因为同一套固件应该在不同真实实验条件中保持一致；场景标签由 PC 采集时准确记录。
