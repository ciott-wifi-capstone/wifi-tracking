# ESP32-S3 七 Anchor：RTT + CIR 采样、标定和误差比较

本工程基于用户上传的 Python V1，而**不是**完整可直接烧录的 ESP-IDF 工程。原始 ZIP 中仅有 Python 程序，没有 Tag / Anchor C 源码；`firmware_reference/` 是需要人工集成的参考模块，未在真实板卡编译和验证。所有误差必须由用户**实际实验**产生，程序没有内置“更好的 CIR 结果”。

## 0. 先决条件

- 保留 `anchors_7.json` 中原有 A1、A2、A3、A4、A5、A6、A8 坐标；**现场必须重新测量坐标并核对**。
- 同一场景的 Anchor 位置、Tag 高度、Wi-Fi 信道/带宽、天线方向、FTM 配置和采集间隔尽量保持不变。
- 本原型**只解析 20 MHz LLTF 128 字节 CSI**。40 MHz、HT-LTF、STBC 等数据不能假装成 LLTF 来解码。
- 根据官方格式，将每两个有符号字节读为一个子载波的 `(Imag, Real)`，排列为 k=0..31、-32..-1。对有效载波做 64 点 IFFT，得到相对 CIR 形状。
- ESP32-S3 的 CSI 如 `first_word_invalid=true`，前四个字节无效，程序将其对应的两个复数点设为 0；会产生谱失真，需要在实验讨论中写明。
- **不能将峰值索引当成直达径 ToF，也不能将 CIR delay spread 当成亚米级时间分辨**。20 MHz CIR 基础时延采样间隔为 50 ns（约 15m 传播距离差）；默认没有 CFO/SFO/TOF 绝对校准。
- FTM 报告的 `corrected_m` 被用于距离；请检查固件是否有按真实测试集预先标定的偏移，否则会引起数据泄漏。

## 1. 固件端改动（必须先做）

原固件只向 Python 输出 `MFUSED` 中的 CSI 统计数据。必须改到每个 Anchor 的 FTM 时间窗口还能输出**对应 AP 的原始 CSI**，否则 `CIR` 模式不会出位置。

新增通信协议（一行一个 CSI 包）：

```
CSIRAW,1,<round>,<esp_us>,<anchor_id>,<source_mac>,<channel>,20,LLTF,<first_word_invalid>,<128字节CSI的base64>
```

固件中打开 ESP-IDF 的 CSI 功能；参考 `firmware_reference/cir_telemetry.c/.h`。回调中**不能做 IFFT 或连续打印**，应只拷贝少量原始数据给队列，然后由普通 FreeRTOS 任务输出。只允许与本轮 Anchor BSSID 匹配的帧进入统计；使用 `esp_timer_get_time()`，与 `MFUSED.esp_us` 同一时间基。每 Anchor 每轮先控制在 5～12 包左右。`MROUND_META` 输出之前应排空本轮 CSI 队列。

建议最初使用 LLTF 20 MHz，保持 Wi-Fi 环境一致；若原系统固件配置不符合以上要求，先调整兼容性。此原型中的 `CSIRAW` 不一定来自 FTM 测距所用帧，仅要求同一 AP、同一测量窗口；通过比较实验研究其预测能力。

同时确认串口带宽足够，必要时 Tag UART 控制台与 PC 端统一为 460800。尤其是 **ESP-IDF monitor、Dashboard、数据采集脚本只能有一个进程占用同一 COM 口**。

## 2. 现场取样步骤

### 单 Anchor 预实验

A1 与 Tag 直线设置 1、2、3、4、6、8 m（实验室允许范围内）。每个距离 60～100 个 FTM round。每点至少三次重复，考虑设备冷启动/不同日期；每组同时采集同 AP、同轮次的 3 包以上 CSI。用卷尺标定地面坐标和高度，按三维距离计算 Ground Truth，记录 `corrected_m - true_distance`。增加人的遮挡、墙遮挡，对应数据按 `LOS`、`NLOS_human`、`NLOS_wall` 等标签存储。

