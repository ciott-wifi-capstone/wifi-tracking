# 现有 V5 的多距离采集与 CIR 拟合

若烧录时出现`esp_lcd_panel_rgb.c`的`internal compiler error: Segmentation fault`，
请先看 [修复编译报错.md](修复编译报错.md)。本包已将两个工程限制为主程序及必需依赖，跳过无关LCD驱动的编译。
此项更改是针对报错路径的绕过，尚未在你的Windows工具链上验证。

## 新一轮多距离实验，从这里开始

完整方法见 [多距离采集与拟合方法.md](多距离采集与拟合方法.md)。
沿用当前V5固件，电脑端新增采集向导、断点续采、B组选参、冻结模型和C组独立测试。

建议1、2、3、4、6、9m在A训练、B验证、C测试三种实际摆放位置各采120秒；
C额外采1.5、2.5、5m。复用旧1/2/3m训练组后还需18组，纯记录约36分钟。

```powershell
cd C:\\Users\\YOUR_USER\Desktop\QAR_FTM_S3_CIR_FIT
python -m pip install -r .\requirements.txt
python .\tools\cir_experiment.py wizard --port COM12 --reuse-existing --extra-test-distances 1.5 2.5 5
```

按提示量距离、摆设备，开始B/C时实际移动整套设备。输入`q`退出，重复命令继续。
新模型保存在`experiments/LOS_MULTI/fit/models`，完成后实时预测用：

```powershell
python .\tools\cir_distance.py live --port COM12 --models .\experiments\LOS_MULTI\fit\models
```

只在模型冻结后采最终测试C；看C的逐距离误差和可用比例，才能判断是否改善。
若更换了板卡、天线或采集配置，请去掉`--reuse-existing`，连A也重新采。
这一轮尚未实测，下面的模型和精度数字均来自此前三组训练与单组2m测试。

## 已有模型与处理方法

本工程在原有 V5 数据流上增加电脑端 CIR 处理、距离拟合和实时输出。
Anchor/Tag 继续运行你当前的固件。根目录`models/`附带此前训练的实验模型，供回放和复现。

处理链：

`FTM同帧CSI → 20MHz LLTF解析 → 缺失频点重建 → 细密CIR → 相对前沿与能量特征 → 距离拟合`

默认输出 `cir_direct`：根据 CIR 形状和 CSI 频率选择性特征直接拟合距离。
另外保留 `cir_residual`，用于探索 CIR 对 FTM 的误差修正。两者均为实验模型。

## 运行旧模型，复现此前实验

1. 将压缩包解压到桌面，得到 `QAR_FTM_S3_CIR_FIT` 文件夹。
2. 两块 ESP32-S3 保持供电，关闭占用 COM12 的 monitor 或串口窗口。
3. 在普通 Windows PowerShell 中执行：

```powershell
cd C:\\Users\\YOUR_USER\Desktop\QAR_FTM_S3_CIR_FIT
python -m pip install -r .\requirements.txt
python .\tools\cir_distance.py live --port COM12 --models .\models
```

如果 Tag 的串口已经变化，将 COM12 改为实际串口。无需输入真实距离。
`Ctrl+C` 停止，原始串口日志和预测 CSV 自动保存到新的 `live_日期_时间` 文件夹。

终端输出格式：

```text
ROUND=138  DISTANCE=2.048m [EXPERIMENTAL]  RAW_FTM=3.300m  pairs=21
```

上面这行来自已上传的独立2m记录回放，实际串口运行时会使用当前收到的CSI。
`DISTANCE` 是拟合结果，`RAW_FTM` 是原始距离；CSV 还保存 FTM线性校准与 CIR+FTM 修正结果。

## 已完成的处理

- 解析20MHz non-HT LLTF的128字节CSI：先虚部、后实部；映射为0..31、-32..-1子载波。
- 第一字无效时排除对应的前两个复数元素；检查带宽、PHY模式、数据长度和有效频点数量。
- 对未测量频点做平滑L1目标的迭代重建，始终保持已测量频点不变。
- 正确在正负频率之间补零，生成16倍采样的相对CIR；插值不会增加实际测量带宽。
- 以主峰为相对零点，提取主峰附近能量、早期能量、尾部能量、相对RMS时延、熵、前沿偏移。
- 使用有限带宽理想脉冲的前沿作为特征参考；未把未标定的CIR格点直接换算为绝对距离。
- 将同一FTM会话的有效配对帧聚合为一条预测，每条记录至少需要4个有效同帧CIR。
- 使用正则化线性回归拟合距离，模型参数保存为可读JSON；运行只需numpy和pyserial。

