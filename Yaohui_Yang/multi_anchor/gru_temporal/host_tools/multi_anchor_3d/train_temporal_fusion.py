from __future__ import annotations

import csv
import math
import random
from collections import defaultdict
from pathlib import Path

import numpy as np
import torch
from torch.utils.data import Dataset, DataLoader

from temporal_fusion_model import (
    TemporalFusionGRU,
    FEATURE_NAMES,
    ANCHOR_IDS,
)


# =========================================================
# 1. 基本设置
# =========================================================

HERE = Path(__file__).resolve().parent

# 你的历史实验数据目录
ROOT = HERE.parents[1] / "results" / "multi_anchor_3d_live"

# 最后训练得到的新模型
CHECKPOINT_PATH = HERE / "temporal_fusion.pt"


SEED = 20260909

# 每次让 GRU 看最近 20 个同一 Anchor 的测量
SEQ_LEN = 20

# 每隔 3 个样本取一个窗口，避免相邻样本过度重复
TRAIN_STRIDE = 3
VALID_STRIDE = 3

BATCH_SIZE = 256

EPOCHS = 80

LEARNING_RATE = 1e-3

# 如果验证集连续若干轮不提升，就停止训练
PATIENCE = 12

# 如果同一 Anchor 两次数据间隔超过这个值，
# 就认为发生了中断，不允许一个 sequence 跨越这个间隔
MAX_TIME_GAP_S = 1.2


# =========================================================
# 2. 验证集设置
#
# 这些数据完全不参与训练
# 用来判断模型是否真的能泛化
# =========================================================

HOLD_OUT_POINTS = {
    "moving|moving_slow",
    "A5|static",
}


ANCHOR_TO_INDEX = {
    aid: i
    for i, aid in enumerate(ANCHOR_IDS)
}


# =========================================================
# 3. 工具函数
# =========================================================

def safe_float(row, name, default=0.0):
    """
    从 CSV 中安全读取浮点数。
    如果数据为空或者非法，则返回 default。
    """

    try:
        value = float(row.get(name, default) or default)

        if math.isfinite(value):
            return value

        return default

    except (TypeError, ValueError):
        return default


# =========================================================
# 4. 读取带真实距离的数据
# =========================================================

def load_labeled_rows():

    rows = []

    if not ROOT.exists():
        raise FileNotFoundError(
            f"找不到历史数据目录: {ROOT}"
        )

    # 遍历：
    #
    # results/multi_anchor_3d_live/
    #     session...
    #     旧：较快移动/
    #     旧：较慢移动/
    #     旧：静态/
    #
    for session_dir in sorted(
        d for d in ROOT.iterdir()
        if d.is_dir()
    ):

        csv_path = session_dir / "link_measurements.csv"

        if not csv_path.exists():
            continue

        print("读取:", csv_path)

        with csv_path.open(
            "r",
            encoding="utf-8-sig",
            newline=""
        ) as f:

            reader = csv.DictReader(f)

            for row in reader:

                # -----------------------------------------
                # 必须有真实距离
                # -----------------------------------------

                try:
                    truth = float(
                        row["true_distance_m"]
                    )

                    baseline = float(
                        row["baseline_distance_m"]
                    )

                    anchor = int(
                        row["anchor"]
                    )

                    timestamp = float(
                        row["pc_time_s"]
                    )

                except (
                    KeyError,
                    TypeError,
                    ValueError,
                ):
                    continue


                if not math.isfinite(truth):
                    continue

                if not math.isfinite(baseline):
                    continue

                if baseline <= 0:
                    continue

                if anchor not in ANCHOR_TO_INDEX:
                    continue


                # -----------------------------------------
                # 当前数据属于什么实验
                # -----------------------------------------

                point_id = (
                    row.get("point_id")
                    or "unknown"
                )

                condition = (
                    row.get("condition")
                    or ""
                )

                point_key = (
                    f"{point_id}|{condition}"
                )


                # -----------------------------------------
                # 构造 FTM + CSI 特征
                #
                # 必须和 temporal_fusion_model.py
                # 中 FEATURE_NAMES 的顺序完全一致
                # -----------------------------------------

                features = np.asarray(
                    [
                        safe_float(row, name)
                        for name in FEATURE_NAMES
                    ],
                    dtype=np.float32,
                )


                # -----------------------------------------
                # 新模型的目标：
                #
                # delta =
                # true_distance - FTM_distance
                # -----------------------------------------

                delta = truth - baseline


                rows.append(
                    {
                        "session": session_dir.name,

                        "point": point_key,

                        "anchor": anchor,

                        "timestamp": timestamp,

                        "features": features,

                        "baseline": baseline,

                        "truth": truth,

                        "delta": delta,
                    }
                )

    return rows


