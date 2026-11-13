# 六 Anchor + 一个移动 TAG：CIR 测距、二维定位与 Ground Truth

本包针对你现有 ESP32-S3、ESP-IDF 6.0.2、UART 接口和已经跑通的 CIR 特征/拟合流程扩展。不是 Wi-PRO 对照实验。包含可编辑源码，不含预编译固件。请作为新目录使用，保留现有已成功的单链路项目。

已经完成：Python 数值/协议检查、旧真实录波兼容性回放、界面布局检查。当前环境没有 ESP-IDF 工具链、你的串口和板子，**未完成实际 IDF 编译、烧录或六板射频验证**。固件首次使用须按下面的两板检查，再接入其余五块 Anchor。

## 1. 固定布置与坐标

单位按米处理，坐标系沿用你的标点程序。需要 6 个固定 Anchor + 1 个移动 TAG，共 7 块板子。

| 固定板 | X (m) | Y (m) | 天线高 Z (m) | 固件广播 SSID |
|---|---:|---:|---:|---|
| A1 | 17.52 | 12.56 | 1.70 | RTT_CIR_A1 |
| A2 | 34.80 | 14.25 | 1.70 | RTT_CIR_A2 |
| A3 | 45.56 | 12.68 | 1.70 | RTT_CIR_A3 |
| A4 | 27.11 | 7.06 | 1.70 | RTT_CIR_A4 |
| A5 | 38.09 | 6.20 | 1.70 | RTT_CIR_A5 |
| A6 | 46.85 | 5.46 | 1.70 | RTT_CIR_A6 |

坐标已写入 `anchors.json`，打开主程序就显示这六个固定点及数字坐标，不必重新标锚点。

**默认移动 TAG 天线也固定为 1.70 m**，定位只估计 X/Y。若 TAG 实际固定为另一高度，先改 `anchors.json` 的 `tag_height_m`，再采校准数据。程序按固定高度的空间斜距求 X/Y，不能在手持高度不断变化时仍把距离全部当平面距离。

锚点坐标由电脑端持有，Anchor 无须保存 X/Y。Anchor 只需正确的 A1–A6 编号。使用原厂真实 MAC，TAG 通过唯一 SSID 发现并记录 BSSID；不伪造 MAC。同名 SSID 出现两块板时拒绝该锚点，避免给错误硬件套坐标。

你的布局跨约 30 m，部分链路超过旧模型的 1–15 m 训练距离。旧模型可作为起点，必须使用本次多锚点数据验证。单次 5.12 m 的成功结果不能替代整个布置的误差统计。

## 2. 在你现有的 IDF PowerShell 里准备

把压缩包解压为：

```text
C:\\Users\\YOUR_USER\Desktop\Multi_Anchor_2D_GT
```

继续使用 **IDF_v6.0.2_Powershell**，不是另开普通 Windows CMD。如果已有 `(venv)`，可继续在该终端运行数据处理；烧录脚本会优先调用 IDF 自己的 Python 环境。

```powershell
cd "$env:USERPROFILE\Desktop\Multi_Anchor_2D_GT"
python -m pip install -r requirements.txt
python -m serial.tools.list_ports
```

只看锚点布局、不打开串口：

```powershell
python live_position.py --show-layout --output captures\layout_check
```

首次没有点位数据时，地图只有六个真实配置锚点，TAG 位置为空；不会生成虚构轨迹。关闭窗口即可。

## 3. 分别烧六块固定 Anchor

固定板均用板子的 **UART/CP210x 接口**连接电脑。每次只连接当前准备烧录的那一块，贴好标签。下面 `COM7` 是 Anchor 串口示例，必须替换为上一条 `list_ports` 查到的实际端口；**COM11 目前是你的移动 TAG，不要把 TAG 误烧成 Anchor**。

连接 A1 后执行：

```powershell
python tools/flash.py --role anchor --id A1 --port COM7
```

编译、烧录后进入 monitor，检查类似：

```text
ANCHOR_READY,anchor_id=A1,ssid=RTT_CIR_A1,channel=6,bssid=板子真实MAC,...
RADIO_ANCHOR,requested_ht40=0,reported_bw_mhz=20,...,ftm_offset_cm=0
```

确认编号后按 **Ctrl+]** 退出 monitor，拔下 A1 的数据线，把它放到 A1 的实测位置持续供电。随后换下一块，分别执行下列对应命令；每换一块重新确认串口，不能把这六条对同一块板连续执行。

