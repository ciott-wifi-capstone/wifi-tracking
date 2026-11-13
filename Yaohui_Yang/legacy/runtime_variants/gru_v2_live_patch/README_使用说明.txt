GRU V2 实时定位补丁
====================

用途
----
把已经训练完成的 temporal_fusion_v2.pt 接入实时 Dashboard：
FTM + CSI 最近20帧 -> GRU+Attention -> 修正每个Anchor距离
-> CSI/模型不确定度加权 Robust WLS -> 恒速度 Kalman -> 黄色实时位置和轨迹。

请覆盖到目录：
C:\\Users\\YOUR_USER\Desktop\04_multi_anchor_3d\host_tools\multi_anchor_3d

本ZIP包含：
- fused_3d_mapper.py        已加入 GRU V2 在线推理 + sigma加权 + Kalman
- fast_dashboard.py         黄色点=Kalman最终位置，灰叉=原始WLS；状态栏显示GRU ready
- anchors_3d.json           已启用 temporal_fusion_v2.pt 和 tracking；COM3；fixed_z=1.0
- temporal_fusion_model.py  训练时使用的 GRU+Attention 模型定义
- requirements.txt          加入 torch>=2.0

重要：
本ZIP故意不包含 temporal_fusion_v2.pt，避免覆盖你刚训练好的最佳模型。
请确认你电脑当前目录里已经存在：
temporal_fusion_v2.pt

建议先备份原文件：
copy .\fused_3d_mapper.py .\fused_3d_mapper_before_gru_v2.py
copy .\fast_dashboard.py .\fast_dashboard_before_gru_v2.py
copy .\anchors_3d.json .\anchors_3d_before_gru_v2.json

覆盖完成后，在 ESP-IDF PowerShell：
cd "C:\\Users\\YOUR_USER\Desktop\04_multi_anchor_3d\host_tools\multi_anchor_3d"
python -m py_compile .\fused_3d_mapper.py
python -m py_compile .\fast_dashboard.py
python -c "import torch; print(torch.__version__)"
python .\fast_dashboard.py --port COM3

运行时：
- 每个Anchor需要累计20个连续样本才会进入GRU修正。
- 状态栏会从 GRU 0/6 逐步变成 GRU 6/6。
- 在GRU尚未ready时，系统使用原始FTM baseline。
- GRU ready 后，右侧距离曲线显示实际用于定位的GRU修正距离。
- 黄色点：Kalman跟踪后的最终实时位置。
- 黄色线：最终运动轨迹。
- 灰色叉：GRU修正距离经过Robust WLS得到、尚未Kalman平滑的瞬时位置。

当前配置：
- 2D fixed Z = 1.0 m
- 旧 range_correction = off
- GRU model = temporal_fusion_v2.pt
- Kalman tracking = on

如果实际Tag天线高度不是1.0m，请只修改 anchors_3d.json：
"fixed_z_m": 你的实际高度

已完成检查：
- Python语法检查通过
- 原工程核心 fused/range 单元测试：7 passed