# =========================================================
# 5. 分训练集和验证集
# =========================================================

def split_rows(rows):

    train_rows = []

    valid_rows = []

    for row in rows:

        if row["point"] in HOLD_OUT_POINTS:
            valid_rows.append(row)

        else:
            train_rows.append(row)

    return train_rows, valid_rows


# =========================================================
# 6. 把单帧数据变成时间序列
#
# 例如同一个 Anchor：
#
# t1
# t2
# ...
# t20
#
# -> 一个 GRU 输入样本
# =========================================================

def make_sequences(rows, stride):

    groups = defaultdict(list)

    # -----------------------------------------------------
    # 同一个：
    #
    # Session
    # Point
    # Anchor
    #
    # 才能组成一个时间序列
    # -----------------------------------------------------

    for row in rows:

        key = (
            row["session"],
            row["point"],
            row["anchor"],
        )

        groups[key].append(row)


    samples = []


    for (
        session,
        point,
        anchor,
    ), group in groups.items():

        # 时间排序
        group.sort(
            key=lambda r: r["timestamp"]
        )


        # -------------------------------------------------
        # 防止数据记录中断时，
        # sequence 跨越很长时间
        # -------------------------------------------------

        segments = []

        current = []

        last_time = None


        for row in group:

            if last_time is not None:

                time_gap = (
                    row["timestamp"]
                    - last_time
                )

                if time_gap > MAX_TIME_GAP_S:

                    if current:
                        segments.append(current)

                    current = []


            current.append(row)

            last_time = row["timestamp"]


        if current:
            segments.append(current)


        # -------------------------------------------------
        # 每一段建立滑动窗口
        # -------------------------------------------------

        for segment in segments:

            if len(segment) < SEQ_LEN:
                continue


            for end in range(
                SEQ_LEN - 1,
                len(segment),
                stride,
            ):

                start = (
                    end
                    - SEQ_LEN
                    + 1
                )

                window = segment[
                    start:end + 1
                ]


                x = np.stack(
                    [
                        row["features"]
                        for row in window
                    ]
                )


                # 预测最后一个时刻
                last_row = window[-1]


                samples.append(
                    {
                        "x": x,

                        "anchor_idx":
                            ANCHOR_TO_INDEX[
                                anchor
                            ],

                        "baseline":
                            last_row[
                                "baseline"
                            ],

                        "truth":
                            last_row[
                                "truth"
                            ],

                        "target_delta":
                            last_row[
                                "delta"
                            ],
                    }
                )


    return samples


# =========================================================
# 7. 计算特征标准化参数
# =========================================================

def compute_normalization(
    samples
):

    # 把所有时间帧放到一起
    x = np.concatenate(
        [
            sample["x"]
            for sample in samples
        ],
        axis=0,
    )


    mu = np.mean(
        x,
        axis=0,
    )


    std = np.std(
        x,
        axis=0,
    )


    # 防止除 0
    std[
        std < 1e-6
    ] = 1.0


    return (
        mu.astype(
            np.float32
        ),

        std.astype(
            np.float32
        ),
    )


