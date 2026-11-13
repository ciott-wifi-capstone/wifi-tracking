import os
import re
import glob
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec

try:
    from sklearn.linear_model import LinearRegression
    from sklearn.ensemble import RandomForestRegressor
    SKLEARN_OK = True
except Exception:
    SKLEARN_OK = False

# =========================
# User settings
# =========================
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = str(PROJECT_ROOT / "data" / "distance_campaign")
OUT_DIR = str(PROJECT_ROOT / "results" / "onepage_summary")
MIN_REPORT_ENTRIES = 1      # set 8 if you want strict filtering
MIN_AVG_RSSI_DBM = -95
USE_STATUS_FILTER = True
RF_N_ESTIMATORS = 200
RF_RANDOM_STATE = 42

# Which RTT field should be treated as the uncalibrated ranging result?
# Usually dist_raw_cm is the best choice for "Raw RTT".
RAW_DISTANCE_CANDIDATES = ["dist_raw_cm", "dist_est_cm", "dist_filt_cm", "dist_cal_cm"]


def find_distance_from_folder(path: str):
    name = Path(path).name
    m = re.search(r"(\d+)\s*m", name, flags=re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def find_experiments(data_root: str):
    exp_list = []
    for sub in sorted(glob.glob(os.path.join(data_root, "*m"))):
        if not os.path.isdir(sub):
            continue
        d_m = find_distance_from_folder(sub)
        if d_m is None:
            continue
        rtt_files = sorted(glob.glob(os.path.join(sub, "*_rtt_raw.csv")))
        csi_files = sorted(glob.glob(os.path.join(sub, "*_csi_packet_raw.csv")))
        if not rtt_files:
            continue
        exp_list.append({
            "distance_m": d_m,
            "distance_cm": d_m * 100,
            "folder": sub,
            "rtt_file": rtt_files[0],
            "csi_file": csi_files[0] if csi_files else None,
        })
    return exp_list


def choose_raw_distance_col(df: pd.DataFrame):
    for col in RAW_DISTANCE_CANDIDATES:
        if col in df.columns:
            return col
    return None


def load_rtt(rtt_file: str, true_cm: int):
    df = pd.read_csv(rtt_file)
    df.columns = [str(c).strip() for c in df.columns]

    for col in ["cycle_id", "status", "avg_rssi", "report_entries"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    raw_col = choose_raw_distance_col(df)
    if raw_col is None:
        raise ValueError(f"No usable RTT distance column found in {rtt_file}")

    df[raw_col] = pd.to_numeric(df[raw_col], errors="coerce")
    out = df.copy()
    out["raw_measured_cm"] = out[raw_col]
    out["true_cm"] = true_cm

    mask = pd.Series(True, index=out.index)
    if USE_STATUS_FILTER and "status" in out.columns:
        mask &= (out["status"] == 0)
    if "report_entries" in out.columns:
        mask &= (out["report_entries"].fillna(0) >= MIN_REPORT_ENTRIES)
    if "avg_rssi" in out.columns:
        mask &= (out["avg_rssi"].fillna(-999) >= MIN_AVG_RSSI_DBM)
    mask &= out["raw_measured_cm"].notna() & (out["raw_measured_cm"] > 0)

    cols_keep = [c for c in ["cycle_id", "avg_rssi", "raw_measured_cm", "true_cm"] if c in out.columns]
    out = out.loc[mask, cols_keep].copy()
    return out


def _parse_num_list(s):
    if pd.isna(s):
        return []
    try:
        return [float(x) for x in str(s).strip().split() if x != ""]
    except Exception:
        return []


def csi_features_from_row(real_values, imag_values):
    reals = _parse_num_list(real_values)
    imags = _parse_num_list(imag_values)
    n = min(len(reals), len(imags))
    if n == 0:
        return np.nan, np.nan, np.nan
    reals = np.asarray(reals[:n], dtype=float)
    imags = np.asarray(imags[:n], dtype=float)
    amp = np.sqrt(reals**2 + imags**2)
    return float(np.mean(amp)), float(np.std(amp)), float(np.median(amp))


def load_csi(csi_file: str):
    if csi_file is None or not os.path.exists(csi_file):
        return None
    df = pd.read_csv(csi_file)
    df.columns = [str(c).strip() for c in df.columns]

    # Need cycle_id + real/imag values for merging and feature extraction
    if "cycle_id" not in df.columns or "real_values" not in df.columns or "imag_values" not in df.columns:
        warnings.warn(f"CSI file missing expected columns, skipped: {csi_file}")
        return None

    df["cycle_id"] = pd.to_numeric(df["cycle_id"], errors="coerce")
    feats = df.apply(lambda row: csi_features_from_row(row["real_values"], row["imag_values"]), axis=1)
    feat_df = pd.DataFrame(feats.tolist(), columns=["csi_mean_amp", "csi_std_amp", "csi_median_amp"])
    out = pd.concat([df[["cycle_id"]].reset_index(drop=True), feat_df], axis=1)
    out = out.dropna(subset=["cycle_id"]).copy()
    return out


def build_full_dataset(exp_list):
    parts = []
    for exp in exp_list:
        print(f"Loading RTT: {exp['rtt_file']}")
        rtt_df = load_rtt(exp["rtt_file"], exp["distance_cm"])
        csi_df = load_csi(exp["csi_file"])
        if csi_df is not None and "cycle_id" in rtt_df.columns:
            merged = pd.merge(rtt_df, csi_df, on="cycle_id", how="left")
        else:
            merged = rtt_df.copy()
            merged["csi_mean_amp"] = np.nan
            merged["csi_std_amp"] = np.nan
            merged["csi_median_amp"] = np.nan
        merged["distance_m"] = exp["distance_m"]
        parts.append(merged)
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts, ignore_index=True)
    return df


def fit_predict_linear(train_X, train_y, test_X):
    model = LinearRegression()
    model.fit(train_X, train_y)
    return model.predict(test_X)


def fit_predict_rf(train_X, train_y, test_X):
    model = RandomForestRegressor(
        n_estimators=RF_N_ESTIMATORS,
        random_state=RF_RANDOM_STATE,
        min_samples_leaf=3,
        n_jobs=-1,
    )
    model.fit(train_X, train_y)
    return model.predict(test_X)


def strict_leave_one_distance_out(df):
    if df.empty:
        raise ValueError("No data loaded.")

    if not SKLEARN_OK:
        raise ImportError("scikit-learn is required. Please install it with: pip install scikit-learn")

    all_preds = []
    mae_rows = []
    distances = sorted(df["true_cm"].dropna().unique())

    for d in distances:
        train = df[df["true_cm"] != d].copy()
        test = df[df["true_cm"] == d].copy()
        if len(train) == 0 or len(test) == 0:
            continue

        # Raw RTT
        test["pred_raw_rtt"] = test["raw_measured_cm"]

        # RTT linear calibration
        pred = fit_predict_linear(train[["raw_measured_cm"]], train["true_cm"], test[["raw_measured_cm"]])
        test["pred_rtt_linear"] = pred

        # RTT + RSSI linear
        if "avg_rssi" in train.columns and train["avg_rssi"].notna().any() and test["avg_rssi"].notna().any():
            train_lr = train.dropna(subset=["raw_measured_cm", "avg_rssi", "true_cm"])
            test_lr = test.dropna(subset=["raw_measured_cm", "avg_rssi"])
            test["pred_rtt_rssi_linear"] = np.nan
            if len(train_lr) > 1 and len(test_lr) > 0:
                pred = fit_predict_linear(
                    train_lr[["raw_measured_cm", "avg_rssi"]],
                    train_lr["true_cm"],
                    test_lr[["raw_measured_cm", "avg_rssi"]],
                )
                test.loc[test_lr.index, "pred_rtt_rssi_linear"] = pred
        else:
            test["pred_rtt_rssi_linear"] = np.nan

        # RTT + RSSI + CSI RF
        rf_features = ["raw_measured_cm", "avg_rssi", "csi_mean_amp", "csi_std_amp", "csi_median_amp"]
        train_rf = train.dropna(subset=rf_features + ["true_cm"])
        test_rf = test.dropna(subset=rf_features)
        test["pred_rtt_rssi_csi_rf"] = np.nan
        if len(train_rf) > 5 and len(test_rf) > 0:
            pred = fit_predict_rf(train_rf[rf_features], train_rf["true_cm"], test_rf[rf_features])
            test.loc[test_rf.index, "pred_rtt_rssi_csi_rf"] = pred

        # MAE for this left-out distance
        for name, col in [
            ("Raw RTT", "pred_raw_rtt"),
            ("RTT linear calibration", "pred_rtt_linear"),
            ("RTT + RSSI linear", "pred_rtt_rssi_linear"),
            ("RTT + RSSI + CSI RF", "pred_rtt_rssi_csi_rf"),
        ]:
            err = (test[col] - test["true_cm"]).abs().dropna()
            mae = float(err.mean()) if len(err) else np.nan
            mae_rows.append({"left_out_distance_cm": d, "left_out_distance_m": d / 100.0, "method": name, "mae_cm": mae})

        all_preds.append(test)

    pred_df = pd.concat(all_preds, ignore_index=True)
    mae_df = pd.DataFrame(mae_rows)
    return pred_df, mae_df


def build_summary_tables(pred_df, mae_df):
    plot_methods = [
        ("Raw RTT", "pred_raw_rtt"),
        ("RTT linear calibration", "pred_rtt_linear"),
        ("RTT + RSSI linear", "pred_rtt_rssi_linear"),
        ("RTT + RSSI + CSI RF", "pred_rtt_rssi_csi_rf"),
    ]

    rows = []
    for true_cm in sorted(pred_df["true_cm"].dropna().unique()):
        sub = pred_df[pred_df["true_cm"] == true_cm]
        row = {"true_cm": true_cm, "true_m": true_cm / 100.0}
        for label, col in plot_methods:
            vals = sub[col].dropna()
            row[f"{label}_mean_cm"] = vals.mean() if len(vals) else np.nan
            row[f"{label}_std_cm"] = vals.std(ddof=1) if len(vals) > 1 else 0.0 if len(vals) == 1 else np.nan
        rows.append(row)
    summary_df = pd.DataFrame(rows)

    mae_bar = mae_df.groupby("method", as_index=False)["mae_cm"].mean().rename(columns={"mae_cm": "mean_mae_cm"})
    return summary_df, mae_bar


def make_onepage_figure(summary_df, mae_df, mae_bar, out_png):
    plt.rcParams.update({
        "font.size": 11,
        "axes.titlesize": 15,
        "axes.labelsize": 12,
        "legend.fontsize": 10,
    })

    fig = plt.figure(figsize=(15, 10), dpi=200)
    gs = GridSpec(2, 2, width_ratios=[1.6, 1.0], height_ratios=[1, 1], figure=fig)
    ax_left = fig.add_subplot(gs[:, 0])
    ax_rt = fig.add_subplot(gs[0, 1])
    ax_rb = fig.add_subplot(gs[1, 1])

    x = summary_df["true_cm"].values

    # Left panel
    max_xy = max(np.nanmax(x) if len(x) else 0, np.nanmax(summary_df.filter(like="_mean_cm").values))
    ideal = np.linspace(0, max_xy * 1.05, 200)
    ax_left.plot(ideal, ideal, "k--", linewidth=1.6, label="Ideal: predicted = true")

    styles = [
        ("Raw RTT", "predicted", "Raw RTT_mean_cm", "Raw RTT_std_cm"),
        ("RTT linear calibration", "predicted", "RTT linear calibration_mean_cm", "RTT linear calibration_std_cm"),
        ("RTT + RSSI linear", "predicted", "RTT + RSSI linear_mean_cm", "RTT + RSSI linear_std_cm"),
        ("RTT + RSSI + CSI RF", "predicted", "RTT + RSSI + CSI RF_mean_cm", "RTT + RSSI + CSI RF_std_cm"),
    ]

    for label, _, mean_col, std_col in styles:
        if mean_col not in summary_df.columns:
            continue
        y = summary_df[mean_col].values
        yerr = summary_df[std_col].values if std_col in summary_df.columns else None
        ax_left.errorbar(x, y, yerr=yerr, marker="o", capsize=3, linewidth=1.5, label=label)

    ax_left.set_title("Ranging result before embedding calibration into ESP code")
    ax_left.set_xlabel("True distance (cm)")
    ax_left.set_ylabel("Predicted / measured distance (cm)")
    ax_left.grid(True, alpha=0.3)
    ax_left.legend(loc="upper left")

    # Right-top bar
    order = ["Raw RTT", "RTT linear calibration", "RTT + RSSI linear", "RTT + RSSI + CSI RF"]
    mae_bar = mae_bar.set_index("method").reindex(order).reset_index()
    bars = ax_rt.bar(mae_bar["method"], mae_bar["mean_mae_cm"])
    ax_rt.set_title("Strict leave-one-distance-out MAE")
    ax_rt.set_ylabel("Mean MAE (cm)")
    ax_rt.grid(True, axis="y", alpha=0.3)
    ax_rt.set_ylim(0, max(10, np.nanmax(mae_bar["mean_mae_cm"]) * 1.18))
    ax_rt.tick_params(axis="x", rotation=20)
    for bar, val in zip(bars, mae_bar["mean_mae_cm"]):
        if pd.notna(val):
            ax_rt.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 5, f"{val:.1f}", ha="center", va="bottom")

    # Right-bottom line
    for method in order:
        sub = mae_df[mae_df["method"] == method].sort_values("left_out_distance_m")
        ax_rb.plot(sub["left_out_distance_m"], sub["mae_cm"], marker="o", linewidth=1.3, label=method)
    ax_rb.set_title("Error at each unseen distance")
    ax_rb.set_xlabel("Left-out test distance (m)")
    ax_rb.set_ylabel("MAE (cm)")
    ax_rb.grid(True, alpha=0.3)
    ax_rb.legend(loc="upper left", fontsize=9)
    ax_rb.set_xticks(sorted(mae_df["left_out_distance_m"].dropna().unique()))

    fig.text(
        0.5,
        0.02,
        "Data: RTT/CSI experiment across all available distances; each test distance is excluded from training during validation.",
        ha="center",
        fontsize=11,
    )
    fig.tight_layout(rect=[0.02, 0.05, 0.98, 0.98])
    fig.savefig(out_png, bbox_inches="tight")
    plt.close(fig)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    exp_list = find_experiments(DATA_ROOT)
    if not exp_list:
        raise FileNotFoundError(f"No experiment folders found under: {DATA_ROOT}")

    print(f"Found {len(exp_list)} experiment distance folder(s).")
    df = build_full_dataset(exp_list)
    if df.empty:
        raise ValueError("No usable merged data could be built.")

    print(f"Total usable samples: {len(df)}")
    pred_df, mae_df = strict_leave_one_distance_out(df)
    summary_df, mae_bar = build_summary_tables(pred_df, mae_df)

    out_png = os.path.join(OUT_DIR, "onepage_summary.png")
    make_onepage_figure(summary_df, mae_df, mae_bar, out_png)

    out_xlsx = os.path.join(OUT_DIR, "onepage_summary_tables.xlsx")
    with pd.ExcelWriter(out_xlsx, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="merged_samples", index=False)
        pred_df.to_excel(writer, sheet_name="predictions", index=False)
        summary_df.to_excel(writer, sheet_name="summary_by_distance", index=False)
        mae_df.to_excel(writer, sheet_name="mae_by_distance", index=False)
        mae_bar.to_excel(writer, sheet_name="mean_mae", index=False)

    out_csv = os.path.join(OUT_DIR, "summary_by_distance.csv")
    summary_df.to_csv(out_csv, index=False)

    print("Done.")
    print(f"Figure saved to: {out_png}")
    print(f"Excel saved to:  {out_xlsx}")
    print(f"CSV saved to:    {out_csv}")


if __name__ == "__main__":
    main()
