# Python 单链路 CIR 测距验证程序

这是处理已采集数据的离线程序。窗口中的测距曲线和逐会话结果来自原始录制数据；不是当前串口实时测距。
包内自带你上传的 recordings(4).zip 的完整原始数据副本，SHA256 为：
`938fb8b1106d4749aef209ac71300b284bd86e08c321b5b3ed2d0aa3b48f913e`。

## 最快运行

解压压缩包，进入能看到 run_distance_validation.py 和 requirements.txt 的文件夹。
在资源管理器地址栏输入 powershell 并回车，执行：

```powershell
python -m pip install -r .\requirements.txt
python .\run_distance_validation.py
```

程序会逐组打印 LOAD，重新从原始 IQ/FTM 提取 CIR 特征，然后使用 A 组拟合、B 组选择正则化参数。
处理完成后，终端显示全距离误差和部分会话的真实距离、预测距离、绝对误差，打开一个可交互的结果窗口。
运行需要 Python、NumPy、Matplotlib；Windows 标准 Python 的 Tkinter 用于弹出窗口。
如果 ESP-IDF 自带的 Python 没有可用的 Tkinter，程序会提示并改为保存 CSV 和 PNG。
要打开交互窗口，可以用普通 Windows Python 运行；也可以主动加 --no-window 只保存结果。

## 结果窗口与截图

- 左上 Data group：A 是训练回代，B 是用于选参的验证。默认 B。
- View true distance：默认 All，可切到 1、2、3、4、6、9、12、15 m 查看该距离。这个选择只改变显示，不重新训练。
- Displayed model：FTM linear 是 FTM 线性校准，CIR only 是仅 CIR 特征拟合，FTM + CIR 是残差修正。默认 FTM + CIR。
- 底部 Session 滑块、Previous/Next：查看每一次历史测距。
- 左上曲线：当前距离筛选下的原始 FTM、所选模型结果和真实距离。
- 右上散点：当前数据组全部距离的预测与真实距离比较，红圈为当前会话。
- 左下表：全部距离的 A 训练 MAE、B 验证 MAE、B 的 P90。这个全局表不会因距离筛选而变成局部精度。
- 右下表：当前一次测距中所有方法的距离和绝对误差。
- Save current view：导出当前窗口为 PNG，同时在终端打印文件位置。也可以使用 Win+Shift+S 自行截图。

窗口标题、分组、筛选范围和误差表都会保留在导出的图片中。每次运行自动保存 overview.png。

## 命令示例

只看 B 组 2 m、仅 CIR 的结果，终端打印前 15 个会话：

```powershell
python .\run_distance_validation.py --split B --distance 2 --model cir_direct --rows 15
```

只看 B 组 2 m、FTM 加 CIR 修正：

```powershell
python .\run_distance_validation.py --split B --distance 2 --model cir_residual --rows 15
```

只保存 CSV 和图片，不弹窗口：

```powershell
python .\run_distance_validation.py --no-window
```

使用电脑上的原始采集文件夹：

```powershell
python .\run_distance_validation.py --input "C:\\Users\\YOUR_USER\Desktop\QAR_FTM_S3_CIR_FIT\recordings"
```

也可以把 --input 后的路径改为自己的原始 ZIP 路径。
目录/ZIP 内每次采集必须包含 metadata.json、ftm.csv、ftm_detail.csv、csi.csv、csi_ftm_frame.csv。
这套拟合流程读取 condition=LOS，且 trial 名称以 _A_Rn 或 _B_Rn 结尾的数据，例如 LOS_2m_A_R1。
其他名称/条件会在终端打印 SKIP，并记录到 data_audit.json，不会自动把新 Anchor 的采集误当作 A/B 数据。
没有至少两个不同距离的 A 组和至少一组 B 数据时会报错，不会编造拟合结果。

## 输出文件

每次运行创建 runs/run_日期_时间_微秒/ 文件夹，不覆盖原始数据：

| 文件 | 内容 |
|---|---|
| overview.png | 本次运行的初始结果窗口 |
| view_*.png | 点击 Save current view 导出的窗口 |
| session_features.csv | 从原始 FTM/CSI 重新提取的会话特征 |
| session_predictions.csv | 所有会话、所有方法的预测距离、真实距离、误差和质量标签 |
| metrics.csv | 所有距离的 A/B 分组误差 |
| metrics_by_distance.csv | 逐距离误差 |
| candidate_scores.csv | B 组选参候选及分数 |
| data_audit.json | 每组可用/不可用会话及原因、未匹配采集记录 |
| run_info.json | 数据来源、模型哈希、训练/验证范围 |
| models/*.json | 本次从 A 组重新拟合、按 B 组选参的三个模型 |

绝对误差为 abs(预测距离 - 真实距离)，MAE 是逐会话绝对误差的平均。
程序没有把预测裁到 1–15 m，没有把负预测裁成零，也没有按 OUT_OF_DOMAIN 标签删除会话。
质量标签是诊断信息，不能当成已校准的失败概率。

## 此次数据应得到的数值

| 方法 | A 训练 MAE (m) | B 选参验证 MAE (m) |
|---|---:|---:|
| Raw FTM | 5.142 | 9.777 |
| FTM linear | 0.799 | 4.630 |
| CIR only | 1.985 | 4.516 |
| FTM + CIR | 0.468 | 4.009 |
| Constant baseline | 4.123 | 4.134 |

共 16 组：A 组 318 个可用会话、B 组 320 个，总共 638 个。
每组采集的训练权重相同，特征标准化只使用 A 组。B 组用于从 1/10/100 中选择正则化强度，
因此 B 是模型选择验证，不是独立最终测试。对距离的显示筛选不会改变训练集或这些总指标。
A 组 0.468 m 不能作为实际泛化精度。仅 CIR 的 B 组结果尚未超过固定均值基线。

## 算法范围

同帧 FTM/CSI → 非 HT、20 MHz LLTF 的 IQ 解码 → 未观测频点平滑 L1 重建 → 补零 IFFT →
相对 CIR 形状与频率选择性特征 → 回归。CIR 时间参考没有绝对传播时延标定，程序不把峰值位置直接换算成真实距离。
CIR only 使用 8 个形状/频谱特征。FTM + CIR 额外使用 4 个 FTM 相关特征，学习距离残差并加回原始 FTM。
真实距离只用于训练标签、B 组选参、误差评估和界面显示，不作为推理特征。
这是单链路程序，A/B 采集组不是六块固定板的 A1–A6 编号。
