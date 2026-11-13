# 六块 ESP32-S3：FTM + CSI 三维定位原型操作手册

## 1. 这版代码实际能做什么

六块板分为 **5 个 Anchor + 1 个移动 Tag**。五个 Anchor 同时提供：

1. IEEE 802.11 FTM responder，用往返时间估计距离；
2. 带 Anchor ID 的广播探测帧，让 Tag 采集该链路的 CSI；
3. Tag 在每轮输出 FTM 距离、CSI 强度/波动/频域粗糙度等融合特征；
4. 电脑执行鲁棒三维多边定位，降低疑似 NLOS 链路权重；
5. 沿 Anchor—Tag 射线累计“无线遮挡概率”，在线生成粗粒度体素图。

这里的环境模型是 **radio anomaly / blockage likelihood map（无线异常或遮挡概率图）**，不是摄像头或激光雷达意义上的墙体几何重建。仅凭单天线 ESP32-S3 的 FTM/CSI，不能保证在“任意障碍、任意环境”中还原障碍物形状，也不能诚实承诺厘米级三维定位。

## 2. 六块板的编号和摆放

| 实物标签 | 固件 | 建议位置 | 作用 |
|---|---|---|---|
| A1 | Anchor，ID=1 | 房间左前、低位约 0.4 m | FTM + CSI 探测 |
| A2 | Anchor，ID=2 | 右前、高位约 2.4 m | FTM + CSI 探测 |
| A3 | Anchor，ID=3 | 右后、低位约 0.4 m | FTM + CSI 探测 |
| A4 | Anchor，ID=4 | 左后、高位约 2.4 m | FTM + CSI 探测 |
| A5 | Anchor，ID=5 | 中部或边界、高位约 2.8–2.9 m | 改善高度可观性 |
| TAG | Tag | 目标随身携带 | 采 FTM/CSI，USB 串口送电脑 |

五个 Anchor 不可以都放在同一高度或同一平面。坐标原点、x/y 方向可以自定，但必须用卷尺测量每块板的天线中心坐标，并填入 `host_tools/ftm_csi_3d/anchors_3d.json`。示例坐标只用于 6 m × 5 m × 3 m 演示，不能直接当成真实坐标。

固定每块板的朝向。ESP 天线方向变化会明显改变 CSI；论文实验中应记录朝向、安装高度和供电方式。

## 3. 软件环境

- 芯片：ESP32-S3；
- 已验证 SDK：ESP-IDF 6.0；
- Windows PowerShell；
- Python 3.10+；
- Python 包：NumPy、pyserial。

安装主机依赖：

```powershell
cd C:\\Users\\YOUR_USER\Desktop\thesis\01_five_anchor_3d\host_tools\ftm_csi_3d
python -m pip install -r requirements.txt
```

## 4. 给五块 Anchor 烧录

Anchor 工程：`firmware/ftm_csi_3d/anchor`

每次只插入一块板，依次执行：

```powershell
cd C:\\Users\\YOUR_USER\Desktop\thesis\01_five_anchor_3d\firmware\ftm_csi_3d\anchor
C:\esp\esp-idf-v6.0\export.ps1
idf.py menuconfig
```

在 `FTM + CSI 3-D anchor` 中设置：

- `Anchor ID`：这块板对应的 1、2、3、4 或 5；
- `Common Wi-Fi channel`：所有板统一为 6；
- `Use HT40`：第一轮保持关闭，即 HT20；
- `Responder hardware offset`：未校准前为 0；
- `CSI probe interval`：80 ms。

保存后烧录，其中 `COMx` 换成实际端口：

```powershell
idf.py -p COMx build flash monitor
```

看到以下形式即启动成功：

```text
AREADY,id=1,ssid=FTM_A1,channel=6,bw=20,...
```

烧录后立刻在板和 USB 线上贴 `A1` 等标签。绝不能让两块板使用相同 ID。重复以上过程直到 A1–A5 完成。

## 5. 给移动 Tag 烧录

Tag 工程：`firmware/ftm_csi_3d/tag`

```powershell
cd C:\\Users\\YOUR_USER\Desktop\thesis\01_five_anchor_3d\firmware\ftm_csi_3d\tag
C:\esp\esp-idf-v6.0\export.ps1
idf.py menuconfig
```

确认 `FTM + CSI 3-D tag` 中 Anchor 数为 5、信道为 6、HT40 关闭、FTM frames 为 16。然后：