```powershell
python tools/flash.py --role anchor --id A2 --port COM7
python tools/flash.py --role anchor --id A3 --port COM7
python tools/flash.py --role anchor --id A4 --port COM7
python tools/flash.py --role anchor --id A5 --port COM7
python tools/flash.py --role anchor --id A6 --port COM7
```

脚本为每个编号创建独立 build 和 sdkconfig，防止 A2 复用 A1 的编号。Anchor 控制台为 115200；全部固定在信道 6、20 MHz、FTM responder、硬件距离 offset=0。不要给每块板先加未知硬件 offset 再同时做电脑端偏置修正。

如果六块 Anchor 原本已经正确广播这六个编号，且 FTM/CSI 配置相同，可以保留；旧板仍广播同一个 `RTT_CIR_LAB` 或编号重复时，需按本节重烧。

## 4. 烧移动 TAG：COM11

连接移动 TAG 的 UART 接口，执行：

```powershell
python tools/flash.py --role tag --port COM11
```

这个 TAG 一次烧录后依次测 A1–A6，**无须走到 A2 时改代码或重烧**。默认请求每链路 32 个 FTM 帧、burst_period=2，延续原单链路请求参数；每次最多等 5 秒。串口输出改为 **921600**，减少大量 CSI 文本输出造成的等待。Anchor 仍为 115200，两种串口速率不影响空中通信。

TAG 不逐板建立 Wi-Fi/DHCP 连接，STA 扫描唯一 SSID 后对每个 BSSID 发 FTM；开启管理帧混杂接收用于未关联 CSI。实际能否取得同帧 CSI，须以下面的上板检查为准。不是手机连接六个 AP 的方案。

进入 monitor 后应依次看到：

```text
MULTI_BOOT,...,6,32,2,UART0
DISCOVER,A1,1,真实BSSID,6,信号强度
...
SCAN_START,boot_id,scan_id,TAG时间,none
PROFILE,32,2,6,20
LINK_ID,round_id,TAG时间,A1,真实BSSID,6
FTM,round_id,TAG时间,0,...
CSI_FTM_FRAME,...
...
SCAN_END,...
```

其中 `DISCOVER` 的第三列：1=唯一正常；0=未发现；大于 1=编号重复。成功 `FTM` 的第 4 列是 0；失败/超时会被保留，不能把失败的距离 0 当真实 0 m。

**先只接 A1 和 TAG 做检查**：其他五个未发现是正常的；此时主程序不会算出二维位置，但 A1 应有成功 FTM 和至少 4 对同帧 CIR。之后六个 Anchor 全部上电，再确认六个编号和 BSSID。检查完按 **Ctrl+]** 退出 monitor，释放 COM11，然后才启动 Python 采集。串口只能由一个程序占用。

如果 921600 出现持续乱码/丢行，可降低 TAG 控制台后重烧：

```powershell
python tools/flash.py --role tag --port COM11 --baud 460800
python live_position.py --port COM11 --baud 460800 --output captures\cal_R1 --allow-ood
```

如果 FTM 成功但 CIR 一直为 0，保留 `serial_raw.log`，检查 ESP-IDF 版本、CSI 开关、同帧 Action=33 输出和 20 MHz；不要把原始 FTM 自动冒充成 CIR 距离。可用 `--method raw_ftm` 单独检查 FTM 几何/覆盖，其结果会明确标为 raw_ftm。

## 5. 推荐的 Ground Truth 采集：第一遍 K1–K6，第二遍 P1–P6

**锚点坐标不是移动 TAG 的 Ground Truth。** TAG 放在 A1 附近时，不能直接填 A1=(17.52,12.56)，除非 TAG 天线的水平投影确实在这个点上。人的脚、手、外壳中心也不能替代天线参考点。

先在地上标出 TAG 天线投影的可重复位置，用卷尺/激光和你的坐标原点、X/Y 轴实测坐标。若用参考点加偏移，偏移必须分别沿地图的 X、Y 轴测量：`x_tag=x_ref+dx`，`y_tag=y_ref+dy`，包含正负号。只知道离一个锚点多少米，不能唯一确定二维坐标。若标点程序只给图片像素坐标，先用已知实长校正比例和坐标轴，才能把其输出作为米制 Ground Truth。

**论文初稿推荐做法：**

