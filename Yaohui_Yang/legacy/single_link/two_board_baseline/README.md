# 双 ESP 早期实验归档

这里完整保存五 Anchor 版本之前的一发一收/双板实验。归档没有删除数据，可用于论文中的单链路测距基线，但不应与五 Anchor 新实验混在同一个数据集里。

```text
02_two_esp_archive/
├─ firmware/       # rtt_responder、rtt_csi_combined
├─ host_tools/     # 单 Anchor 串口记录和绘图工具
├─ analysis/       # 双板 RTT/CSI 离线分析脚本
├─ data/           # 1–18 m 原始测距数据及独立 session
├─ results/        # 旧模型、统计表和图表
└─ docs/           # 历史启动命令
```

运行旧分析脚本时，先进入本目录：

```powershell
cd C:\\Users\\YOUR_USER\Desktop\thesis\02_two_esp_archive
python analysis\scripts\process_rtt_csi_data_v4.py
```

分析脚本使用本归档目录内的 `data` 和 `results`，不会写入当前五 Anchor 工程。
