# CSI 动态可靠度加权 V1

这是一个初步版本，核心原则是：**FTM 给距离，CSI 给这一次距离的可信度**。

- 不把 A1/A2/... 永久写成 LOS/NLOS。
- 不使用“室内/室外”固定先验。
- 每一个 Round、每一块 Anchor 都重新根据 CSI 计算 `csi_reliability ∈ [0,1]`。
- CSI可靠度主要由：CSI包数、CSI RSSI稳定性、CSI功率稳定性、roughness、RSSI强度（弱影响）组成。
- `csi_reliability` 再映射成 `csi_sigma_m`，并进入 WLS 权重。
- 几何 residual 只用 Huber 作为最后一道异常值保护，不作为主要可靠度判断器。

核心公式近似为：

`weight = q_entries * q_time * reliability^2 / (sigma_floor^2 + sigma_recent^2 + sigma_csi^2)`

其中 `sigma_csi` 随 CSI reliability 下降而增大。

## 运行

先关闭 `idf.py monitor`，保证 COM5 没被占用：

```powershell
cd C:\\Users\\YOUR_USER\Desktop\esp32s3_7anchor_csi_weight_v1
python -m pip install -r requirements.txt
python dashboard_7anchor_epoch.py --port COM5
```

也可以双击 `start_dashboard.cmd`。

## 注意

这是 V1 启发式版本。当前 Target 固件只上传 CSI 的统计特征，不是完整子载波 CSI/CIR，因此它只能做“初步动态可靠度估计”。后续采集带 Ground Truth 的实验数据后，应重新标定阈值，或者训练 `CSI features -> range uncertainty sigma` 模型。
