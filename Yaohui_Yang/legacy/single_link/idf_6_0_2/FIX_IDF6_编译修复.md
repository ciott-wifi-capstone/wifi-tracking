# 适用于 ESP-IDF v6.0.2 的 Tag 编译修复

你截图里运行的目录是 `C:\\Users\\YOUR_USER\Desktop\QAR_FTM_S3\tag`，所以改的是 **Tag (COM12)**，不是 Anchor。

## 必须修复的源码内容

1. `tag/main/CMakeLists.txt` 加入 `esp_timer`，解决 `esp_timer.h: No such file or directory`。
2. 在 `tag/main/app_main.c`，ESP-IDF 6 已删除 `use_get_report_api`；本工程用版本判断避免访问该字段。
3. ESP-IDF 6 已将带宽常量名改为 `WIFI_BW20` 和 `WIFI_BW40`。
4. 两个 main 组件还声明了 `esp_hw_support` 供 `esp_mac.h` 使用。

## 直接操作（IDF v6.0.2 PowerShell）

备份旧工程，把压缩包里的 `QAR_FTM_S3` **覆盖**原桌面工程，确认 `tag/main/CMakeLists.txt` 已包含 `esp_timer`。然后：

```powershell
cd C:\\Users\\YOUR_USER\Desktop\QAR_FTM_S3\tag
idf.py reconfigure
idf.py build
idf.py -p COM12 flash monitor
```

**只有 `idf.py build` 显示 `Project build complete` 后才能确认主程序编译成功。** 即使看到 `Successfully created ESP32-S3 image`，若最后有 `ninja: build stopped`，仍是失败。

如果之后要烧录新 Anchor：

```powershell
cd C:\\Users\\YOUR_USER\Desktop\QAR_FTM_S3\anchor
idf.py reconfigure
idf.py build
idf.py -p COM10 flash monitor
```

现在的 Anchor 只要已经能提供 `RTT_CIR_LAB` + FTM Responder，就可以暂时不烧。

此代码提供原始 FTM / CSI 采集和诊断，**没有宣称已经提高测距准确度，也未复刻 Wi-PRO 的原始时间戳补偿**。