# =========================================================
# 8. PyTorch Dataset
# =========================================================

class TemporalDataset(Dataset):

    def __init__(
        self,
        samples,
        mu,
        std,
    ):

        self.samples = samples

        self.mu = mu

        self.std = std


    def __len__(self):

        return len(
            self.samples
        )


    def __getitem__(
        self,
        index,
    ):

        sample = (
            self.samples[index]
        )


        x = (
            sample["x"]
            - self.mu
        ) / self.std


        return (
            torch.tensor(
                x,
                dtype=torch.float32,
            ),

            torch.tensor(
                sample[
                    "anchor_idx"
                ],
                dtype=torch.long,
            ),

            torch.tensor(
                sample[
                    "baseline"
                ],
                dtype=torch.float32,
            ),

            torch.tensor(
                sample[
                    "truth"
                ],
                dtype=torch.float32,
            ),

            torch.tensor(
                sample[
                    "target_delta"
                ],
                dtype=torch.float32,
            ),
        )


# =========================================================
# 9. 不确定度损失
#
# 网络不仅预测 delta，
# 还预测 sigma
# =========================================================

def gaussian_nll(
    error,
    sigma,
):

    sigma = torch.clamp(
        sigma,
        min=0.05,
        max=20.0,
    )


    loss = (
        0.5
        * (
            error
            / sigma
        ) ** 2
        + torch.log(
            sigma
        )
    )


    return torch.mean(
        loss
    )


# =========================================================
# 10. 验证模型
# =========================================================

@torch.no_grad()
def evaluate(
    model,
    loader,
    device,
):

    model.eval()


    corrected_errors = []

    raw_errors = []

    sigmas = []


    for (
        x,
        anchor_idx,
        baseline,
        truth,
        target_delta,
    ) in loader:


        x = x.to(
            device
        )

        anchor_idx = (
            anchor_idx.to(
                device
            )
        )

        baseline = baseline.to(
            device
        )

        truth = truth.to(
            device
        )


        output = model(
            x,
            anchor_idx,
        )


        corrected = (
            baseline
            + output["delta"]
        )


        corrected_error = (
            torch.abs(
                corrected
                - truth
            )
        )


        raw_error = (
            torch.abs(
                baseline
                - truth
            )
        )


        corrected_errors.append(
            corrected_error.cpu()
        )

        raw_errors.append(
            raw_error.cpu()
        )

        sigmas.append(
            output["sigma"].cpu()
        )


    corrected_errors = (
        torch.cat(
            corrected_errors
        ).numpy()
    )

    raw_errors = (
        torch.cat(
            raw_errors
        ).numpy()
    )

    sigmas = (
        torch.cat(
            sigmas
        ).numpy()
    )


    return {
        "mae":
            float(
                np.mean(
                    corrected_errors
                )
            ),

        "median":
            float(
                np.median(
                    corrected_errors
                )
            ),

        "p90":
            float(
                np.percentile(
                    corrected_errors,
                    90,
                )
            ),

        "raw_mae":
            float(
                np.mean(
                    raw_errors
                )
            ),

        "sigma_mean":
            float(
                np.mean(
                    sigmas
                )
            ),
    }


# =========================================================
# 11. 主训练函数
# =========================================================

