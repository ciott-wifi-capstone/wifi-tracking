# 五 Anchor 实时可视化启动说明

## 启动前

1. 给 A1-A5 供电，不要按 BOOT。
2. 用 USB 将移动板连接电脑。
3. 关闭 ESP-IDF Monitor、串口助手等会占用 COM7 的程序。
4. 确认移动板串口为 COM7。如果 Windows 更换了端口，请使用下面的命令行方式指定新端口。

## 最简单的启动方式

双击同目录中的 `start_live_dashboard.cmd`。

窗口会显示：

- A1-A5 实时 FTM 距离；
- A1-A5 RSSI；
- A1-A5 每次测量窗口收到的 CSI 包数；
- 以最低 Z Anchor 为原点的实时三维位置和移动轨迹。

默认启动的是 Qt/OpenGL 硬件加速界面。主三维场景和右侧曲线使用独立的持久化绘图对象，拖动时不会重绘整张窗口：左键旋转、中键平移、滚轮缩放，点击“重置视角”可重新按 5 个 Anchor 自动取景。若电脑没有安装 Qt/OpenGL 依赖，启动脚本会自动退回旧 Matplotlib 界面。

关闭图形窗口即可安全结束并保存数据。

## PowerShell 启动方式

```powershell
cd C:\\Users\\YOUR_USER\Desktop\thesis\01_five_anchor_3d\host_tools\ftm_csi_3d
.\start_live_dashboard.ps1 -Port COM7
```

如果端口变为 COM10，例如：

```powershell
.\start_live_dashboard.ps1 -Port COM10
```

也可以直接运行：

```powershell
python .\fast_dashboard.py --port COM7
```

## 自动保存位置

每次启动都会新建一个带时间戳的目录：

```text
C:\\Users\\YOUR_USER\Desktop\thesis\01_five_anchor_3d\results\ftm_csi_3d_live\session-YYYYMMDD-HHMMSS
```

其中包含：

- `raw_serial.log`：完整原始串口数据；
- `link_measurements.csv`：每块 Anchor 的距离、RSSI、CSI 和权重；
- `positions.csv`：每轮计算的位置；
- `latest_state.json`：最新状态；
- `radio_voxels.csv`：无线遮挡概率体素。

## 当前限制

`anchors_3d.json` 已录入以 A3 为基准的五个 Anchor 实测坐标，并设置 `coordinates_verified=true`。RTT、CSI、RSSI 和三维解算均使用真实实时数据；但当前每条 FTM 链路的 `scale`/`bias_m` 仍是默认值，正式精度评价前仍需用卷尺真值完成逐链路距离校准。

显示层会自动选择 Z 坐标最低的 Anchor 作为 `(0, 0, 0)`，并对 Anchor 和移动板轨迹减去相同偏移。这个变换只影响画面，不改变定位求解器使用的原始标定坐标。初始视野只根据 5 个 Anchor 的包围范围和留白计算，因此不会随移动板轨迹跳动；点击“重置视角”会再次回到这个稳定范围。

## 界面数据说明

- **实时 3D 定位轨迹**：三角形是 A1-A5，带“基准”标记的最低 Z Anchor 显示为 `(0,0,0)`；黑点是当前移动板估计位置，灰线是历史轨迹。X、Y、Z 范围自动适配。
- **FTM 距离**：移动板与每个锚点的往返时间测距结果，单位为米。固件已经使用 5 点中值滤波，但尚未完成偏置校准，因此不能直接当作真实卷尺距离。
- **FTM RSSI**：测距帧的接收信号强度，单位 dBm。数值越接近 0，信号越强。约 `-30 dBm` 很强，`-50 dBm` 良好，`-70 dBm` 较弱，低于 `-80 dBm` 通常容易失败。
- **FTM 状态**：`成功` 表示本轮收到有效 RTT 报告；`失败` 表示本轮没有得到有效距离；`等待` 表示最近 4 秒没有新记录。
- **CSI 包/次**：对应 Anchor 在本次 FTM 测量窗口中收到的 HT20 MCS0 CSI 探测包数量。
- **累计成功/失败**：该锚点从移动板本次启动以来的成功与失败次数，例如 `157/1` 表示成功 157 次、失败 1 次。
- **测量轮次**：移动板完成一次 A1-A5 顺序测距后增加 1。
- **有效锚点**：最近 4 秒内状态成功的锚点数量。三维定位至少需要 4 个非共面锚点。
- **融合记录**：实时窗口从串口解析到的 `MFUSED` 总行数，不等于定位轮次数。
