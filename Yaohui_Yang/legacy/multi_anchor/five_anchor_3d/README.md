# 五 Anchor FTM + CSI 三维定位工程

这是当前论文主工程：五块固定 ESP32-S3 Anchor 和一块移动 Tag。Anchor 坐标预先测量，主机端完成实时三维定位和可视化。

## 分类

```text
01_five_anchor_3d/
├─ firmware/
│  ├─ ftm_csi_3d/          # 当前 Anchor 与 Tag 固件
│  └─ ftm_only_baseline/   # 多 Anchor FTM-only 对照基线
├─ host_tools/
│  ├─ ftm_csi_3d/          # 实时窗口、三维求解、快速校准
│  └─ ftm_only_baseline/   # FTM-only 主机端基线
├─ results/
│  └─ ftm_csi_3d_live/     # 五 Anchor 实测 session
├─ docs/                    # 烧录、部署与 AI 交接说明
├─ tests/                   # 主机算法回归测试
└─ development_artifacts/   # 仿真、界面和冒烟测试临时产物
```

## 常用操作

启动实时窗口：

```powershell
cd C:\\Users\\YOUR_USER\Desktop\thesis\01_five_anchor_3d\host_tools\ftm_csi_3d
.\start_live_dashboard.ps1 -Port COM7
```

运行回归测试：

```powershell
cd C:\\Users\\YOUR_USER\Desktop\thesis\01_five_anchor_3d
python -m unittest tests\test_fused_3d_mapper.py
python host_tools\ftm_csi_3d\fused_3d_mapper.py --simulate --rounds 40
```

烧录工程：

```text
firmware/ftm_csi_3d/anchor
firmware/ftm_csi_3d/tag
```

`results/ftm_csi_3d_live` 中每个 session 同时保存原始串口日志和与该次运行对应的派生结果，便于完整追溯。

移动前的 ESP-IDF 编译缓存已保留为 `build_before_stage_reorg`。当前 `ftm_csi_3d/anchor` 与 `tag` 均已在新路径 clean build 成功，后续可直接执行 `idf.py -p COMx flash monitor`。
