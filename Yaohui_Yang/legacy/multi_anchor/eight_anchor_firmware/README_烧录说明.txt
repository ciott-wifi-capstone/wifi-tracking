ESP32-S3 8 Anchor + 1 Tag 固件说明
=================================

固定 Anchor 与坐标：
A1 = (45.67,  5.07) m
A2 = (28.30, 14.62) m
A3 = (27.42,  6.40) m
A4 = (17.24,  5.10) m
A5 = (35.40,  6.40) m
A6 = (15.53, 13.26) m
A7 = (49.93, 15.30) m
A8 = (35.49, 14.18) m

重要：这些 XY 坐标不写进 Anchor 固件。Anchor 固件只负责唯一 ID / SSID：
A1 -> FTM_A1
A2 -> FTM_A2
...
A8 -> FTM_A8
坐标由电脑端定位程序使用。coordinates_xy.json 仅作为坐标参考。

每块 Anchor 都是独立 ESP-IDF 工程：
anchor_A1 ... anchor_A8
移动板工程：tag_8anchors

以 A2、COM4 为例：
cd C:\\Users\\YOUR_USER\Desktop\anchor_A2
idf.py set-target esp32s3
idf.py fullclean
idf.py build
idf.py -p COM4 flash monitor

启动成功应看到：
AREADY,id=2,ssid=FTM_A2,...

移动 Tag：
cd C:\\Users\\YOUR_USER\Desktop\tag_8anchors
idf.py set-target esp32s3
idf.py fullclean
idf.py build
idf.py -p COM4 flash monitor

启动成功应看到：
TREADY,anchors=8,...
并在扫描时看到 A1...A8 中实际在线的板子。

本版本相较 6-Anchor 版本：
1. 新增 A4、A8 固件；
2. Tag 最大 Anchor 数固定为 8；
3. 保留 Round/Epoch：每轮 A1...A8 的新测量按同一个 round_id 输出；
4. CSI 只在当前 Anchor 的测距 slot 内累计；
5. 8 块 Anchor 的 CSI probe 在 40 ms 周期内分 8 个相位，减少互相碰撞；
6. MCSI_DIAG 的 match 计数扩展到 8 个 Anchor。

注意：如果做 2D 定位但各 Anchor 高度不同，PC 端仍应录入每块板真实 z 高度。
只给 XY 而把 z 随意设为 0 会给高精度测距带来额外几何误差。
