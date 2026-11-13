# ESP-IDF 6.0.2 兼容性检查状态（2026-10-18）

- 从用户截图确认：Tag main 组件中 `#include "esp_timer.h"` 报错，其依赖未声明。
- 已修复：两个工程 `main/CMakeLists.txt` 显式声明 `esp_timer` 与 `esp_hw_support` 等依赖。
- 已修复：ESP-IDF 6.0 删除 `wifi_ftm_initiator_cfg_t.use_get_report_api`；在 v6 不再访问该字段。
- 已修复：`WIFI_BW_HT20` / `WIFI_BW_HT40` 改为 v6.0 的 `WIFI_BW20` / `WIFI_BW40`。
- 已依据 Espressif ESP-IDF v6.0.2 官方文档及其版本迁移指南核对上述 API 变更。
- 已运行：Python py_compile 检查，源码静态断言与 ZIP CRC 完整性检查。
- **未运行：实际 ESP-IDF v6.0.2 `idf.py build`、flash、真实板卡运行**。该环境没有 ESP-IDF 工具链和实体板卡，无法保证源码不存在其他编译/运行问题。若失败，请提交完整的**第一条** `error:`。
- 不需要 Anchor 重新烧录才能修复 Tag 的 `esp_timer.h`；若之前 Anchor 已成功且仍能工作，可保留。
- 不要把本工程的原始距离诊断当成已验证的高精度校准算法。
