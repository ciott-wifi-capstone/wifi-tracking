#!/usr/bin/env python3
"""Collect Wi-PRO serial records, write CSV files, and optionally plot live ranges.

Example commands
    python src/wipro_live.py --port COM24 --seconds 120 --no-plot
    python src/wipro_live.py --port COM24 --interval 1000 --burst 16

Requires pyserial. Plotting additionally requires matplotlib and a GUI backend.
"""

from __future__ import annotations

import argparse
import base64
import bisect
import collections
import csv
import math
import pathlib
import re
import struct
import sys
import threading
import time

import node_labels

sys.stdout.reconfigure(errors="replace")

C_M_PER_S = 299792458.0
PS_TO_M = C_M_PER_S * 1e-12 / 2.0          # Round-trip picoseconds to one-way metres.
FRAME_START, FRAME_END, SEP = 0x02, 0x03, 0x01
# Board identity comes from the firmware MAC response.
# node_labels matches complete registered address variants.
KNOWN_KINDS = {"CSI", "FTM", "RANGE", "DBG", "MAC"}
MAC_RE = re.compile(r"\b([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b")


def iter_frames(buffer: bytearray):
    """Yield complete known TLV frames while discarding interleaved console text.

    Keep a partial trailing frame for the next read. Unknown frame types trigger
    resynchronisation at the next start delimiter.
    """
    while True:
        start = buffer.find(FRAME_START)
        if start < 0:
            buffer.clear()
            return
        end = buffer.find(FRAME_END, start + 1)
        if end < 0:
            del buffer[:start]                 # Retain an incomplete frame.
            return
        payload = bytes(buffer[start + 1:end])
        kind_bytes, _, rest = payload.partition(bytes([SEP]))
        kind = kind_bytes.decode("ascii", "replace").strip()
        if kind not in KNOWN_KINDS:
            del buffer[:start + 1]             # Resynchronise after an unknown frame type.
            continue
        del buffer[:end + 1]
        fields = [p.decode("utf-8", "replace").strip()
                  for p in rest.split(bytes([SEP]))] if rest else []
        yield kind, fields


def diff_48bit(a: int, b: int) -> int:
    """Compute a signed 48-bit timestamp difference with hardware-counter wraparound."""
    diff = (a - b) & 0xFFFFFFFFFFFF
    return diff - 0x1000000000000 if diff >= 0x800000000000 else diff


def parse_ftm(fields: list[str]) -> tuple[str, list[dict]]:
    """Decode 40-byte FTM report blocks and attach timestamp-matched CSI.

    Report blocks use little-endian timestamps. Trailing CSI fields are grouped as
    channel, secondary channel, payload, timestamp and MAC timestamp. CSI pairing
    uses the report's t2 timestamp with a strict 2000 ps tolerance.
    """
    if len(fields) < 3:
        return "", []
    peer = fields[0]
    reports: list[dict] = []
    # Firmware emits peer and report count before the first payload.
    index = 2
    while index < len(fields):
        if fields[index] == "CSI":
            index += 1
            break
        try:
            payload = base64.b64decode(fields[index], validate=False)
        except Exception:
            payload = b""
        for offset in range(0, len(payload) - 39, 40):
            chunk = payload[offset:offset + 40]
            if len(chunk) < 40:
                break
            t1, t2, t3, t4 = struct.unpack_from("<QQQQ", chunk, 8)
            rtt_ps = diff_48bit(t4, t1) - diff_48bit(t3, t2)
            reports.append({
                "token": chunk[0], "rssi": struct.unpack_from("<b", chunk, 1)[0],
                "t1": t1, "t2": t2, "t3": t3, "t4": t4,
                "rtt_ps": rtt_ps, "raw_m": rtt_ps * PS_TO_M,
                "csi_b64": "",
            })
        index += 1
    # Remaining CSI fields form five-value groups.
    while index + 5 <= len(fields):
        csi_b64 = fields[index + 2]
        try:
            t_ps = int(fields[index + 3]) * 1000
        except ValueError:
            index += 5
            continue
        for report in reports:
            if abs(t_ps - report["t2"]) < 2000:
                report["csi_b64"] = csi_b64
                break
        index += 5
    return peer, reports


