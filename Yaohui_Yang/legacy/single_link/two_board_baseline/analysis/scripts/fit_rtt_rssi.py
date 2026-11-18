import pandas as pd
import numpy as np
from pathlib import Path
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

# ===== 1. 你的数据文件 =====
files = {
    "1m": 100,
    "2m": 200,
    "3m": 300,
    "5m": 500,
    "6m": 600,
    "7m": 700,
    "8m": 800,
    "9m": 900,
    "10m": 1000,
}

# 如果你有 4m.csv，也加进去
# files["4m"] = 400

PROJECT_ROOT = Path(__file__).resolve().parents[2]
data_dir = PROJECT_ROOT / "data" / "legacy_group1"

all_df = []

# ===== 2. 读入所有 CSV =====
for fname, true_cm in files.items():
    path = data_dir / f"{fname}.csv"
    if not path.exists():
        print(f"Skip missing file: {path}")
        continue

    df = pd.read_csv(path)

    # 兼容你现在的字段
    required_cols = ["status", "rtt_raw_ns", "avg_rssi", "report_entries"]
    for col in required_cols:
        if col not in df.columns:
            raise ValueError(f"{path} 缺少列: {col}")

    # ===== 3. 先做基础筛选 =====
    df = df[df["status"] == 0].copy()
    df = df[df["report_entries"] >= 8].copy()
    df = df[df["avg_rssi"] > -95].copy()

    # 去掉明显异常值
    df = df[df["rtt_raw_ns"] > 0].copy()

    df["true_cm"] = true_cm
    df["file"] = fname

    all_df.append(df)

if not all_df:
    raise RuntimeError("没有有效数据，请检查 CSV 文件路径和列名。")

data = pd.concat(all_df, ignore_index=True)

print("总样本数:", len(data))
print(data[["file", "true_cm", "rtt_raw_ns", "avg_rssi", "report_entries"]].head())

# ===== 4. 做线性拟合 =====
# 模型: true_cm = a * rtt_raw_ns + b * avg_rssi + c
X = data[["rtt_raw_ns", "avg_rssi"]].values
y = data["true_cm"].values

model = LinearRegression()
model.fit(X, y)

a = model.coef_[0]
b = model.coef_[1]
c = model.intercept_

pred = model.predict(X)

mae = mean_absolute_error(y, pred)
rmse = np.sqrt(mean_squared_error(y, pred))
r2 = r2_score(y, pred)

print("\n===== 拟合结果 =====")
print(f"a (rtt_raw_ns 系数) = {a:.6f}")
print(f"b (avg_rssi 系数)   = {b:.6f}")
print(f"c (截距)            = {c:.6f}")

print("\n===== 拟合误差 =====")
print(f"MAE  = {mae:.3f} cm")
print(f"RMSE = {rmse:.3f} cm")
print(f"R²   = {r2:.4f}")

# ===== 5. 看每个距离的平均效果 =====
data["pred_cm"] = pred
summary = data.groupby("file").agg(
    true_cm=("true_cm", "first"),
    samples=("true_cm", "count"),
    mean_rtt=("rtt_raw_ns", "mean"),
    mean_rssi=("avg_rssi", "mean"),
    pred_mean=("pred_cm", "mean"),
)
summary["abs_err_cm"] = (summary["pred_mean"] - summary["true_cm"]).abs()

print("\n===== 分距离结果 =====")
print(summary)

# ===== 6. 生成可以直接贴进 C 代码的片段 =====
print("\n===== 直接贴进 C 的系数 =====")
print(f"const float A = {a:.6f}f;")
print(f"const float B = {b:.6f}f;")
print(f"const float C = {c:.6f}f;")