```powershell
idf.py -p COMx build flash monitor
```

正确启动时应先看到 `TREADY` 和 `MSCAN`，之后每个成功 Anchor 都产生一行 `MFUSED`，每轮最后有 `MROUND`。若 `MFUSED` 中 `csi_packets` 长期为 0，优先检查所有板是否同信道、Anchor 是否出现 `APROBE` 发送失败、Tag 与 Anchor 是否为本项目的新固件。

## 6. 配置真实房间并运行

编辑 `host_tools/ftm_csi_3d/anchors_3d.json`，必须修改 Tag 串口、房间边界以及 A1–A5 的天线中心实测坐标。初次运行时 `scale=1.0, bias_m=0.0`。

先执行无硬件自检：

```powershell
cd C:\\Users\\YOUR_USER\Desktop\thesis\01_five_anchor_3d
python host_tools\ftm_csi_3d\fused_3d_mapper.py --simulate --rounds 40
```

再接入 Tag：

```powershell
python host_tools\ftm_csi_3d\fused_3d_mapper.py `
  --config host_tools\ftm_csi_3d\anchors_3d.json `
  --port COM7
```

输出位于 `results/ftm_csi_3d_live/`：

- `positions.csv`：每轮三维位置、残差和几何分数；
- `link_measurements.csv`：距离、CSI、NLOS 概率、残差和权重；
- `latest_state.json`：供可视化或其他程序实时读取；
- `radio_voxels.csv`：粗粒度无线遮挡概率体素。

## 7. 换新环境时的快速修正

1. 在新场地安装 A1–A5，保持非共面布局；
2. 重新测量坐标和房间边界并修改 JSON；
3. 把 Tag 放到一个准确已知、离墙不太近的位置，静止 25 秒；
4. 执行单点快速偏置校准：

```powershell
cd C:\\Users\\YOUR_USER\Desktop\thesis\01_five_anchor_3d\host_tools\ftm_csi_3d
python quick_calibrate.py --config anchors_3d.json --port COM7 `
  --position 2.00 1.50 1.10 --seconds 25
```

脚本会为每个 Anchor 用中位数更新 `bias_m`，并自动备份原 JSON。单点校准只能修正常量偏置；若要同时估计 scale 和 bias，至少在空间中设置 3–5 个已知点，并用训练/验证分组拟合。

随后启动定位程序，在场地内沿不同路线走 30–60 秒。程序的 CSI 鲁棒基线和体素图会在线累积。换场地后必须新建输出目录，不能把旧地图说成新环境自适应结果。

## 8. `MFUSED` 串口协议

```text
MFUSED,schema,round,esp_us,anchor,status,rtt_raw_ns,dist_est_cm,
corrected_cm,filtered_cm,ftm_rssi,entries,csi_packets,csi_rssi_mean,
csi_rssi_std,csi_power_mean,csi_power_std,csi_roughness,
csi_first_invalid,csi_age_ms,ok_count,fail_count,bssid,scan_rssi
```

当前 schema 为 1，共 24 列。修改固件字段时必须增加 schema 版本，并同步修改 `parse_mfused()` 与测试。

## 9. 论文实验应该怎样做

至少设置这些可比较组：

1. FTM 原始距离 + 普通最小二乘；
2. FTM 中值滤波 + 鲁棒三维定位；
3. FTM + CSI 启发式 NLOS 降权（本版）；
4. FTM + CSI 监督模型降权/修正（收集标签后的增强版）。

测试环境至少包含：空旷 LOS、人体遮挡、木门/墙角、金属柜、多路径走廊、完全换房间。指标使用三维误差的 median、MAE、RMSE、P90、P95，另报告更新率、丢失率、每链路 FTM 成功率。训练点、调参点和测试点必须按“场地/采集组”严格分开，避免数据泄漏。

## 10. 当前已知边界

- 已完成的是可编译的工程原型，不等于已经在你的六块实物板上验证；
- NLOS 概率当前是保守启发式，不是已经训练好的机器学习模型；
- CSI 探测帧是 MAC 与 payload 双重归属，但公共原始发包接口不能保证固定 MCS；
- 两条以上链路严重遮挡时高度信息会不足，算法会加强短时高度先验以避免跳到地面或天花板；
- 体素射线只能表达“这条传播路径异常”，不能确定障碍究竟位于射线上的哪一点；
- 真正提高论文分数，关键是严格实验、消融、跨场地测试和如实陈述限制。