def parse_csi(fields: list[str]) -> dict | None:
    """Validate and decode a ten-field CSI frame.

    The size field counts base64 characters. Reject malformed addresses, payload
    lengths, base64 data and incomplete complex samples.
    """
    if len(fields) != 10:
        return None
    if not MAC_RE.fullmatch(fields[2]):
        return None
    payload = fields[5]
    if not fields[4].isdigit() or len(payload) != int(fields[4]):
        return None
    try:
        if len(base64.b64decode(payload, validate=True)) % 2:
            return None                      # Complex CSI uses pairs of bytes.
    except Exception:                        # noqa: BLE001
        return None
    try:
        return {
            "mac_timestamp": int(fields[0], 16),
            "seq": int(fields[1], 10),
            "tgt_mac": fields[2],
            "timestamp": int(fields[3], 10),
            "size": int(fields[4], 10),
            "csi_b64": payload,
            "channel": int(fields[6], 10),
            "channel2": int(fields[7], 10),
            "rssi": int(fields[8], 10),
            "sig_mode": int(fields[9], 10),
        }
    except (ValueError, IndexError):
        return None


CAPTURE_FILES = ("range.csv", "ftm.csv", "csi.csv")


def safe_out_dir(out_dir: pathlib.Path, force: bool = False,
                 per_run: bool = False) -> pathlib.Path:
    """Choose an output directory without overwriting an existing capture.

    per_run creates a timestamped subdirectory for every launch. Otherwise, redirect
    only when the requested directory already contains capture data. force writes
    directly to the requested directory and permits overwriting.
    """
    out_dir = pathlib.Path(out_dir)
    if force:
        return out_dir

    if per_run:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        target = out_dir / f"run_{stamp}"
        suffix = 2
        while target.exists():
            target = out_dir / f"run_{stamp}_{suffix}"
            suffix += 1
        print(f"本次采集写入 : {target}")
        print(f"               （每次运行一个目录，不会覆盖以前的采集；"
              f"要写回 {out_dir.name}\\ 本身加 --force）")
        return target

    # Retain the requested path when no capture files exist.
    existing = [n for n in CAPTURE_FILES
                if (out_dir / n).exists() and (out_dir / n).stat().st_size > 0]
    if not existing:
        return out_dir
    stamp = time.strftime("%Y%m%d_%H%M%S")
    target = out_dir / f"run_{stamp}"
    suffix = 2
    while target.exists():
        target = out_dir / f"run_{stamp}_{suffix}"
        suffix += 1
    print(f"注意：{out_dir} 里已经有采集（{', '.join(existing)}）。")
    print(f"      本次改写 {target}，不覆盖旧数据。（要覆盖原目录加 --force）")
    return target


