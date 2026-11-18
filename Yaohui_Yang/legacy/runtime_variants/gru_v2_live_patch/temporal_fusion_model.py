import torch
import torch.nn as nn
import torch.nn.functional as F


# 这些都是你现有工程里已经有的 FTM / CSI 特征
FEATURE_NAMES = [
    "baseline_distance_m",
    "ftm_rssi",
    "csi_packets",
    "csi_rssi_mean",
    "csi_rssi_std",
    "csi_power_mean",
    "csi_roughness",
    "nlos_probability",
]

ANCHOR_IDS = [1, 2, 3, 5, 6, 7]


class TemporalFusionGRU(nn.Module):
    def __init__(
        self,
        feature_dim=len(FEATURE_NAMES),
        hidden_dim=128,
        num_layers=2,
        anchor_embed_dim=16,
        dropout=0.15,
        max_delta_m=65.0,
    ):
        super().__init__()

        self.max_delta_m = max_delta_m

        # 每个 Anchor 有一个可学习的身份向量
        self.anchor_embedding = nn.Embedding(
            num_embeddings=len(ANCHOR_IDS),
            embedding_dim=anchor_embed_dim,
        )

        # 每一帧的 FTM + CSI 特征先编码
        self.feature_encoder = nn.Sequential(
            nn.Linear(feature_dim, 64),
            nn.ReLU(),
            nn.Linear(64, 128),
            nn.ReLU(),
            nn.Dropout(dropout),
        )

        # GRU 学最近若干帧的时间变化
        self.gru = nn.GRU(
            input_size=128,
            hidden_size=hidden_dim,
            num_layers=num_layers,
            batch_first=True,
            dropout=dropout if num_layers > 1 else 0.0,
        )

        # Attention 自动判断最近哪些时刻更重要
        self.attention = nn.Sequential(
            nn.Linear(hidden_dim, 64),
            nn.Tanh(),
            nn.Linear(64, 1),
        )

        fusion_dim = hidden_dim + anchor_embed_dim

        self.fusion = nn.Sequential(
            nn.Linear(fusion_dim, 128),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(128, 64),
            nn.ReLU(),
        )

        # 输出 1：FTM 应该修正多少米
        self.delta_head = nn.Linear(64, 1)

        # 输出 2：这一条预测有多不确定
        self.sigma_head = nn.Linear(64, 1)

    def forward(self, x, anchor_idx):
        # x: [batch, time, feature]

        encoded = self.feature_encoder(x)

        # [batch, time, hidden]
        gru_out, _ = self.gru(encoded)

        # 时间注意力
        scores = self.attention(gru_out)
        attention_weights = torch.softmax(scores, dim=1)

        temporal_feature = torch.sum(
            gru_out * attention_weights,
            dim=1,
        )

        anchor_feature = self.anchor_embedding(anchor_idx)

        fused = torch.cat(
            [temporal_feature, anchor_feature],
            dim=1,
        )

        fused = self.fusion(fused)

        # residual correction
        delta = self.delta_head(fused)
        delta = torch.tanh(delta) * self.max_delta_m

        # uncertainty
        sigma = F.softplus(self.sigma_head(fused)) + 0.05

        return {
            "delta": delta.squeeze(-1),
            "sigma": sigma.squeeze(-1),
            "attention": attention_weights.squeeze(-1),
        }


def corrected_distance(baseline_distance, delta):
    return baseline_distance + delta