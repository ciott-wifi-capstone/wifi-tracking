# ESP32-S3 多 Anchor FTM + CSI 固件（严格 Round/Epoch 版）

这套固件基于你原来的 `04_multi_anchor_3d` 修改，保留原有 `MFUSED schema 1`，所以现有 PC 解析器仍可继续使用。

## 这版固件做了什么

1. 每轮测量明确输出 `MROUND_START` 和 `MROUND_META`。
2. 每个 Anchor 有独立 `MSLOT_START / MSLOT_END` 时间戳。
3. `MFUSED` 仍带 `round_id + ESP32 esp_timer 时间戳`，格式不改。
4. CSI 只在“当前正在测距的 Anchor 时间槽”内累计；进入下一块 Anchor 前会清空该 Anchor 的 CSI 统计，防止上一轮/旧 CSI 混进当前 FTM。
5. 当前六块固定板按 ID：A1、A2、A3、A5、A6、A7，均提供独立项目文件夹。
6. 默认 Wi-Fi channel=6，20 MHz；Anchor CSI probe period=40 ms；Tag 默认最多扫描 ID 1..7，FTM 每次 8 帧。

## 非常重要：Anchor 坐标不烧进固定板或 Tag

固定板固件只需要知道自己的 ID（A1/A2/...）。

`(x, y, z)` 坐标属于 PC 端定位解算配置（例如 `anchors_3d.json`），不应该写进 ESP32 固件。

所以你说的“从左往右第三个天线改成 (19.27, 8.72)”需要在 PC 的坐标配置中改对应 Anchor ID；请先确认这块板实际是 A几。

## 固定 Anchor 烧录

例如烧 A1：

```powershell
cd anchor_A1
idf.py set-target esp32s3
idf.py fullclean
idf.py build
idf.py -p COM5 flash monitor
```

A2/A3/A5/A6/A7 分别进入对应目录烧录。每个目录 `sdkconfig.defaults` 已经写好唯一 ID。

如果 COM 口不同，把 `COM5` 改成该板实际端口。

## 移动 Tag 烧录

```powershell
cd tag
idf.py set-target esp32s3
idf.py fullclean
idf.py build
idf.py -p COM7 flash monitor
```

## 正常启动时应该看到

Anchor：

```text
AREADY,id=1,ssid=FTM_A1,...
```

Tag：

```text
TREADY,anchors=7,frames=8,channel=6,bw=20
MROUND_START,1,...
MSLOT_START,1,1,...
MFUSED,1,1,...
MTIME,1,1,...
MSLOT_END,1,1,...
...
MROUND,1,...
MROUND_META,1,...
```

## 关于“同一个时间戳内的数据一起定位”

这版固件给每条测量保留真实 ESP32 时间戳，并明确分 Round。PC 端应该只使用同一个 `round_id` 中的 Anchor 数据来做一次 WLS/鲁棒定位；缺失的 Anchor 不要用上一轮数据补。

移动速度较快时，后续可以再利用每条 `MFUSED` 的独立 `esp_us` 做异步 EKF 更新。