class Capture:
    """Read serial frames in a worker thread and retain CSV records and UI state."""

    def __init__(self, port: str, out_dir: pathlib.Path, commands: list[str],
                 baud: int = 115200, force: bool = False, per_run: bool = False):
        self.port = port
        # Apply overwrite protection for every caller.


        self.out_dir = safe_out_dir(out_dir, force=force, per_run=per_run)
        self.commands = commands
        self.baud = baud
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.own_mac = "UNKNOWN"
        self.channel: int | None = None
        self.mute: bool | None = None
        self.wipro_on: bool | None = None
        self.peers: set[str] = set()
        self.counts = collections.Counter()
        self.dropped = collections.Counter()   # Frames rejected by validation.
        self.t0 = time.time()
        # Per-peer rows include relative time, both ranges, RSSI and hardware t2.
        self.series: dict[str, list[tuple[float, float | None, float | None, int | None]]] = {}
        self.rate_window: collections.deque = collections.deque(maxlen=400)
        self.thread: threading.Thread | None = None
        self.error: str | None = None
        self._writers: dict[str, tuple] = {}
        self._since_flush = 0
        # Resolve labels from registered MAC variants.
        self.table = node_labels.load_table()


    def _writer(self, name: str, header: list[str]):
        if name not in self._writers:
            handle = (self.out_dir / name).open("w", newline="", encoding="utf-8")
            writer = csv.writer(handle)
            writer.writerow(header)
            self._writers[name] = (handle, writer)
        # Flush open CSV files after 256 records.
        self._since_flush += 1
        if self._since_flush >= 256:
            self._since_flush = 0
            for handle, _ in self._writers.values():
                handle.flush()
        return self._writers[name][1]

    def close(self) -> None:
        for handle, _ in self._writers.values():
            try:
                handle.close()
            except Exception:
                pass
        self._writers.clear()


    def _open(self):
        import serial
        return serial.Serial(self.port, self.baud, timeout=0.05)

    def run(self) -> None:
        try:
            device = self._open()
        except Exception as exc:                     # noqa: BLE001
            self.error = f"打开 {self.port} 失败：{exc}"
            return
        self.out_dir.mkdir(parents=True, exist_ok=True)
        with device:
            time.sleep(0.3)
            device.reset_input_buffer()
            for command in self.commands:
                device.write((command + "\r\n").encode())
                self._note_command(command)
                time.sleep(0.4)
            buffer = bytearray()
            text_tail = ""
            while not self.stop.is_set():
                try:
                    chunk = device.read(8192)
                except Exception as exc:             # noqa: BLE001
                    self.error = f"读串口出错：{exc}"
                    break
                if not chunk:
                    continue
                buffer.extend(chunk)
                for kind, fields in iter_frames(buffer):
                    self._handle(kind, fields)
                # Console text supplies channel status, not board identity.


                try:
                    text_tail = (text_tail + chunk.decode("utf-8", "replace"))[-2000:]
                except Exception:
                    continue
                channel = re.search(r"Current channel:\s*(\d+)\s*\((\d+)MHz\)", text_tail)
                if channel:
                    self.channel = int(channel.group(1))
        self.close()

    def _note_command(self, command: str) -> None:
        head = command.split()[0] if command.split() else command
        if head == "mute":
            self.mute = command.strip().endswith("0")
        elif head == "wipro":
            self.wipro_on = command.strip().endswith("1")


    def _handle(self, kind: str, fields: list[str]) -> None:
        self.counts[kind] += 1
        self.rate_window.append(time.time())
        if kind == "MAC" and fields:
            # Accept board identity only from the id-command response.

            candidate = fields[0].strip().upper()
            if MAC_RE.fullmatch(candidate):
                self.own_mac = candidate
            return
        if kind == "RANGE" and len(fields) >= 3:
            peer = fields[0]
            try:
                t2 = int(fields[1])
                distance = float(fields[2])
            except ValueError:
                return
            with self.lock:
                self.peers.add(peer)
                self._append(peer, now=None, wipro=distance, t2=t2)
            writer = self._writer("range.csv", ["t_ms", "own_mac", "tgt_mac",
                                                "timestamp", "range"])
            writer.writerow([int(time.time() * 1000), self.own_mac, peer, t2,
                             f"{distance:.4f}"])
        elif kind == "FTM":
            if not fields or not MAC_RE.fullmatch(fields[0]):
                self.dropped[kind] += 1
                return
            peer, reports = parse_ftm(fields)
            if not peer or not reports:
                self.dropped[kind] += 1
                return
            writer = self._writer("ftm.csv", ["own_mac", "t_ms", "seq", "tgt_mac",
                                              "dlog_token", "rssi", "t1", "t2", "t3",
                                              "t4", "payload_b64"])
            stamp = int(time.time() * 1000)
            for report in reports:
                writer.writerow([self.own_mac, stamp, 0, peer, report["token"],
                                 report["rssi"], report["t1"], report["t2"],
                                 report["t3"], report["t4"], report["csi_b64"]])
                with self.lock:
                    self.peers.add(peer)
                    self._append(peer, now=None, raw=report["raw_m"],
                                 rssi=report["rssi"], t2=report["t2"])
        elif kind == "CSI":
            event = parse_csi(fields)
            if not event:
                # Count malformed CSI frames.
                self.dropped[kind] += 1
                return
            writer = self._writer("csi.csv", ["t_ms", "timestamp", "own_mac", "tgt_mac",
                                              "seq", "payload_b64", "mac_timestamp",
                                              "rssi", "sig_mode"])
            writer.writerow([int(time.time() * 1000), event["timestamp"], self.own_mac,
                             event["tgt_mac"], event["seq"], event["csi_b64"],
                             event["mac_timestamp"], event["rssi"], event["sig_mode"]])

    def _append(self, peer: str, now: float | None, wipro: float | None = None,
                raw: float | None = None, rssi: int | None = None,
                t2: int | None = None) -> None:
        """Merge RANGE and FTM updates by peer and hardware t2.

        Each row contains relative seconds, Wi-PRO range, raw range, RSSI and t2.
        Unmatched updates remain separate rows.
        """
        del now
        elapsed = time.time() - self.t0
        rows = self.series.setdefault(peer, [])
        if t2 is not None:
            for row in reversed(rows[-8:]):
                if row[4] == t2:
                    if wipro is not None:
                        row[1] = wipro
                    if raw is not None:
                        row[2] = raw
                        row[3] = rssi
                    return
        if wipro is not None:
            rows.append([elapsed, wipro, None, None, t2])
        else:
            rows.append([elapsed, None, raw, rssi, t2])

    def snapshot(self, since: float | None = None):
        """Copy UI state, optionally restricting each peer's series to a time window.

        Series are time ordered, so bisect locates the first included row.
        """
        with self.lock:
            if since is None:
                series = {peer: [tuple(r) for r in rows]
                          for peer, rows in self.series.items()}
            else:
                series = {}
                for peer, rows in self.series.items():
                    # Search by timestamp because stored rows are lists.


                    start = bisect.bisect_left(rows, since, key=lambda r: r[0])
                    series[peer] = [tuple(r) for r in rows[start:]]
            return {
                "own_mac": self.own_mac, "channel": self.channel,
                "peers": sorted(self.peers), "counts": dict(self.counts),
                "dropped": dict(self.dropped),
                "labels": {peer: node_labels.label_for(peer, self.table)
                           for peer in self.peers},
                "own_label": node_labels.label_for(self.own_mac, self.table),
                "known": dict(self.table),
                "series": series, "error": self.error,
                "mute": self.mute, "wipro": self.wipro_on,
            }


