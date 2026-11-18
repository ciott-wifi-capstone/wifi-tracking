# ESP32-S3 可扩展多 Anchor FTM/CSI 三维定位

本工程从当前五 Anchor 三维代码独立建立，支持配置 4–12 个 Anchor。它保留 FTM/CSI 采集、鲁棒加权三维解算、OpenGL 实时界面和原始日志，但不继承旧六点线性拟合方案或参数。

## 默认状态

- 当前配置先列出已有 A1–A5 的实测坐标，便于直接启动验证。
- 所有 Anchor 使用 `scale=1.0`、`bias_m=0.0`，即主机端不做旧线性拟合。
- 新的分段单调测距修正默认处于 `shadow`（影子）模式：会计算、记录，但不会改变定位结果。
- 当前定位模式为 `2d_fixed_z`：只解算 X、Y，Tag高度由 `anchors_3d.json` 中的 `positioning.fixed_z_m` 固定给出。
- 增加 A6–A12 时，在 `host_tools/multi_anchor_3d/anchors_3d.json` 的 `anchors` 中追加坐标。
- 新场地必须更新 `room_bounds_m`，并确认坐标单位为米。

## 目录

- `firmware/multi_anchor_3d/anchor`：静态 Anchor 固件，ID 可设为 1–12。
- `firmware/multi_anchor_3d/tag`：移动 Tag 固件，最大 Anchor 数可设为 4–12。
- `host_tools/multi_anchor_3d`：实时解算、可视化和启动脚本。
- `results/multi_anchor_3d_live`：新工程的输出，不与旧五 Anchor 结果混合。
- `tests`：主机端解析、解算和多 Anchor 几何测试。

## 启动当前 A1–A5

```powershell
cd C:\\Users\\YOUR_USER\Desktop\thesis\04_multi_anchor_3d\host_tools\multi_anchor_3d
.\start_live_dashboard.ps1 -Port COM7
```

或在 CMD 中运行：

```bat
cd /d C:\\Users\\YOUR_USER\Desktop\thesis\04_multi_anchor_3d\host_tools\multi_anchor_3d
python fast_dashboard.py --port COM7
```

## 增加 Anchor

1. 在静态固件 `menuconfig` 中为新板设置唯一 `ANCHOR_ID`。
2. 在移动固件中把 `FTM_MAX_ANCHORS` 设置为实际最大ID。
3. 所有板保持相同Wi-Fi信道和带宽。
4. 在 `anchors_3d.json` 中添加新Anchor的实测坐标，初始保持 `scale=1.0`、`bias_m=0.0`。
5. 重新烧录移动Tag；已有静态A1–A5无需重烧。

## 数据边界

旧工程的标定结果没有复制到这里。旧的P01–P06只能作为五Anchor历史训练/基线数据；本工程生成的新数据进入独立结果目录，不应直接与旧实验混合统计。

固定高度必须使用与Anchor相同的A3基准坐标系。例如Tag天线中心比A3基准高1.20米，就设置 `"fixed_z_m": 1.20`。该值属于主机解算配置，修改后重启界面即可，不需要重新烧录ESP32。

## 采集带真实坐标的数据

每个固定测试点单独启动一次。下面表示移动板真实坐标为 `(1.2, -0.5, 0.8)m`：

```bat
python fast_dashboard.py --port COM7 --point-id P01 --truth 1.2 -0.5 0.8 --condition LOS --repeat 1
```

输出的 `link_measurements.csv` 同时包含原始 RTT 换算距离、ESP 端估计/滤波距离、模型候选距离、实际参与解算的距离和相对真实坐标计算出的逐 Anchor 误差。`session_metadata.json` 保存本次坐标、环境标签和完整配置快照。

建议每个 Anchor 覆盖至少 8–12 个不同真值距离，每个点静止采集 300–500 条，并划分独立的训练点与验证点。采集完成后训练：

```bat
python fit_range_correction.py
```

脚本会更新 `range_correction.json`，但不会自动让模型介入定位。先在 `shadow` 模式比较 `baseline_distance_m` 与 `model_corrected_m`；验证集确实改善后，再把 `anchors_3d.json` 中 `range_correction.mode` 从 `shadow` 改为 `apply`。恢复原始基线则改为 `off`。
