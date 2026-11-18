# ESP32-S3 Target：7 Anchor 版本（A7 禁用）

此工程由原 8 Anchor Target 修改而来。

启用 Anchor：A1、A2、A3、A4、A5、A6、A8。
A7 不扫描、不测距、不参与每轮 Epoch。

注意：`CONFIG_FTM_MAX_ANCHORS` 仍为 8，这是为了让 A8 继续使用 `id-1 = 7` 的数组索引；实际启用数量是 7。

正常启动日志应包含：

`TREADY,anchors=7,ids=A1:A2:A3:A4:A5:A6:A8,...`

每轮最多会测到 7 个 Anchor，且不会等待 FTM_A7。

## 烧录

在 ESP-IDF PowerShell 中进入工程目录，然后：

```powershell
idf.py set-target esp32s3
idf.py fullclean
idf.py build
idf.py -p COMx flash monitor
```

将 COMx 换成 Target 实际端口。

## PC 端坐标（X,Y）

- A1 = (45.67, 5.07)
- A2 = (28.30, 14.62)
- A3 = (27.42, 6.40)
- A4 = (17.24, 5.10)
- A5 = (35.40, 6.40)
- A6 = (15.53, 13.26)
- A8 = (35.49, 14.18)

A7 应从 PC 端定位配置中删除或禁用。
