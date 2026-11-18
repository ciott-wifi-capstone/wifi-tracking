
from __future__ import annotations

import csv
import math
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1] / "results" / "multi_anchor_3d_live"

# 你这次确定的 Tag 参考直线
TAG_Y = 6.415

# 先按 Dashboard 当前配置使用 1.0 m。
# 如果之后量得更准确，例如 0.94 m，只改这里即可。
TAG_Z = 1.0

# 每个 P 点约停 5 秒
STOP_SECONDS = 5.0

ANCHORS = {
    1: np.array([21.58, 5.64, 1.35], dtype=float),
    2: np.array([31.57, 5.64, 2.14], dtype=float),
    3: np.array([28.16, 7.10, 0.00], dtype=float),
    5: np.array([41.35, 5.64, 0.52], dtype=float),
    6: np.array([36.52, 6.94, 1.32], dtype=float),
    7: np.array([47.22, 5.64, 0.52], dtype=float),
}

POINTS = {
    "P_A1": np.array([21.58, TAG_Y, TAG_Z], dtype=float),
    "P_A3": np.array([28.16, TAG_Y, TAG_Z], dtype=float),
    "P_A2": np.array([31.57, TAG_Y, TAG_Z], dtype=float),
    "P_A6": np.array([36.52, TAG_Y, TAG_Z], dtype=float),
    "P_A5": np.array([41.35, TAG_Y, TAG_Z], dtype=float),
    "P_A7": np.array([47.22, TAG_Y, TAG_Z], dtype=float),
}

FORWARD_ROUTE = ["P_A1", "P_A3", "P_A2", "P_A6", "P_A5", "P_A7"]
REVERSE_ROUTE = list(reversed(FORWARD_ROUTE))

# 当前 5 次正向 + 5 次反向
SESSIONS = {
    # A1 -> A7
    "session-20260909-170554": "forward",
    "session-20260909-170831": "forward",
    "session-20260909-171035": "forward",
    "session-20260909-173739": "forward",
    "session-20260909-173934": "forward",

    # A7 -> A1
    "session-20260909-172450": "reverse",
    "session-20260909-172653": "reverse",
    "session-20260909-172853": "reverse",
    "session-20260909-174125": "reverse",
    "session-20260909-174317": "reverse",
}


def build_schedule(t0: float, t1: float, route: list[str]):
    """按“每个 P 点停约5秒 + 点间近似匀速”构造真值时间表。"""
    span = t1 - t0
    n_stops = len(route)
    total_stop = STOP_SECONDS * n_stops
    moving_time = span - total_stop

    if moving_time <= 0:
        raise RuntimeError(
            f"Session 太短：总时长 {span:.1f}s，小于停留总时间 {total_stop:.1f}s"
        )

    distances = []
    for a, b in zip(route[:-1], route[1:]):
        distances.append(float(np.linalg.norm(POINTS[b][:2] - POINTS[a][:2])))

    total_distance = sum(distances)
    segment_times = [moving_time * d / total_distance for d in distances]

    phases = []
    t = t0

    for i, point_name in enumerate(route):
        # 停留
        stop_start = t
        stop_end = t + STOP_SECONDS
        phases.append({
            "kind": "stop",
            "start": stop_start,
            "end": stop_end,
            "p0": POINTS[point_name],
            "p1": POINTS[point_name],
            "label": point_name,
        })
        t = stop_end

        # 移动到下一点
        if i < len(route) - 1:
            next_name = route[i + 1]
            move_end = t + segment_times[i]
            phases.append({
                "kind": "move",
                "start": t,
                "end": move_end,
                "p0": POINTS[point_name],
                "p1": POINTS[next_name],
                "label": f"{point_name}->{next_name}",
            })
            t = move_end

    # 浮点误差让最后阶段精确落在 t1
    phases[-1]["end"] = t1
    return phases


def truth_at(t: float, phases):
    if t <= phases[0]["start"]:
        return phases[0]["p0"].copy(), phases[0]["label"], phases[0]["kind"]

    for phase in phases:
        if phase["start"] <= t <= phase["end"]:
            if phase["kind"] == "stop":
                return phase["p0"].copy(), phase["label"], "stop"

            denom = max(phase["end"] - phase["start"], 1e-9)
            alpha = min(max((t - phase["start"]) / denom, 0.0), 1.0)
            p = phase["p0"] + alpha * (phase["p1"] - phase["p0"])
            return p, phase["label"], "move"

    return phases[-1]["p1"].copy(), phases[-1]["label"], phases[-1]["kind"]


def annotate_session(session_name: str, direction: str):
    session_dir = ROOT / session_name
    src = session_dir / "link_measurements.csv"
    dst = session_dir / "link_measurements_labeled.csv"

    if not src.exists():
        print(f"[SKIP] {session_name}: 找不到 {src.name}")
        return

    with src.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        print(f"[SKIP] {session_name}: CSV为空")
        return

    times = []
    for row in rows:
        try:
            times.append(float(row["pc_time_s"]))
        except (KeyError, TypeError, ValueError):
            pass

    if not times:
        print(f"[SKIP] {session_name}: 没有有效时间戳")
        return

    t0, t1 = min(times), max(times)
    route = FORWARD_ROUTE if direction == "forward" else REVERSE_ROUTE
    phases = build_schedule(t0, t1, route)

    fieldnames = list(rows[0].keys())
    extra_fields = ["true_x_m", "true_y_m", "true_z_m", "route_phase", "route_direction"]
    for name in extra_fields:
        if name not in fieldnames:
            fieldnames.append(name)

    labeled = 0

    for row in rows:
        try:
            t = float(row["pc_time_s"])
            aid = int(row["anchor"])
        except (KeyError, TypeError, ValueError):
            continue

        if aid not in ANCHORS:
            continue

        tag, phase_label, phase_kind = truth_at(t, phases)
        dist = float(np.linalg.norm(tag - ANCHORS[aid]))

        row["true_x_m"] = f"{tag[0]:.6f}"
        row["true_y_m"] = f"{tag[1]:.6f}"
        row["true_z_m"] = f"{tag[2]:.6f}"
        row["true_distance_m"] = f"{dist:.6f}"
        row["point_id"] = phase_label
        row["condition"] = f"route_{direction}_{phase_kind}"
        row["route_phase"] = phase_label
        row["route_direction"] = direction
        labeled += 1

    with dst.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(
        f"[OK] {session_name:28s} {direction:7s} "
        f"duration={t1-t0:6.1f}s rows={len(rows):5d} labeled={labeled:5d}"
    )
    print(f"     -> {dst}")


def main():
    print("=" * 72)
    print("Label route sessions for GRU V2")
    print(f"ROOT      : {ROOT}")
    print(f"TAG line  : y={TAG_Y:.3f} m, z={TAG_Z:.3f} m")
    print(f"Stop time : {STOP_SECONDS:.1f} s / point")
    print("=" * 72)

    for session_name, direction in SESSIONS.items():
        annotate_session(session_name, direction)

    print()
    print("完成。原始 link_measurements.csv 没有修改。")
    print("训练请使用每个 Session 新生成的 link_measurements_labeled.csv。")


if __name__ == "__main__":
    main()
