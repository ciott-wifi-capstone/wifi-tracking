# 编译报错绕过修复

Anchor/Tag顶层CMakeLists改为`set(COMPONENTS main)`，按已声明依赖裁剪构建，避免编译未使用的LCD驱动。
CMake最低版本按ESP-IDF v6.0.2文档更新为3.22。烧录脚本区分首次设置目标与已有工程重新配置。
这项修改针对截图中esp_lcd_panel_rgb.c的编译器内部崩溃；不是已验证的工具链修复。
本环境无ESP-IDF/Xtensa编译器，未进行Windows构建或硬件验证。
C源码、sdkconfig.defaults、无线参数与CIR算法保持原样。

## 多距离 CIR 实验升级状态

新增电脑端 `tools/cir_experiment.py`：

- A训练、B验证、C最终测试；每组按完整采集隔离。
- 默认1/2/3/4/6/9m，可增加仅用于测试的中间距离。
- 逐点交互采集、实际距离标签、失败尝试保留、断点续采和整轮导出。
- 可复用包内三组V5训练ZIP，不导入旧HT/UDP时间窗数据。
- 仅用B选择正则化参数，模型只用A拟合；冻结哈希后采C，C不参与训练。
- 新结果保存在 `experiments/LOS_MULTI/fit/`，不覆盖旧实验结果。

10项软件测试通过。新增实验尚未连接硬件采集，不存在新一轮精度结果。
Anchor/Tag的C源码、sdkconfig和此前CIR算法与模型均保持原样；顶层CMake与烧录脚本见本页新修复说明。

## 此前 CIR 拟合状态

电脑端 CIR 重建、正则化距离拟合、模型JSON、整组留出评估、独立2m评估和实时串口预测已完成。
默认主输出为 cir_direct。根目录models/仍是旧1/2/3m模型，状态为EXPERIMENTAL_SMALL_DATA。
旧结果见 README_先看.md 与 results/fit_report.md。

## 原 V5 记录，仅供历史参考

- C Tag source: changed (802.11 Public Action/FTM response type + dialog/followup token extraction); ESP-IDF 6.0.2 hardware compiler not available in this container. **NOT binary-verified or flashed**.
- Anchor source: unchanged from v4, no need to reflash if running normally.
- ESP-IDF optional patch: safe dry-run + backup, adapted to observed v6.0.2 public native header; untested on hardware. Does not guarantee accurate ToF.
- Offline Python: syntax checked; parser synthetic test and token/clock matching synthetic test passed.
- Matching: action33 frame identity; clock-offset coherence is preliminary only; corrected distance is intentionally not fabricated.
- Original v4 data from probe time windows cannot be retroactively treated as same FTM-frame CSI.

