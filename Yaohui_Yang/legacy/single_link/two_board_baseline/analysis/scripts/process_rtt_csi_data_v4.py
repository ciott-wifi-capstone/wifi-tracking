# -*- coding: utf-8 -*-
r"""
RTT + CSI offline data processor, robust version.

Run from any directory:
    python analysis/scripts/process_rtt_csi_data_v4.py

Expected folder structure:
    02_two_esp_archive/data/distance_campaign/1m/*_rtt_raw.csv
    02_two_esp_archive/data/distance_campaign/1m/*_csi_packet_raw.csv
    02_two_esp_archive/data/distance_campaign/2m/...

Outputs:
    02_two_esp_archive/results/analysis_output_v3/
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Optional, Dict, List

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

# =========================
# User settings
# =========================
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data" / "distance_campaign"
OUT_DIR = PROJECT_ROOT / "results" / "analysis_output_v3"

PROCESS_CSI = True          # 如果还是 CSI 报错，可以先改成 False，只处理 RTT
ONLY_STATUS_OK = True
MIN_REPORT_ENTRIES = 1      # 先用 1 保证有图；论文最终版可改成 8
MIN_AVG_RSSI_DBM = -100     # 先用 -100 保证有图；论文最终版可改成 -85 或 -90
REMOVE_OUTLIERS = True
OUTLIER_Q_LOW = 0.01
OUTLIER_Q_HIGH = 0.99

FIG_DPI = 200
MAX_CSI_ROWS_FOR_AMPLITUDE = 8000
DISTANCE_COLUMN_PRIORITY = ["dist_filt_cm", "dist_cal_cm", "dist_raw_cm", "dist_est_cm"]


# =========================
# Helper functions
# =========================
def mkdir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def infer_actual_distance_m(path: Path) -> Optional[float]:
    """Infer actual distance from folder name, e.g. data/3m/file.csv -> 3."""
    for part in reversed(path.parts):
        m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*m", part.lower())
        if m:
            return float(m.group(1))
    return None


def read_csv_safe(path: Path) -> pd.DataFrame:
    """Read CSV robustly. Bad lines are skipped instead of crashing."""
    try:
        return pd.read_csv(path, on_bad_lines="skip", engine="python")
    except TypeError:
        # For very old pandas versions
        return pd.read_csv(path, error_bad_lines=False, engine="python")
    except Exception as e:
        print(f"WARNING: failed to read {path}: {e}")
        return pd.DataFrame()


def add_relative_time(df: pd.DataFrame) -> pd.DataFrame:
    """Add t_rel_s using pc_time_s, or esp_time_us, or row index fallback."""
    df = df.copy()
    if df.empty:
        df["t_rel_s"] = []
        return df

    if "pc_time_s" in df.columns:
        t = pd.to_numeric(df["pc_time_s"], errors="coerce")
        if t.notna().any():
            df["t_rel_s"] = t - t.min()
            return df

    if "esp_time_us" in df.columns:
        t = pd.to_numeric(df["esp_time_us"], errors="coerce")
        if t.notna().any():
            df["t_rel_s"] = (t - t.min()) / 1_000_000.0
            return df

    df["t_rel_s"] = np.arange(len(df), dtype=float)
    return df


def choose_distance_cm(df: pd.DataFrame) -> pd.Series:
    chosen = pd.Series(np.nan, index=df.index, dtype="float64")
    for col in DISTANCE_COLUMN_PRIORITY:
        if col in df.columns:
            vals = pd.to_numeric(df[col], errors="coerce")
            vals = vals.where(vals > 0)
            chosen = chosen.fillna(vals)
    return chosen


def filter_rtt(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    df = add_relative_time(df)
    df["distance_cm_used"] = choose_distance_cm(df)
    df["distance_m_used"] = df["distance_cm_used"] / 100.0

    for col in ["status", "report_entries", "avg_rssi", "rtt_raw_ns", "rtt_est_ns", "distance_m_used"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    mask = df["distance_m_used"].notna() & (df["distance_m_used"] > 0)
    if ONLY_STATUS_OK and "status" in df.columns:
        mask &= df["status"] == 0
    if "report_entries" in df.columns:
        mask &= df["report_entries"] >= MIN_REPORT_ENTRIES
    if "avg_rssi" in df.columns:
        mask &= df["avg_rssi"] >= MIN_AVG_RSSI_DBM

    out = df.loc[mask].copy()
    if REMOVE_OUTLIERS and len(out) >= 20:
        lo = out["distance_m_used"].quantile(OUTLIER_Q_LOW)
        hi = out["distance_m_used"].quantile(OUTLIER_Q_HIGH)
        out = out[(out["distance_m_used"] >= lo) & (out["distance_m_used"] <= hi)].copy()
    return out


def parse_space_numbers(x) -> np.ndarray:
    if not isinstance(x, str) or not x.strip():
        return np.array([], dtype=float)
    return np.fromstring(x, sep=" ", dtype=float)


def compute_csi_features(csi_df: pd.DataFrame) -> pd.DataFrame:
    """Compute mean CSI amplitude per packet. Malformed files return empty df."""
    if csi_df.empty:
        return pd.DataFrame()

    required = {"real_values", "imag_values"}
    if not required.issubset(set(csi_df.columns)):
        print(f"WARNING: CSI columns abnormal, skipped. Existing columns: {list(csi_df.columns)[:12]}")
        return pd.DataFrame()

    csi_df = add_relative_time(csi_df)
    if len(csi_df) > MAX_CSI_ROWS_FOR_AMPLITUDE:
        idx = np.linspace(0, len(csi_df) - 1, MAX_CSI_ROWS_FOR_AMPLITUDE).astype(int)
        csi_df = csi_df.iloc[idx].copy()

    amps: List[float] = []
    for re_s, im_s in zip(csi_df["real_values"], csi_df["imag_values"]):
        re_vals = parse_space_numbers(re_s)
        im_vals = parse_space_numbers(im_s)
        n = min(len(re_vals), len(im_vals))
        if n == 0:
            amps.append(np.nan)
            continue
        amp = np.sqrt(re_vals[:n] ** 2 + im_vals[:n] ** 2)
        amp = amp[np.isfinite(amp) & (amp > 0)]
        amps.append(float(np.mean(amp)) if len(amp) else np.nan)
    csi_df["mean_csi_amp"] = amps

    if "csi_rssi" in csi_df.columns:
        csi_df["csi_rssi"] = pd.to_numeric(csi_df["csi_rssi"], errors="coerce")

    return csi_df


# =========================
# Plot functions
# =========================
def savefig(path: Path) -> None:
    plt.tight_layout()
    plt.savefig(path, dpi=FIG_DPI)
    plt.close()


def plot_rtt_timeseries(df: pd.DataFrame, actual_m: Optional[float], out_png: Path, title: str) -> None:
    plt.figure(figsize=(10, 4.5))
    plt.plot(df["t_rel_s"], df["distance_m_used"], linewidth=1.2, label="RTT measured distance")
    if actual_m is not None:
        plt.axhline(actual_m, linestyle="--", linewidth=1.2, label=f"Actual = {actual_m:g} m")
    plt.xlabel("Time (s)")
    plt.ylabel("Distance (m)")
    plt.title(title)
    plt.grid(True, alpha=0.3)
    plt.legend()
    savefig(out_png)


def plot_rssi_vs_distance(df: pd.DataFrame, actual_m: Optional[float], out_png: Path, title: str) -> None:
    if "avg_rssi" not in df.columns:
        return
    plt.figure(figsize=(7, 5))
    plt.scatter(df["avg_rssi"], df["distance_m_used"], s=18, alpha=0.65)
    if actual_m is not None:
        plt.axhline(actual_m, linestyle="--", linewidth=1.2, label=f"Actual = {actual_m:g} m")
        plt.legend()
    plt.xlabel("Average RSSI (dBm)")
    plt.ylabel("Measured distance (m)")
    plt.title(title)
    plt.grid(True, alpha=0.3)
    savefig(out_png)


def plot_csi(csi_df: pd.DataFrame, out_dir: Path, title: str) -> None:
    csi_feat = compute_csi_features(csi_df)
    if csi_feat.empty:
        return

    if "csi_rssi" in csi_feat.columns and csi_feat["csi_rssi"].notna().any():
        plt.figure(figsize=(10, 4))
        plt.plot(csi_feat["t_rel_s"], csi_feat["csi_rssi"], linewidth=1.0)
        plt.xlabel("Time (s)")
        plt.ylabel("CSI RSSI (dBm)")
        plt.title(title + " - CSI RSSI")
        plt.grid(True, alpha=0.3)
        savefig(out_dir / "csi_rssi_timeseries.png")

    if csi_feat["mean_csi_amp"].notna().any():
        plt.figure(figsize=(10, 4))
        plt.plot(csi_feat["t_rel_s"], csi_feat["mean_csi_amp"], linewidth=1.0)
        plt.xlabel("Time (s)")
        plt.ylabel("Mean CSI amplitude")
        plt.title(title + " - mean CSI amplitude")
        plt.grid(True, alpha=0.3)
        savefig(out_dir / "csi_mean_amplitude_timeseries.png")


def summarize_rtt(df: pd.DataFrame, actual_m: Optional[float], file: Path) -> Dict[str, object]:
    mean_m = float(df["distance_m_used"].mean()) if len(df) else np.nan
    err = mean_m - actual_m if actual_m is not None and not math.isnan(mean_m) else np.nan
    duration = float(df["t_rel_s"].max() - df["t_rel_s"].min()) if len(df) >= 2 else np.nan
    return {
        "actual_distance_m": actual_m,
        "file": str(file),
        "n_valid_rtt": int(len(df)),
        "duration_s": duration,
        "rtt_rate_hz": float(len(df) / duration) if duration and duration > 0 else np.nan,
        "mean_measured_m": mean_m,
        "median_measured_m": float(df["distance_m_used"].median()) if len(df) else np.nan,
        "std_m": float(df["distance_m_used"].std(ddof=1)) if len(df) > 1 else np.nan,
        "var_m2": float(df["distance_m_used"].var(ddof=1)) if len(df) > 1 else np.nan,
        "min_m": float(df["distance_m_used"].min()) if len(df) else np.nan,
        "max_m": float(df["distance_m_used"].max()) if len(df) else np.nan,
        "mean_error_m": err,
        "abs_mean_error_m": abs(err) if pd.notna(err) else np.nan,
        "rmse_to_actual_m": float(np.sqrt(np.mean((df["distance_m_used"] - actual_m) ** 2))) if actual_m is not None and len(df) else np.nan,
        "mean_rssi_dbm": float(df["avg_rssi"].mean()) if "avg_rssi" in df.columns and len(df) else np.nan,
        "mean_report_entries": float(df["report_entries"].mean()) if "report_entries" in df.columns and len(df) else np.nan,
    }


def plot_overall(summary_by_distance: pd.DataFrame, all_rtt: pd.DataFrame, fig_dir: Path) -> None:
    s = summary_by_distance.sort_values("actual_distance_m")

    plt.figure(figsize=(8, 5))
    plt.errorbar(
        s["actual_distance_m"],
        s["mean_measured_m"],
        yerr=s["std_m"],
        marker="o",
        capsize=4,
        linewidth=1.5,
        label="Mean RTT distance ± std",
    )
    min_v = min(s["actual_distance_m"].min(), s["mean_measured_m"].min())
    max_v = max(s["actual_distance_m"].max(), s["mean_measured_m"].max())
    plt.plot([min_v, max_v], [min_v, max_v], linestyle="--", linewidth=1.2, label="Ideal y=x")
    plt.xlabel("Actual distance (m)")
    plt.ylabel("Measured RTT distance (m)")
    plt.title("RTT ranging result by actual distance")
    plt.grid(True, alpha=0.3)
    plt.legend()
    savefig(fig_dir / "overall_actual_vs_measured_mean_std.png")

    plt.figure(figsize=(8, 5))
    plt.plot(s["actual_distance_m"], s["mean_error_m"], marker="o", linewidth=1.5)
    plt.axhline(0, linestyle="--", linewidth=1.2)
    plt.xlabel("Actual distance (m)")
    plt.ylabel("Mean error (m)")
    plt.title("Mean RTT error by distance")
    plt.grid(True, alpha=0.3)
    savefig(fig_dir / "overall_mean_error_by_distance.png")

    if not all_rtt.empty:
        order = sorted(all_rtt["actual_distance_m"].dropna().unique())
        data = [all_rtt.loc[all_rtt["actual_distance_m"] == d, "distance_m_used"].dropna().values for d in order]
        if data:
            plt.figure(figsize=(10, 5))
            plt.boxplot(data, labels=[f"{d:g}m" for d in order], showfliers=False)
            plt.xlabel("Actual distance")
            plt.ylabel("Measured RTT distance (m)")
            plt.title("RTT distance distribution by actual distance")
            plt.grid(True, axis="y", alpha=0.3)
            savefig(fig_dir / "overall_rtt_boxplot_by_distance.png")


# =========================
# Main
# =========================
def main() -> None:
    data_dir = DATA_DIR
    out_dir = OUT_DIR
    fig_dir = out_dir / "figures"
    mkdir(out_dir)
    mkdir(fig_dir)

    rtt_files = sorted(data_dir.glob("**/*_rtt_raw.csv"))
    print(f"Found {len(rtt_files)} RTT file(s).")
    if not rtt_files:
        print(f"No RTT files found under: {data_dir}")
        return

    summaries: List[Dict[str, object]] = []
    all_rtt_list: List[pd.DataFrame] = []

    for rtt_file in rtt_files:
        actual_m = infer_actual_distance_m(rtt_file)
        label = f"{actual_m:g}m" if actual_m is not None else rtt_file.parent.name
        run_out = fig_dir / label
        mkdir(run_out)

        print(f"\nProcessing RTT: {rtt_file}")
        raw_rtt = read_csv_safe(rtt_file)
        rtt = filter_rtt(raw_rtt)
        if rtt.empty:
            print("WARNING: no valid RTT rows after filtering. Try lowering filters.")
            continue

        rtt["actual_distance_m"] = actual_m
        rtt["source_file"] = str(rtt_file)
        all_rtt_list.append(rtt)
        summaries.append(summarize_rtt(rtt, actual_m, rtt_file))

        plot_rtt_timeseries(rtt, actual_m, run_out / "rtt_distance_timeseries.png", f"RTT distance ({label})")
        plot_rssi_vs_distance(rtt, actual_m, run_out / "rtt_rssi_vs_distance.png", f"RTT distance vs RSSI ({label})")

        if PROCESS_CSI:
            csi_files = sorted(rtt_file.parent.glob("*_csi_packet_raw.csv"))
            if csi_files:
                csi_file = csi_files[0]
                print(f"Processing CSI: {csi_file}")
                csi_df = read_csv_safe(csi_file)
                try:
                    plot_csi(csi_df, run_out, f"CSI features ({label})")
                except Exception as e:
                    print(f"WARNING: CSI plotting failed for {csi_file}, skipped. Reason: {e}")

    if not summaries:
        print("No valid RTT data found. Nothing to summarize.")
        return

    summary_by_file = pd.DataFrame(summaries).sort_values("actual_distance_m")
    summary_by_file.to_csv(out_dir / "summary_by_file.csv", index=False, encoding="utf-8-sig")

    all_rtt = pd.concat(all_rtt_list, ignore_index=True) if all_rtt_list else pd.DataFrame()
    all_rtt.to_csv(out_dir / "all_rtt_filtered.csv", index=False, encoding="utf-8-sig")

    grouped = summary_by_file.groupby("actual_distance_m", dropna=False)
    summary_by_distance = grouped.agg(
        n_files=("file", "count"),
        n_valid_rtt=("n_valid_rtt", "sum"),
        mean_measured_m=("mean_measured_m", "mean"),
        median_measured_m=("median_measured_m", "mean"),
        std_m=("std_m", "mean"),
        var_m2=("var_m2", "mean"),
        mean_error_m=("mean_error_m", "mean"),
        abs_mean_error_m=("abs_mean_error_m", "mean"),
        rmse_to_actual_m=("rmse_to_actual_m", "mean"),
        mean_rssi_dbm=("mean_rssi_dbm", "mean"),
        mean_report_entries=("mean_report_entries", "mean"),
    ).reset_index()
    summary_by_distance.to_csv(out_dir / "summary_by_distance.csv", index=False, encoding="utf-8-sig")

    plot_overall(summary_by_distance.dropna(subset=["actual_distance_m"]), all_rtt, fig_dir)

    print("\nDone.")
    print(f"Output folder: {out_dir}")
    print(f"Important figure: {fig_dir / 'overall_actual_vs_measured_mean_std.png'}")
    print(f"Summary table: {out_dir / 'summary_by_distance.csv'}")


if __name__ == "__main__":
    main()