### 七 Anchor 定位

保留你现场实际的七个 Anchor（编号 1,2,3,4,5,6,8）。在覆盖区选择 12～20 个真实坐标已知的 **静止** 测试点，每点建议 60～100 个完整 round；让每个测试点独立成一个 session/log 文件。Tag 固定相同朝向/高度做主要比较，再用另一组朝向测泛化能力。再做有人移动、屏蔽部分链路的测试。移动 Tag 轨迹必须另有逐时刻 GT，**不能**拿一个固定 (x,y) 给整条轨迹打标签。

不要先按每条 MFUSED 行随机分训练/测试。这种方法因为同一点的相邻记录高度相关，会大幅高估 CIR 效果。应当**按测量点/采集 session 划分 train/test**，最好把第二天和全新点留作测试集，并在比较前冻结参数。

## 3. Windows 采集方法

在项目目录：

```powershell
python -m pip install -r requirements.txt
python lab_pipeline.py capture --port COM5 --baud 115200 --output recordings/P01_day1.log --seconds 180
```

串口若统一改成 460800，两个地方同时改用 `460800`。`--seconds` 不填则 Ctrl+C 结束。**一份日志只对应一个静态测试点**。该文件保留 `MROUND_START / MFUSED / CSIRAW / MROUND_META` 全部原始报文，后续四种算法均重放**同一份**数据。

原 Dashboard 也将所有串口行写到 `results_7anchor_epoch/raw_serial.log`，如需一边看图一边录制可以使用其日志，**切勿**同时打开两次 COM5。

### 标记 Ground Truth

复制 `experiment_manifest_TEMPLATE.csv` 为 `experiment_manifest.csv`，把中文占位坐标换成真实米制浮点数。CSV 最少包含：

`session_id,split,condition,true_x_m,true_y_m,true_z_m,log_file`

`split` 只允许 `train` 或 `test`。每个 session 应用一个日志文件，并且每份日志仅包含**同一个静态坐标**。

## 4. 从真实日志得到每条链路的测距误差

```powershell
python lab_pipeline.py features --manifest experiment_manifest.csv --output lab_results/link_dataset.csv
```

同时自动输出 `lab_results/ranging_summary.csv`，汇总各场景、各 Anchor 的 RTT 测距偏差、MAE、RMSE、中位误差、P90 和 CIR 匹配率。

输出 `link_dataset.csv`：逐条 `session,round,anchor` 有 `true_distance_m,measured_distance_m,signed_error_m,abs_error_m`，以及 `cir_packets,cir_peak_ratio,cir_rms_delay_ns,cir_offpeak_ratio,cir_entropy`。

### 四种 CIR 特征各自含义

- `cir_peak_ratio`: CIR 最大 tap 能量占总能量的比值。
- `cir_rms_delay_ns`: **以最大峰为参考**、循环时延坐标上得到的相对 RMS 能量扩散，单位 ns；不是已经校准的物理 RMS delay spread。
- `cir_offpeak_ratio`: 最大 tap 两侧 ±1 tap 之外的能量比例；不能直接叫“后续反射路径功率比”。
- `cir_entropy`: 64 个 tap 功率归一化后的熵，越高表示越分散，但不必然表示误差越大。

## 5. 用真实距离误差标定 CIR 权重

```powershell
python lab_pipeline.py calibrate --dataset lab_results/link_dataset.csv --output cir_model.json
```

只有 `train` 数据参与拟合。当前是无需 sklearn 的、带正则化和按 session 平衡加权的简化模型：从 4 类 CIR 特征和有效包数回归预测 `log(max(abs_error, 0.35m))`。它预测的是**绝对测距误差尺度**，不是统计意义上严格校准的高斯标准差。参数和检验结果依赖真实采集数据。

在 `anchors_7.json` 设置：

```json
"cir_weighting": {
  "mode": "cir",
  "min_packets": 3,
  "max_packets_per_link": 32,
  "tolerance_ms": 650,
  "packets_full": 5,
  "sigma_min_m": 0.35,
  "sigma_max_m": 8.0,
  "calibration_file": "cir_model.json"
}
```

