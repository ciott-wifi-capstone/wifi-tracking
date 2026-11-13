# 验证状态

- 已实测：使用用户上传的 `recordings(2).zip` 运行 `tools/analyze_recordings.py`，成功生成统计 CSV。
- 已运行：Python `py_compile`、旧数据解析和打包结构检查。
- 已按 ESP-IDF 5.5.3 官方文档核对：FTM start/end/get_report、Responder offset、CSI 获取方法、相关数据结构字段。
- 尚未运行：在 ESP-IDF 5.5.x 环境 `idf.py build`、实体 ESP32-S3 上 flash/串口观测、20/40 MHz 空口切换验证。
- **因此不是现成 `.bin`**，需要在你的电脑 `idf.py build` 后烧录。
- V0 数据采集与诊断，不包含未经验证的实时精确距离补偿。