def main():

    random.seed(
        SEED
    )

    np.random.seed(
        SEED
    )

    torch.manual_seed(
        SEED
    )


    # -----------------------------------------------------
    # CPU / GPU
    # -----------------------------------------------------

    device = torch.device(
        "cuda"
        if torch.cuda.is_available()
        else "cpu"
    )


    print()
    print("==============================")
    print("Temporal CSI + FTM Training")
    print("==============================")
    print()

    print(
        "Device:",
        device,
    )

    print(
        "Data folder:",
        ROOT,
    )


    # -----------------------------------------------------
    # 读取历史数据
    # -----------------------------------------------------

    rows = load_labeled_rows()


    if len(rows) == 0:

        raise RuntimeError(
            "没有找到带 true_distance_m 的训练数据"
        )


    print()
    print(
        "有效带标签数据:",
        len(rows),
    )


    # -----------------------------------------------------
    # Train / Validation
    # -----------------------------------------------------

    train_rows, valid_rows = (
        split_rows(
            rows
        )
    )


    print(
        "训练数据:",
        len(train_rows),
    )

    print(
        "验证数据:",
        len(valid_rows),
    )


    print()

    print(
        "训练场景:",
        sorted(
            {
                r["point"]
                for r in train_rows
            }
        ),
    )


    print(
        "验证场景:",
        sorted(
            {
                r["point"]
                for r in valid_rows
            }
        ),
    )


    # -----------------------------------------------------
    # 建 sequence
    # -----------------------------------------------------

    train_samples = (
        make_sequences(
            train_rows,
            TRAIN_STRIDE,
        )
    )


    valid_samples = (
        make_sequences(
            valid_rows,
            VALID_STRIDE,
        )
    )


    print()

    print(
        "Train sequences:",
        len(train_samples),
    )

    print(
        "Valid sequences:",
        len(valid_samples),
    )

    print(
        "Sequence length:",
        SEQ_LEN,
    )


    if (
        len(train_samples) == 0
        or
        len(valid_samples) == 0
    ):

        raise RuntimeError(
            "无法构建足够的时间序列"
        )


    # -----------------------------------------------------
    # 特征标准化
    # -----------------------------------------------------

    mu, std = (
        compute_normalization(
            train_samples
        )
    )


    print()

    print(
        "Feature mean:",
        mu,
    )

    print(
        "Feature std:",
        std,
    )


    # -----------------------------------------------------
    # Dataset
    # -----------------------------------------------------

    train_dataset = (
        TemporalDataset(
            train_samples,
            mu,
            std,
        )
    )


    valid_dataset = (
        TemporalDataset(
            valid_samples,
            mu,
            std,
        )
    )


    train_loader = (
        DataLoader(
            train_dataset,
            batch_size=BATCH_SIZE,
            shuffle=True,
        )
    )


    valid_loader = (
        DataLoader(
            valid_dataset,
            batch_size=BATCH_SIZE,
            shuffle=False,
        )
    )


    # -----------------------------------------------------
    # 建立新 GRU 模型
    # -----------------------------------------------------

    model = (
        TemporalFusionGRU()
        .to(
            device
        )
    )


    print()
    print(model)
    print()


    # -----------------------------------------------------
    # Optimizer
    # -----------------------------------------------------

    optimizer = (
        torch.optim.AdamW(
            model.parameters(),
            lr=LEARNING_RATE,
            weight_decay=1e-4,
        )
    )


    scheduler = (
        torch.optim.lr_scheduler
        .ReduceLROnPlateau(
            optimizer,
            mode="min",
            factor=0.5,
            patience=4,
        )
    )


    # -----------------------------------------------------
    # 开始训练
    # -----------------------------------------------------

    best_mae = float(
        "inf"
    )

    best_state = None

    no_improve = 0


    for epoch in range(
        1,
        EPOCHS + 1,
    ):


        model.train()


        total_loss = 0.0

        batch_count = 0


        for (
            x,
            anchor_idx,
            baseline,
            truth,
            target_delta,
        ) in train_loader:


            x = x.to(
                device
            )

            anchor_idx = (
                anchor_idx.to(
                    device
                )
            )

            baseline = (
                baseline.to(
                    device
                )
            )

            truth = (
                truth.to(
                    device
                )
            )

            target_delta = (
                target_delta.to(
                    device
                )
            )


            optimizer.zero_grad(
                set_to_none=True
            )


            output = model(
                x,
                anchor_idx,
            )


            # =============================================
            # Loss 1:
            # delta + uncertainty
            # =============================================

            delta_error = (
                output["delta"]
                - target_delta
            )


            loss_nll = (
                gaussian_nll(
                    delta_error,
                    output["sigma"],
                )
            )


            # =============================================
            # Loss 2:
            # 最终距离本身也要准确
            # =============================================

            corrected_distance = (
                baseline
                + output["delta"]
            )


            loss_distance = (
                torch.nn.functional
                .smooth_l1_loss(
                    corrected_distance,
                    truth,
                    beta=1.0,
                )
            )


            # =============================================
            # 总 Loss
            # =============================================

            loss = (
                loss_nll
                + 0.20
                * loss_distance
            )


            loss.backward()


            # 防止 GRU 梯度爆炸
            torch.nn.utils.clip_grad_norm_(
                model.parameters(),
                max_norm=5.0,
            )


            optimizer.step()


            total_loss += float(
                loss.item()
            )

            batch_count += 1


        # -------------------------------------------------
        # Validation
        # -------------------------------------------------

        metrics = evaluate(
            model,
            valid_loader,
            device,
        )


        scheduler.step(
            metrics["mae"]
        )


        current_lr = (
            optimizer
            .param_groups[0]["lr"]
        )


        print(
            f"Epoch {epoch:03d}"
            f" | Loss={total_loss / max(batch_count, 1):.4f}"
            f" | Valid MAE={metrics['mae']:.3f} m"
            f" | Median={metrics['median']:.3f} m"
            f" | P90={metrics['p90']:.3f} m"
            f" | Raw={metrics['raw_mae']:.3f} m"
            f" | Sigma={metrics['sigma_mean']:.3f} m"
            f" | LR={current_lr:.2e}"
        )


        # -------------------------------------------------
        # 保存最佳模型
        # -------------------------------------------------

        if (
            metrics["mae"]
            <
            best_mae
            - 1e-4
        ):

            best_mae = (
                metrics["mae"]
            )


            best_state = {
                key:
                    value.detach()
                    .cpu()
                    .clone()

                for (
                    key,
                    value
                )
                in model
                .state_dict()
                .items()
            }


            no_improve = 0


        else:

            no_improve += 1


        # -------------------------------------------------
        # Early stopping
        # -------------------------------------------------

        if (
            no_improve
            >= PATIENCE
        ):

            print()
            print(
                "Early stopping:",
                epoch,
            )

            break


    # =====================================================
    # 加载最佳模型
    # =====================================================

    if best_state is None:

        raise RuntimeError(
            "训练失败，没有得到模型"
        )


    model.load_state_dict(
        best_state
    )


    final_metrics = evaluate(
        model,
        valid_loader,
        device,
    )


    # =====================================================
    # 保存模型
    # =====================================================

    checkpoint = {

        "schema": 1,

        "model_type":
            "TemporalFusionGRU",

        "state_dict":
            model.state_dict(),

        "feature_names":
            FEATURE_NAMES,

        "anchor_ids":
            ANCHOR_IDS,

        "sequence_length":
            SEQ_LEN,

        "feature_mu":
            mu,

        "feature_std":
            std,

        "max_delta_m":
            model.max_delta_m,

        "validation_metrics":
            final_metrics,

        "held_out_points":
            sorted(
                HOLD_OUT_POINTS
            ),
    }


    torch.save(
        checkpoint,
        CHECKPOINT_PATH,
    )


    print()
    print("==============================")
    print("Training Finished")
    print("==============================")

    print()

    print(
        "Raw MAE:",
        f"{final_metrics['raw_mae']:.3f} m"
    )

    print(
        "GRU MAE:",
        f"{final_metrics['mae']:.3f} m"
    )

    print(
        "Median:",
        f"{final_metrics['median']:.3f} m"
    )

    print(
        "P90:",
        f"{final_metrics['p90']:.3f} m"
    )

    print()

    print(
        "模型已保存:"
    )

    print(
        CHECKPOINT_PATH
    )


if __name__ == "__main__":
    main()