def run_with_plot(capture: Capture, window_s: float, seconds: float | None) -> None:
    import matplotlib
    # Try GUI backends before the matplotlib default.

    for backend in ("TkAgg", "QtAgg", None):
        try:
            if backend:
                matplotlib.use(backend, force=True)
            import matplotlib.pyplot as plt  # noqa: F401
            break
        except Exception:                        # noqa: BLE001
            continue
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation

    figure, axes = plt.subplots(3, 1, figsize=(11, 8.5), sharex=True)
    figure.suptitle("Wi-PRO 实时看板", fontsize=13)
    ax_range, ax_cmp, ax_rssi = axes
    ax_range.set_ylabel("Wi-PRO 距离 (m)")
    ax_cmp.set_ylabel("距离 (m)")
    ax_cmp.set_xlabel("时间 (s)")
    ax_rssi.set_ylabel("RSSI (dBm)")
    for axis in axes:
        axis.grid(alpha=0.3)
    status = figure.text(0.01, 0.012, "", fontsize=9, family="monospace",
                         verticalalignment="bottom")

    def update(_frame):
        snap = capture.snapshot()
        for axis in axes:
            axis.clear()
            axis.grid(alpha=0.3)
        ax_range.set_ylabel("Wi-PRO 距离 (m)")
        ax_cmp.set_ylabel("距离 (m)")
        ax_rssi.set_ylabel("RSSI (dBm)")
        ax_rssi.set_xlabel("时间 (s)")

        now = time.time() - capture.t0
        labels = snap["labels"]
        for peer, rows in snap["series"].items():
            label = labels.get(peer, peer[-8:])
            points = [r for r in rows if r[1] is not None and r[0] > now - window_s]
            if points:
                ax_range.plot([p[0] for p in points], [p[1] for p in points],
                              marker=".", markersize=3, linewidth=1, label=label)
                ax_cmp.plot([p[0] for p in points], [p[1] for p in points],
                            marker=".", markersize=3, linewidth=1)
            raws = [r for r in rows if r[2] is not None and r[0] > now - window_s]
            if raws:
                ax_cmp.plot([r[0] for r in raws], [r[2] for r in raws],
                            linestyle="--", linewidth=1, alpha=0.65)
            rssis = [r for r in rows if r[3] is not None and r[0] > now - window_s]
            if rssis:
                ax_rssi.plot([r[0] for r in rssis], [r[3] for r in rssis],
                             linewidth=1, label=label)
        if snap["series"]:
            ax_range.legend(fontsize=7, ncol=2, loc="upper right")
            ax_cmp.set_title("实线 = Wi-PRO 修正后；虚线 = 原始 FTM（同色为同一节点）",
                             fontsize=9)
        else:
            ax_range.set_title("还没有 peer —— 至少两块板通电、且都已刷 Wi-PRO 固件，"
                               "互相发现后才会出距离", fontsize=10, color="darkred")
        if now > 5:
            ax_range.set_xlim(max(0.0, now - window_s), now + 0.5)

        counts = snap["counts"]
        recent = [t for t in capture.rate_window if t > time.time() - 2.0]
        dropped = snap["dropped"]
        drop_note = ("    丢弃 " + " ".join(f"{k}×{v}" for k, v in dropped.items())
                     if dropped else "")
        status.set_text(
            f"端口 {capture.port}   本机 {snap['own_label']} ({snap['own_mac']})   "
            f"信道 {snap['channel']}   mute={'开(静音)' if snap['mute'] is False else snap['mute']}"
            f"   wipro={snap['wipro']}\n"
            f"peers {len(snap['peers'])}: "
            f"{', '.join(labels.get(p, p[-8:]) for p in snap['peers']) or '（无）'}"
            f"    帧计数 {counts}    CSI 速率 {len(recent) / 2.0:.1f} 帧/s{drop_note}"
            + (f"\n⚠ {snap['error']}" if snap["error"] else ""))
        return []

    animation = FuncAnimation(figure, update, interval=500, cache_frame_data=False)
    if seconds:
        deadline = time.time() + seconds
        while time.time() < deadline and not capture.stop.is_set():
            plt.pause(0.5)
    else:
        plt.show()
    del animation


