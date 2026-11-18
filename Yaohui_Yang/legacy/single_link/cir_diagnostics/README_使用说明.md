# V5 FTM–CSI 配对与 CIR 诊断：离线版

本包依据实际上传的 `V5_TIMESTAMP_01.zip` 生成。**不会替换 Tag/Anchor 固件，不会修改 ESP-IDF，也不会根据已知 2m 真值硬编距离修正值。**

## 你可以直接看

- `analysis/分析结论.md`：具体指标和技术边界
- `analysis/raw_ftm_distance.png`：15 轮 FTM 与实际 2m 的差异
- `analysis/relative_cir.png`：440 条匹配 CSI 的相对 CIR 能量曲线
- `analysis/lltf_subcarrier_map.png`：有效子载波 52 个、空置 12 个的频域分布
- `analysis/matched_csi_cir_features.csv`：逐帧电磁特征和配对状态（**没有伪造修正距离**）
- `analysis/round_level_summary.csv`：每轮的统计结果

## 在 Windows 复用分析工具

1. 安装依赖：

   ```powershell
   python -m pip install numpy pandas matplotlib
   ```

2. 将 `V5_TIMESTAMP_01.zip` 放在脚本旁边（或指定 ZIP 的绝对路径），执行：

   ```powershell
   python .\analyze_v5_cir.py .\V5_TIMESTAMP_01.zip --out .\analysis
   ```

3. 代码会检查 `round_id + dialog_token` 唯一性，并将 `CSI` 按 ESP32-S3 的 **虚部在前、实部在后** 解码。20MHz LLTF 的 64 个频点中，索引 1..26 和 38..63 为 52 个有效子载波。`IFFT` 得到 CIR，但峰的零延迟基准尚未校准。

## 物理解释和后续限制

FTM 提供收发时间差，但接收机的包检测时刻会受射频延迟及多径叠加影响。CSI 描述频率响应；其 IFFT 的时域峰是**相对硬件 FFT/包检测时刻**，不等同于精确的自由空间传播时间。20MHz 的时延取样格约 50ns，对应单程约 15m；FFT 零填充提供插值、不增加独立带宽。

这次 440 条配对 CSI 在 64 点 IFFT 上主峰都处于相对 bin0，**不能从中稳定分辨出相隔几米的多径峰**。此外，新版同帧数据只有这一组 2m 摆放条件，无法把未知固定设备延迟和环境相关偏差唯一分开；强行让输出变成 2m 会构成训练集泄漏。

如果后续对现有不同距离和不同环境数据进行泛化评估，请注意较早的 CSI 是 FTM 前 UDP CSI，**不能当成本版同帧 CSI** 直接混合用于绝对 ToF 校准。
