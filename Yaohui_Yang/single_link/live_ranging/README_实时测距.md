# 用拟合模型测量当前实际距离

本程序从 TAG 的串口读取此刻的 FTM/CSI，输出当前原始 FTM、仅 CIR 拟合和 FTM＋CIR 修正距离。
程序包不包含历史测量日志，不会自动回放旧数据，也不在测量时重新拟合模型。
附带的三个模型来自你此前 1、2、3、4、6、9、12、15 m 的 A/B 实验，模型文件保持冻结。

## 现场操作

1. 先测一条链路：给一块 Anchor 和 TAG 供电，确认两者使用匹配的 SSID。
   旧固件是 RTT_CIR_LAB；按编号烧录的 A1 必须对应 TAG 的 A1 配置，A2–A6 同理。
2. TAG 使用 UART/CP210x 接口，按你之前确认的 COM11。
3. 如果 idf.py monitor 还开着，按 Ctrl+] 退出；关闭其他占用 COM11 的采集器/串口窗口。
4. 解压本包，进入能看到 live_distance.py 的文件夹，在地址栏输入 powershell 并回车。
5. 执行：

```powershell
python -m pip install -r .\requirements.txt
python .\live_distance.py --port COM11 --seconds 120
```

TAG 和 Anchor 放到要测的位置，保持这一组内位置不变。
窗口读取当前串口数据，等待一个完整的 ROUND_START→FTM/CSI→ROUND_END 后显示预测。
窗口默认的大数字为 CIR only，即仅 CIR 特征拟合的距离。左侧可切换 FTM linear 或 FTM + CIR。
所有方法始终同时计算并记录；切换只改变主显示，不修改模型或已记录结果。

默认 120 秒后自动停止采集，窗口保留最终测量结果供你截图。
点击 Save current view (PNG) 导出当前窗口，或使用 Win+Shift+S 截图。
Stop capture; keep window 可提前停止。关闭窗口后会保存 final_view.png。

如果要让它一直测，直到你主动停止：

```powershell
python .\live_distance.py --port COM11 --seconds 0
```

如果要默认显示 FTM＋CIR：

```powershell
python .\live_distance.py --port COM11 --seconds 120 --model cir_residual
```

没有可用图形界面的 Python 会退回终端实时测量。终端会逐轮打印当前原始 FTM、CIR 和 FTM＋CIR 值，仍可截图。
也可以主动指定终端模式：

```powershell
python .\live_distance.py --port COM11 --seconds 120 --no-window
```

程序每次创建独立的 measurements/live_日期_时间_微秒/ 文件夹。
要测另一个固定距离，先停止这一组，重新运行上述命令开始新文件夹，再记录新的实际距离。

## 如何看窗口

- LIVE SERIAL：正常运行时数据来源是当前串口，界面不是旧实验的回放。
- CIR only：仅用 CIR 形状/相关频谱特征的模型输出。
- FTM + CIR：用 CIR 及 FTM 特征预测残差，再加回原始 FTM 的模型输出。
- FTM linear：仅对 FTM 距离做线性校准。
- BSSID：这次链路实际来源；新编号 TAG 还会显示 A1–A6。旧 TAG 无编号时显示 UNKNOWN，保留实际 BSSID。
- FINISHED：采集已停止，大数字是最后捕获的结果，不会继续随移动而更新。
- CIR_UNAVAILABLE：本轮没有足够有效同帧 CIR，只保留原始 FTM，拟合值留空。
- FTM_FAILED：本轮 FTM 失败，距离留空，不把零当作实际距离。
- OUT_OF_DOMAIN：测量特征超出训练分布的简单诊断，仍显示预测；它不是校准过的误差概率。
- INVALID_NEGATIVE_PREDICTION：模型给出了负值，原样保留并标记，不能作为有效距离使用。
- SOURCE_MISMATCH：CSI 来源与本轮 AP 标识不一致，拒绝输出拟合结果。
- STALE：超过 12 秒没有新结果，主数字清空；表格标题会说明它保留的是最后一轮历史值。

原始 TAG 大约每几秒完成一次单链路 FTM。电脑窗口刷新较快不代表无线测距也提高到同样频率。
窗口没有把真实距离作为模型输入，也没有把输出裁到标定距离或强制靠近真实值。

## 文件

| 文件 | 内容 |
|---|---|
| serial_raw.log | 此次从串口收到的原始日志，可用于事后核查 |
| live_predictions.csv | 每轮时间、轮号、来源、原始 FTM、三个模型预测及质量标签 |
| measurement.json | 串口、时长、开始结束、已完成会话数、错误与冻结模型哈希 |
| snapshot_*.png | 点击按钮保存的当前窗口 |
| final_view.png | 关闭窗口后保存的最终图，终端模式无此图 |

CSV 中 *_m 保留原始模型数值，包含负值或域外预测；*_usable_m 仅在质量为 EXPERIMENTAL 时填写。
这不表示 EXPERIMENTAL 状态已经通过了独立精度验证。

## 测量结束后核对误差

先用卷尺记录两天线参考点之间的真实距离。测量时不输入该值；结束后才运行下面的评估程序。
例如这一组确实静止在 2 m 时：

```powershell
$capture = Read-Host '粘贴刚才终端输出的 measurements/live_... 完整文件夹路径'
python .\evaluate_measurement.py --capture $capture --truth-m 2
```

会显示这一组各方法的 MAE、有效数值个数/已完成轮数，并保存 evaluation_2m.csv/json。
负数和域外预测仍计入误差；失败或缺失结果另记数量，没有伪造为零误差。
评估不会改动测量 CSV 或模型。

## 适用范围

沿用当前能输出 FTM、CSI、FTM_DETAIL、CSI_FTM_FRAME 的 TAG 固件；普通 FTM-only 固件不能得到 CIR 拟合值。
本工具是单链路测量。六 Anchor 自动轮询仍需另行改造 TAG。
旧模型针对原板对训练，原 AP BSSID 是 02:10:5b:f5:6c:ee；更换板子、天线、带宽或环境后应重新验证。
当前 B 组选参验证 MAE：仅 CIR 约 4.516 m，FTM＋CIR 约 4.009 m，不能保证分米级测距。

本环境只做真实串口日志的接口一致性验证、模拟串口及图形检查，没有连接你的 COM11 或 ESP32 硬件。
串口 API 参考：https://pyserial.readthedocs.io/en/latest/pyserial_api.html