def autodetect_port() -> str | None:
    """Rank USB serial ports, preferring native Espressif USB over UART bridges.

    Exclude Bluetooth ports. The Wi-PRO data console uses native USB.
    """
    try:
        from serial.tools import list_ports
    except ImportError:
        return None
    ranked: list[tuple[int, str]] = []
    for port in list_ports.comports():
        hwid = (port.hwid or "").upper()
        description = f"{port.description or ''} {port.product or ''}".upper()
        if "BTHENUM" in hwid or "BLUETOOTH" in description or "蓝牙" in description:
            continue
        if "303A" in hwid:
            rank = 3
        elif "10C4" in hwid:
            rank = 2
        elif "USB" in hwid or "USB" in description:
            rank = 1
        else:
            continue
        ranked.append((rank, port.device))
    if not ranked:
        return None
    ranked.sort(reverse=True)
    return ranked[0][1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", default=None, help="COM 口；不给则自动探测")
    parser.add_argument("--out", type=pathlib.Path, default=None,
                        help="CSV 输出目录（默认 ./wipro_live_data）")
    parser.add_argument("--interval", type=int, default=1000,
                        help="发起 FTM 的周期 ms（固件默认 5000）")
    parser.add_argument("--burst", type=int, default=None,
                        help="每次交换的 FTM 帧数，可选 16/25/32/64")
    parser.add_argument("--raw-ftm", action="store_true",
                        help="不开 wipro（只留原始 FTM 基线，用于 A/B 对照）")
    parser.add_argument("--keep-mute", action="store_true",
                        help="不下发 mute 0（默认会下发，把本机切成发起方）")
    parser.add_argument("--window", type=float, default=60.0, help="绘图时间窗（秒）")
    parser.add_argument("--seconds", type=float, default=None,
                        help="只跑这么多秒后退出（不给则一直跑）")
    parser.add_argument("--no-plot", action="store_true", help="只采集不绘图")
    parser.add_argument("--force", action="store_true",
                        help="允许覆盖输出目录里已有的采集（默认绝不覆盖，"
                             "改写带时间戳的子目录）")
    args = parser.parse_args()

    port = args.port or autodetect_port()
    if not port:
        raise SystemExit(
            "没找到板子的串口。\n"
            "  * 板子插好了吗？（要看数据必须插【原生 USB】口，不是 CP210x 那个 UART 口）\n"
            "  * 串口被 WSL 占着？在 Windows 执行  usbipd detach --busid <BUSID>\n"
            "  * 或者用 --port COMx 明确指定")
    out_dir = args.out or (pathlib.Path.cwd() / "wipro_live_data")

    commands = ["id"]          # Request the board identity frame.
    if not args.keep_mute:
        commands.append("mute 0")
    commands.append("wipro 0" if args.raw_ftm else "wipro 1")
    commands.append(f"interval {args.interval}")
    if args.burst:
        commands.append(f"burst {args.burst}")
    commands.append("channel")   # Request the channel for the status display.

    # Give each CLI capture a separate output directory.


    capture = Capture(port, out_dir, commands, force=args.force, per_run=True)

    print(f"端口      : {port}")
    # Report the actual directory after overwrite protection.

    print(f"输出目录  : {capture.out_dir}")
    print(f"启动命令  : {'; '.join(commands)}")
    print(f"模式      : {'原始 FTM 基线（wipro 关）' if args.raw_ftm else 'Wi-PRO 修正（wipro 开）'}")

    capture.thread = threading.Thread(target=capture.run, daemon=True)
    capture.thread.start()
    time.sleep(3.0)
    if capture.error:
        raise SystemExit(capture.error)
    total = sum(capture.snapshot()["counts"].values())
    if total == 0:
        print("⚠ 端口打开了，但 3 秒内一帧 TLV 都没收到。可能的原因：")
        print("  1) 连的是 CP210x 那个 UART 口 —— Wi-PRO 的控制台/数据在【原生 USB】口上")
        print("     （firmware/sdkconfig.defaults: CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y）")
        print("  2) 板子没在跑 Wi-PRO 固件（可以按一下复位）")
        print("  3) 波特率/端口选错了（USB-JTAG 忽略波特率，但必须是对的那个口）")
        print("  仍然继续采集，收到帧就会自动开始统计。")
    else:
        print(f"已连接，3 秒内收到 {total} 帧。开始采集，Ctrl+C 结束。\n")

    try:
        if args.no_plot:
            deadline = time.time() + (args.seconds or 1e9)
            while time.time() < deadline:
                time.sleep(1.0)
                snap = capture.snapshot()
                if int(time.time()) % 5 == 0:
                    print(f"  peers={len(snap['peers'])} 帧={snap['counts']} "
                          f"误差={snap['error'] or '无'}")
        else:
            run_with_plot(capture, args.window, args.seconds)
    except KeyboardInterrupt:
        pass
    finally:
        capture.stop.set()
        time.sleep(0.5)
        snap = capture.snapshot()
        # Record provenance after the board identity has arrived.

        try:
            import capture_provenance as prov
            p = prov.write(capture.out_dir, commands=commands,
                           board_mac=snap.get("own_mac"),
                           note=f"wipro_live capture; own_label={snap.get('own_label')}")
            if p:
                print(f"溯源文件  : {p.name}")
        except Exception:                                     # noqa: BLE001
            pass
        print(f"\n采集结束。本机 MAC {snap['own_mac']}，信道 {snap['channel']}，"
              f"peers {len(snap['peers'])}，帧计数 {snap['counts']}")
        print(f"CSV 在 {capture.out_dir}")
        if snap["peers"]:
            print("下一步：python wipro_adapter.py " + str(capture.out_dir))
        else:
            print("没有 peer，所以没有 FTM/RANGE。至少两块板通电并互相发现后才能测距。")


if __name__ == "__main__":
    main()