配置里保留其他现有字段即可。未训练模型时只能使用**启发式 CIR 权重**，不能当成已验证的误差估计。

## 6. 公平对比四个算法（最重要）

```powershell
python lab_pipeline.py compare --manifest experiment_manifest.csv --model cir_model.json --output lab_results
```

离线重放所有日志，依次运行：

1. `rtt`: RTT + WLS；
2. `csi`: 原 V1 CSI 统计特征 + WLS；
3. `cir`: CIR 特征预测测距误差 + WLS；
4. `hybrid`: CSI + CIR 混合权重 + WLS。

默认使用同一个 WLS 求解器、Huber 和 Kalman，唯一改变权重。输出：

- `lab_results/position_detail.csv`: 每轮定位坐标与原始/滤波后的二维定位误差；
- `lab_results/position_summary.csv`: 按 train/test、环境和模式汇总 MAE、RMSE、Median、P90；
- `lab_results/position_summary_common_rounds.csv`: 只对四种模式**共同成功**的相同 round 计算同一组指标，避免 CIR 缺帧造成幸存者偏差；
- `lab_results/coverage.csv`: 每个模式成功定位的轮数占比，以及有效 CSI/CIR 匹配数量；
- `lab_results/replays/<session_id>/<mode>/links.csv`: 各 Anchor 权重、CIR 特征、WLS 距离残差与使用的 method。

**务必同时汇报定位成功率**：CIR 模式严格要求足够的匹配 CSI 包，不足就不输出位置；如果只比较它成功的少数轮次，可能产生幸存者偏差。进一步公平对比时，应当对所有方法只取共同成功的 round 计算误差，同时单独报告覆盖率。

定位误差（与真实坐标比较）为：

`position_error = hypot(x_pred - x_true, y_pred - y_true)`

WLS 内部的 `rms_m` 是方程残差，**不能当作真实定位误差**。

可选画图：

```powershell
python -m pip install matplotlib
python plot_lab_results.py --results lab_results
```

生成 CIR 特征与 RTT 绝对测距误差散点图，以及不同模式的定位误差 CDF。

## 7. 实时 Dashboard

```powershell
python dashboard_7anchor_epoch.py --port COM5 --mode csi
python dashboard_7anchor_epoch.py --port COM5 --mode cir
python dashboard_7anchor_epoch.py --port COM5 --mode hybrid
python dashboard_7anchor_epoch.py --port COM5 --mode rtt
```

CIR 模式接收不到同轮同 AP 的 `CSIRAW` 包，**就不允许悄悄退回 CSI**，而是跳过该 Anchor；如果有效 Anchor 数少于三或共线，就不输出位置。混合模式如果单条链路缺失 CIR，会明确写成 `csi_fallback`。

## 8. 预实验验收清单

- [ ] 串口没有冲突、没有明显截断、`CSIRAW` 解析成功；
- [ ] LLTF 20 MHz 实际输出 128 字节而不是 256 或 384 字节；
- [ ] source_mac 与对应 MFUSED 的 BSSID 一致，轮次/时间戳吻合；
- [ ] 单 Anchor 至少 3 个匹配 CSI 包（建议 5 个以上）；
- [ ] A1～A6、A8 至少 3 个非共线 Anchor 有效；
- [ ] 链路真实距离对照已经测量，配好设备高度；
- [ ] test 点/天与 train 独立，标定结果不泄漏测试集；
- [ ] 同时汇报误差、成功率、计算耗时/延迟；
- [ ] 列出无法解析或掉包的比例。

参考：Espressif ESP32-S3 Wi-Fi CSI 官方文档、ESP-IDF `esp_wifi_set_csi_*()` API、Espressif `esp-csi` 示例。最终若需高精度物理 CIR/NLOS 分析，仍需针对实际相位偏差、子载波缺失、带宽与设备时钟进行额外标定。
