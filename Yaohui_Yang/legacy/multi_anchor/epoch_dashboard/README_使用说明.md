# ESP32-S3 7 Anchor 严格 Epoch 实时定位（A7 禁用）

本程序是为当前这套 7 Anchor 固件重新写的电脑端程序，不再使用旧 6 Anchor 的历史测量融合方式，也默认不加载旧 GRU/NN 模型。

启用 Anchor：A1、A2、A3、A4、A5、A6、A8。

坐标：
- A1 = (45.67, 5.07) m
- A2 = (28.30, 14.62) m
- A3 = (27.42, 6.40) m
- A4 = (17.24, 5.10) m
- A5 = (35.40, 6.40) m
- A6 = (15.53, 13.26) m
- A8 = (35.49, 14.18) m

## 算法变化

1. **严格 Round/Epoch**：只使用同一个 `round_id` 内的 MFUSED 数据；绝不拿上一轮 Anchor 补当前轮。
2. **单次距离优先**：使用当前 FTM exchange 的 `corrected_cm`，不使用跨轮 median `filtered_cm` 作为定位输入，减少运动滞后。
3. **RANSAC-like 子集初值**：7 个 Anchor 中枚举 4 Anchor 子集，选择对全体测量最一致的初值，降低单个障碍物/NLOS Anchor 把位置拉跑的风险。
4. **动态权重**：综合 FTM entries、RSSI、CSI 是否存在、该 Anchor 历史 MAD 稳定性、Epoch 内时间差、Huber 残差和 NLOS 概率。
5. **非对称 NLOS**：FTM 在多径/NLOS 下更常表现为距离偏大，因此“测量距离显著大于当前几何预测”会被更强地判为 NLOS 并降权。
6. **鲁棒 IRLS/WLS**：反复重估 residual/NLOS/weight，再求二维位置。
7. **Kalman 最后平滑**：先把 range/NLOS 处理好，再对最终 XY 做常速度 Kalman；不会用滤波掩盖明显坏距离。

## 第一次安装

在普通 PowerShell 或 CMD 中进入本目录：

```powershell
cd C:\\Users\\YOUR_USER\Desktop\esp32s3_7anchor_epoch_dashboard
python -m pip install -r requirements.txt
```

## 每次运行

1. A1/A2/A3/A4/A5/A6/A8 全部上电。
2. Target 接电脑，当前为 COM5。
3. 退出 `idf.py monitor`（`Ctrl + ]`），否则 COM5 会被占用。
4. 运行：

```powershell
python dashboard_7anchor_epoch.py --port COM5
```

也可以双击 `start_dashboard.cmd`。

## 先做软件自检

```powershell
python self_test.py
```

如果显示 `SELFTEST ... error=... m` 且退出码正常，说明求解器基本工作。

## 高度补偿

你目前只提供了 X/Y，没有提供这 7 块板的真实天线高度，所以配置中：

```json
"use_height_compensation": false
```

如果之后测得每块 Anchor 的 `z_m` 和移动 Target 天线高度 `target_z_m`，把它们填入 `anchors_7.json` 并把：

```json
"use_height_compensation": true
```

程序会将 FTM 的空间斜距转换成二维水平距离后再定位。这一步在 Anchor/Target 高差明显时会提高准确性。

## 输出文件

`results_7anchor_epoch/`：
- `raw_serial.log`：完整串口原始数据
- `positions.csv`：每个 Epoch 的 raw XY、Kalman XY、RMS、Anchor 数等
- `links.csv`：每个 Anchor 每轮的距离、权重、NLOS、残差、RSSI、CSI 等

这些文件可用于后续分析障碍物、重新调权重、训练模型。