- 第一遍沿 A1→A2→A3→A4→A5→A6 的可通行路线走，在各锚点附近约 1–2 m 的可站立位置各选一个实测点 K1–K6，标为 **CALIBRATION**，每点先 5 个完整六锚点轮次。
- 第二遍仍按 A1→A6，选六个不同的实测点 P1–P6，标为 **TEST**。建议每个 P 距相应 K 至少 1 m；程序默认拒绝距离校准点不足 0.25 m 的“独立点测试”。每点也先 5 轮。
- 两遍都保持 TAG 天线高度、板子朝向、固定板安装位置一致。先停稳约 3–5 秒，再点采集。按现场走廊/门的实际通路走，不按坐标直线穿墙。
- 不必另外造一大批 NLOS 点。把现有布置下每条链路能否测到、质量和误差记录下来；自动失败记录也必须保留。

这样第一遍有 6 个位置 × 6 个链路的距离条件，不是只有 6 个距离值，适合重新拟合 CIR 系数并检验远距离泛化。每个位置 5 轮是起步采集量，不能据此宣称高统计置信度。

**只想尽快跑通的缩减版：**先在起点附近、中部、终点附近测 3 个分散的 K 点，直接运行 `calibrate.py` 做逐锚点偏置修正，沿用现有成功模型。3 点版不重新拟合 12 个 CIR/FTM 特征系数。后续增加 K4–K6 时可保存在新的校准会话目录，一起作为 `--sessions` 输入，不必重测原 K1–K3。

## 6. 第一遍：运行并采校准点

退出 IDF monitor 后，在同一个 IDF PowerShell 里运行：

```powershell
python live_position.py --port COM11 --output captures\cal_R1 --allow-ood
```

`--allow-ood` 会保留旧模型输出为正、但特征超出旧训练域的距离用于实验，并给它们至少 8 m 的低权重噪声尺度；界面写明 `OOD / low weight`。这不是认定远距离输出可靠。所有原始预测和质量标记均保存。希望严格排除这类链路时改为 `--reject-ood`；有效锚点不足 4 个时位置应当为空，不能强行算位置。

界面操作：

1. 等 A1–A6 表格出现记录，保持 TAG 静止，让至少 4 个有效且几何可用的锚点初始化位置。
2. 走到 K1 的标记点，停稳，输入 `Point ID=K1`、实测 `X (m)`、`Y (m)`、`Scans=5`，保持 `CALIBRATION`。
3. 点 **Collect static point**。命令等待下一个完整六锚点轮次开始，再给该点打标签；坐标不会发给 TAG。
4. 保持天线不动，直到屏幕出现 `K1 | CALIBRATION | COMPLETE | 5/5 scans | You may move now.`。
5. 再走到 K2，改 ID 和实测坐标重复，直到 K6。两点之间的移动数据也保存，但不会被标为这两个点的真值。
6. 收完按 **Stop; keep this window**，等显示 FINISHED，按 **Save PNG** 可截图，再关闭窗口。

每点是自动采完 **5 个完整轮次**后结束该点标签，不会因为已经出现几个好结果就提前结束。总接收和实时显示仍继续，以便你走向下一点。点 **Cancel point** 会把本点标成 ABORTED，不进入拟合。

没有硬编码每点 30 秒：看界面 `Scan time`。例如实际一轮 8 秒，5 轮约需 40 秒，另加等下个轮次的时间；以实测轮询时间为准。

输出目录不能与旧非空会话重名，避免覆盖；重测使用 `cal_R2` 或 `test_R2`。

## 7. 拟合方法与命令

每个 K 点的真距离由已知坐标计算：

```text
r_true = sqrt((x_tag-x_anchor)^2 + (y_tag-y_anchor)^2 + (z_tag-z_anchor)^2)
```

### 路径一：推荐六点版，重拟合 CIR 距离模型

六个 K 点采完且已停止采集后，运行：

```powershell
python refit_cir.py --sessions captures\cal_R1 --out models_gt_R1
python calibrate.py --sessions captures\cal_R1 --models models_gt_R1 --out calibration_gt_R1.json --include-ood
```

第一条继续用原来一致的“LLTF CSI → 缺失子载波重建 → CIR → 同帧特征 → Ridge 拟合距离”流程，重拟合 `FTM linear`、`CIR only` 和 `FTM+CIR` 三个模型。CIR 的峰值对齐后用于统计特征，不把相对峰位直接换算成绝对传播距离。