## 本次真实验证结果

训练使用 `V5_LOS_1m_A_R1`、`V5_LOS_2m_A_R1`、`V5_LOS_3m_A_R1`，合计175个会话。
另将更早的 `V5_TIMESTAMP_01` 独立2m采集（15个会话）留出，不参与训练或参数选择。

| 评估方式 | 原始FTM MAE | 只用FTM线性校准 | CIR直接拟合 | CIR+FTM修正 |
|---|---:|---:|---:|---:|
| 训练回代，175会话 | 4.536m | 0.121m | 0.091m | 0.110m |
| 整组留出，175会话 | 4.536m | 0.226m | 0.780m | 0.682m |
| 独立2m采集，15会话 | 1.600m | 1.349m | 0.081m | 1.589m |

训练回代不代表实际精度。整组留出时，CIR尚未优于简单FTM线性校准。
独立2m组的结果较好，但本次训练标签均值恰好是2m：恒定输出2m也能在这一组得到0误差。
因此0.081m不能证明系统已经具有通用分米级测距能力。
目前三组训练的距离与摆放条件混在一起，CIR可能也学到场景特征。

更详细的 MAE、RMSE、中位数、P90、域外数量和数据审核见 `results/fit_report.md`。
评估没有删除域外样本，也没有把预测值限制在1—3m来美化误差。

## 输出状态

- `EXPERIMENTAL`：产生了拟合结果，模型仍处于小数据实验阶段。
- `OUT_OF_DOMAIN`：输入特征明显偏离训练分布；仍记录原始拟合值，但 `distance_for_use_m` 留空。
- `CIR_UNAVAILABLE`：缺少足够的可用CIR；保留原始FTM，拟合距离留空。
- `INVALID_NEGATIVE_PREDICTION`：回归发生不合理外推；不提供可用距离。

这些状态不是经过标定的置信度，不能解释为某个百分比的测距准确率。

## 使用已有记录回放

```powershell
python .\tools\cir_distance.py replay --input .\example_data\V5_TIMESTAMP_01.zip --models .\models --out .\replay_external
```

也可以读取旧采集文件夹：

```powershell
python .\tools\cir_distance.py replay --input .\recordings\你的试验文件夹 --models .\models --out .\replay_my_capture
```

对新的实时日志进行无标签回放：

```powershell
python .\tools\cir_distance.py replay --log .\live_实际时间\serial_raw.log --models .\models --out .\replay_live
```

## 重新拟合

附带四组原始ZIP，可以复现本次处理和验证。执行：

```powershell
python .\tools\cir_distance.py train --train .\example_data\V5_LOS_1m_A_R1.zip .\example_data\V5_LOS_2m_A_R1.zip .\example_data\V5_LOS_3m_A_R1.zip --holdout .\example_data\V5_TIMESTAMP_01.zip --out .\results
```

`--train` 只放训练采集，`--holdout` 放独立测试采集。不要将同一采集同时用于两者。
训练需要至少两个不同距离。至少三个采集时，程序增加整组留出评估。
真实距离只用于训练和误差评估，不会传给实时预测模型。

## 验证范围

已验证频域顺序、IQ解析、无效字处理、已测频点保持、训练/测试隔离、标签不参与预测、
原始串口日志与离线CSV特征一致，以及缺失CIR时的行为。
可执行：

```powershell
python -m unittest discover -s tools -p 'test_cir*.py' -v
```

未连接实体板卡，未在你的Windows电脑上实际运行COM12。
Anchor/Tag的C源码、lab_config.h和sdkconfig.defaults与原V5逐字节一致。
顶层CMake与烧录脚本已更新，编译报错处理见修复说明；电脑端多距离拟合本身不要求更换固件。

## 文件

- `tools/cir_core.py`：CIR处理和模型计算。
- `tools/cir_io.py`：旧数据读取与实时数据适配。
- `tools/cir_distance.py`：训练、回放、串口预测入口。
- `tools/cir_experiment.py`：多距离采集向导、验证选参、模型冻结、最终测试与导出。
- `run_multidistance_COM12.ps1`：安装依赖后可运行的采集向导快捷入口。
- `多距离采集与拟合方法.md`：实验安排、操作步骤、算法和结果判断方法。
- `models/cir_direct.json`：默认的CIR直接距离模型。
- `models/cir_residual.json`：CIR+FTM修正模型。
- `models/ftm_affine.json`：简单FTM校准基线。
- `results/`：本次真实数据的特征、预测、误差与审核报告。
- `example_data/`：三组训练ZIP及一组独立测试ZIP。
- `anchor/`、`tag/`：保留的现有V5固件源码。
