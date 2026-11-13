# 7 Anchor 权重算法 V2（当前室内/室外部署）

当前已知部署：
- 高信任室内 Anchor：A2、A6、A8
- 低信任室外隔墙 Anchor：A1、A3、A4、A5
- A7 禁用

## V2 与旧版的区别

旧版会先用所有 Anchor 得到位置，再用该位置的 residual 判断 NLOS。若位置本身被坏距离拉偏，可能把正常的 A6/A8 也误判成 NLOS，形成反馈环。

V2 改为：
1. 明确使用场景先验。A2/A6/A8 默认 prior NLOS=0.08；A1/A3/A4/A5 默认 prior NLOS=0.80。
2. 权重不再只靠当前 residual。先由 FTM entries、RSSI、CSI、历史 MAD、时间一致性产生基础权重。
3. 使用 Leave-One-Out (LOO) residual：判断 Ai 时，先把 Ai 从位置计算里拿掉，再看其它 Anchor 预测的 Ai 距离与 Ai 实测距离差多少。因此 Ai 不会“用自己的坏距离证明自己是好/坏”。
4. FTM 多径典型是距离偏大，所以只有 LOO residual 显示“实测距离明显过长”时，才强烈增加 NLOS 概率。
5. 已知室内 A2/A6/A8 对 residual 的 NLOS 证据更保守（loo_strength=0.55）；隔墙室外链路更激进（0.92）。
6. 最终 WLS 权重约为：

   quality / sigma^2 × (1 - P_NLOS)^2 × Huber(LOO residual)

   室外 Anchor 不是完全删除，仍以低权重帮助几何约束。
7. GUI 的“权重”现在归一化到 0~1，便于直接比较。

## 当前先验
- A2/A6/A8: prior NLOS 0.08
- A1/A3/A4/A5: prior NLOS 0.80

这些值都在 `anchors_7.json` 中，可直接修改。

## 运行
确保 Target COM5 没被 `idf.py monitor` 占用：

```powershell
cd C:\\Users\\YOUR_USER\Desktop\esp32s3_7anchor_weight_v2
python -m pip install -r requirements.txt
python dashboard_7anchor_epoch.py --port COM5
```

或双击 `start_dashboard.cmd`。