模型参数选择按 **整个实测位置留一验证**：某个 K 点被留出时，其所有 Anchor 链路、所有重复轮次一起留出；相距不足 0.25 m 的重复位置也归为同一组。标准化仅在各折训练数据内拟合。不随机拆同一点的相邻帧。此处验证分数用于选择参数，不作为独立论文测试精度。

第二条用已保存的 CIR 特征重新计算新模型距离，不需要再采 K 点。然后对每个 Anchor 拟合一个偏置：各 K 点先取本点误差中位数，再对这些中位数取中位数，防止某点多采几轮就占更大权重。

```text
r_corrected = r_model - bias_anchor
```

新模型和偏置文件冻结后，在 TEST 完成前不要再改模型、调 alpha 或用 P 点调整偏置。换模型、换物理板、换天线高度/安装位置或换 FTM burst 配置，必须更新匹配校准；程序会核对模型哈希、几何、BSSID 和固件 profile。

### 路径二：三点快速版，沿用原模型只校正六板偏置

```powershell
python calibrate.py --sessions captures\cal_R1 --out calibration_R1.json --include-ood
```

这一步实际修改的是每个板的距离偏置，不修改原 CIR 系数。适合先检查多锚点覆盖；对于距离相关误差、多径、NLOS 和远距离模型外推，一个常数偏置可能不够。后续测试决定是否改善。

多个校准会话可以合并：

```powershell
python refit_cir.py --sessions captures\cal_R1 captures\cal_R2 --out models_gt_R2
python calibrate.py --sessions captures\cal_R1 captures\cal_R2 --models models_gt_R2 --out calibration_gt_R2.json --include-ood
```

拟合至少需要 4 个得到充分校准的锚点；没有足够成功位置的板会列为 Uncalibrated 并在定位时排除。希望强制六块均完整校准，可给 `calibrate.py` 增加 `--require-anchor-count 6`。若报位置/链路不足，先增加分散 K 点或修复该板覆盖，不能把 TEST 改标成校准来“补齐”。

## 8. 第二遍：冻结模型后的独立测试与截图

六点重拟合版运行：

```powershell
python live_position.py --port COM11 --models models_gt_R1 --calibration calibration_gt_R1.json --output captures\test_R1 --allow-ood
```

三点快速版运行：

```powershell
python live_position.py --port COM11 --calibration calibration_R1.json --output captures\test_R1 --allow-ood
```

在界面把单选框切换到 **TEST**，按 P1→P6 收集，每点填写它自己的实测 X/Y，5 轮后 COMPLETE 再走。不要复用 K 点坐标。`Save PNG` 可保存当前地图和数据状态。最后按停止、等 FINISHED、关闭。

六点版统计：

```powershell
python evaluate_gt.py --session captures\test_R1 --calibration calibration_gt_R1.json --out results\test_R1
```

三点版只把上述校准文件名改为 `calibration_R1.json`。生成：

- `summary.json`：二维误差 MAE/中位数/P90/RMSE、有效定位覆盖率、逐点等权 MAE、各 Anchor 距离误差。
- `point_summary.csv`：各测试点真值、平均估计坐标、误差和有效/应有轮数。
- `position_errors.csv`、`range_errors.csv`：逐条误差和失败原因，供你用 Python 自己复核。
- `test_point_map.png`：实测点与平均估计点，显示六个固定锚点。
- `position_error_cdf.png`：静态测试时的全部有效二维误差 CDF，WLS 和 EKF 分开。

CSV 的 Excel 打开编码为 UTF-8 BOM。失败位置不虚构误差；覆盖率的分母保留应采轮数，不只展示成功案例。没有有效定位的点也会写在报告中。

`--allow-repeat-locations` 只用于另一次在同一物理点重复采集的重复性测试，输出会明确标成 SAME_LOCATION_REPEATABILITY；不能把它写成未见位置泛化效果。

## 9. 实时位置如何计算，以及怎么理解“从 A1 走到 A6”

- 原始 FTM/三种拟合距离和同帧 CIR 数量逐链路保存；默认用 `cir_residual` 即 FTM+CIR。
- 每个完整轮次使用有效距离做加权、Huber 鲁棒非线性最小二乘，至少 4 个锚点。它用于**停稳测点**的扫描定位和误差统计。
- 移动显示使用 [x,y,vx,vy] 的距离 EKF，在每条链路自己的 TAG FTM 会话中间时刻更新，不把跨几秒得到的六个距离冒充为同时测得。首次初始化应保持静止；失去足够新鲜锚点或时间回跳会重新初始化。
- 地图三角形是固定 Anchor；红点是 TAG 估计；绿色是连续轨迹；紫色叉是最近一次静态扫描 WLS。坐标没有被吸附到锚点、路线或输入 GT。
- `Age` 根据 TAG 测量至输出的时间加电脑收到后的时间计算，是测量年龄的**下界**；没有消除 USB/操作系统排队延迟。TAG 会话中点是近似时间，不是外部运动捕捉时间戳。

**六链路轮询不是高帧率定位。** 保守参数下，走路途中可能只能得到低频、带滞后的轨迹，不能保证追上正常步速。先看实际 `Scan time` 和 `Age`，先做走走停停的 P1–P6 采集。

如果覆盖/同帧 CSI 已稳定，可尝试更短 burst 请求后重新校准和测试：

```powershell
python tools/flash.py --role tag --port COM11 --burst-period 1
```

`--burst-period 0` 在 API 中是 **No preference**，不保证 ASAP 或固定高频率。更改后的 PROFILE 不同，旧校准不能直接复用；不能为了图看起来连贯而把插值/高频绘图当作新测量。

**只知道起点终点坐标，不能生成步行全过程 Ground Truth。** 本包把两点之间的移动段保留为无真值轨迹。若要论文里的连续动态定位误差，须额外准备与测量时间对齐的相机/动捕/全站仪等独立位置真值；不能默认匀速插值后把它当测得真值。

## 10. 文件与排错

每次会话目录含 `serial_raw.log`（原始字节）、`ranges.jsonl`（全部链路预测/特征/质量/时间）、`positions.jsonl`（全部定位及失败扫描）、`points.json`（GT 标签和状态）、`host_commands.jsonl`、`session.json`。请整目录保留，不只保留截图。

| 现象 | 先检查 |
|---|---|
| PermissionError / COM11 busy | Ctrl+] 退出 IDF monitor；关闭旧采集程序；只留本程序使用 COM11 |
| 乱码、无 SCAN_START | TAG UART 控制台波特率是否与 `--baud` 一致；确认用 UART 接口；ROM 开机早期 115200 杂字可与应用 921600 不同 |
| 所有 FTM status=-3 / NOT_FOUND | 固定板是否上电、SSID 是否 RTT_CIR_A1–A6、是否信道 6；点 Rescan anchors 后等下一次发现 |
| DUPLICATE_SSID | 两块 Anchor 刷成了同一个编号，按标签重烧对应 A 号 |
| FTM 成功而 CIR_UNAVAILABLE | 查同帧 CSI_FTM_FRAME 与 FTM_DETAIL、至少 4 对、128 字节 LLTF/20 MHz；不要仅看普通 CSI 总行数 |
| 地图只有锚点、TAG 未出 | 是否至少 4 条有效、新鲜、有几何约束的距离；查看 OOD、负值、未校准板、PROFILE/MAC 不匹配 |
| K/P 一直 PENDING | MARK 命令是否能通过 UART 发到 TAG，是否仍运行旧单锚点 TAG 固件；完整一轮结束前不会立即 ACK |
| 点标为 INCOMPLETE/ABORTED | 丢串口记录、采到一半重启/停机或主动取消；该点不参与拟合/独立测试，另起 ID 重采 |
| 估计位置跳动很大 | 看逐 Anchor 距离/偏置变化、OOD/NLOS、距离覆盖、时间跨度和几何；多径误差不能单靠平滑变成精确真值 |

本包不依赖修改 Wi-Fi 库的纳秒时间戳补丁，也不把公开微秒元数据宣称为纳秒级 CIR 绝对 ToF。

软件自检：

```powershell
python -m unittest discover -s tests -v
```

测试中的数值/轨迹是临时生成的算法测试夹具，不是实测六锚点效果；本包没有预填虚假 GT 或误差结果。

ESP-IDF 6.0.2 API 对照：
https://docs.espressif.com/projects/esp-idf/en/v6.0.2/esp32s3/api-reference/network/esp_wifi.html

串口 API：
https://pyserial.readthedocs.io/en/latest/pyserial_api.html
