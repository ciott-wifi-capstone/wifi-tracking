#!/usr/bin/env python3
"""Reproduce the frozen A-train/B-selection single-link results and figures.

Default input is the supplied session feature table. With --raw-recordings,
re-extract features from the original recordings ZIP/folder and verify that
they reproduce the supplied features. No test split is fitted or evaluated.
"""
import argparse
import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path
import sys

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'code'))
from cir_core import CIR_FEATURES, FTM_FEATURES, fit_model, load_model, predict

KINDS = ['raw_ftm', 'constant_training_mean', 'ftm_affine', 'cir_direct', 'cir_residual']
LABELS = {'raw_ftm': 'Raw FTM', 'constant_training_mean': 'Constant 6.5 m',
          'ftm_affine': 'FTM linear', 'cir_direct': 'CIR only', 'cir_residual': 'FTM + CIR'}
CHINESE = {'raw_ftm': '原始 FTM', 'constant_training_mean': '固定输出训练均值 6.5 m',
           'ftm_affine': 'FTM 线性校准', 'cir_direct': '仅 CIR 特征回归', 'cir_residual': 'FTM＋CIR 残差修正'}
COLORS = {'raw_ftm': '#666666', 'constant_training_mean': '#999999',
          'ftm_affine': '#D97706', 'cir_direct': '#167D8D', 'cir_residual': '#7654A3'}
NUMERIC = set(CIR_FEATURES + FTM_FEATURES + ['true_distance_m', 'paired_cir_frames'])


def read_csv(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


def write_csv(path, rows):
    with Path(path).open('w', encoding='utf-8-sig', newline='') as stream:
        fields = list(dict.fromkeys(k for r in rows for k in r))
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def summarize(rows):
    errors = np.asarray([r['signed_error_m'] for r in rows])
    captures = defaultdict(list)
    for r in rows:
        captures[r['trial']].append(abs(r['signed_error_m']))
    return dict(sessions=len(rows), trials=len(captures),
                mae_m=float(np.mean(np.abs(errors))),
                rmse_m=float(np.sqrt(np.mean(errors**2))),
                median_abs_error_m=float(np.median(np.abs(errors))),
                p90_abs_error_m=float(np.percentile(np.abs(errors), 90)),
                mean_signed_error_m=float(np.mean(errors)),
                trial_macro_mae_m=float(np.mean([np.mean(v) for v in captures.values()])),
                negative_predictions=sum(r['predicted_m'] < 0 for r in rows),
                out_of_domain_sessions=sum(r['quality'] == 'OUT_OF_DOMAIN' for r in rows))


def estimates(rows, models, split):
    answer = []
    mean_label = next(iter(models.values()))['training_label_mean_m']
    for r in rows:
        values = {'raw_ftm': (r['ftm_distance_m'], 'RAW'),
                  'constant_training_mean': (mean_label, 'CONSTANT_BASELINE')}
        for kind, model in models.items():
            result = predict(model, r)
            values[kind] = (result['distance_m'], result['quality'])
        for kind, (value, quality) in values.items():
            error = float(value) - r['true_distance_m']
            answer.append(dict(split=split, trial=r['trial'], round_id=int(r['round_id']),
                               true_distance_m=r['true_distance_m'], model=kind,
                               predicted_m=float(value), quality=quality,
                               paired_cir_frames=int(r['paired_cir_frames']),
                               signed_error_m=error, absolute_error_m=abs(error)))
    return answer


def save_figure(fig, directory, name):
    for suffix in ('png', 'pdf', 'svg'):
        fig.savefig(directory / (name + '.' + suffix), dpi=300, bbox_inches='tight')
    plt.close(fig)


def draw_figures(all_predictions, metrics, directory):
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 10,
                         'axes.spines.top': False, 'axes.spines.right': False,
                         'axes.grid': True, 'grid.alpha': .2,
                         'pdf.fonttype': 42, 'ps.fonttype': 42})
    distances = sorted({r['true_distance_m'] for r in all_predictions})
    splits = [('training_fit', 'A: training'), ('validation_selection', 'B: model-selection validation')]
    fig, axes = plt.subplots(1, 2, figsize=(10.5, 4.1), sharey=True, layout='constrained')
    for ax, (split, title) in zip(axes, splits):
        rows = [r for r in all_predictions if r['split'] == split and r['model'] == 'raw_ftm']
        qs = np.asarray([np.percentile([r['predicted_m'] for r in rows if r['true_distance_m'] == d],
                                      [10, 50, 90]) for d in distances])
        ax.errorbar(distances, qs[:, 1], yerr=[qs[:, 1]-qs[:, 0], qs[:, 2]-qs[:, 1]],
                    fmt='o-', color='#2563A0', capsize=4, label='Median; P10-P90')
        ax.plot([0, 16], [0, 16], '--', color='#333333', label='Ideal range')
        ax.set(title=title, xlabel='Ground-truth distance (m)', xlim=(0, 16), xticks=distances)
        ax.legend(fontsize=9)
    axes[0].set_ylabel('Raw FTM distance (m)')
    save_figure(fig, directory, '01_raw_ftm_distance')

    shown = ['raw_ftm', 'ftm_affine', 'cir_direct', 'cir_residual']
    fig, ax = plt.subplots(figsize=(8.6, 4.4), layout='constrained')
    x = np.arange(len(shown))
    for offset, (split, label), color in zip([-.18, .18], splits, ['#79A7C9', '#CE8452']):
        vals = [next(r['mae_m'] for r in metrics if r['split'] == split and r['model'] == kind) for kind in shown]
        bars = ax.bar(x + offset, vals, width=.34, label=label, color=color, zorder=3)
        ax.bar_label(bars, fmt='%.2f', fontsize=9, padding=3)
    base = next(r['mae_m'] for r in metrics if r['split'] == 'validation_selection' and r['model'] == 'constant_training_mean')
    ax.axhline(base, color='#666666', linestyle='--', label=f'B constant baseline: {base:.2f} m')
    ax.set(xticks=x, xticklabels=[LABELS[k] for k in shown], ylabel='Mean absolute error (m)',
           title='Training fit and model-selection validation', ylim=(0, 11.4))
    ax.legend(fontsize=9, loc='upper right')
    save_figure(fig, directory, '02_training_validation_mae')

    validation = [r for r in all_predictions if r['split'] == 'validation_selection']
    fig, ax = plt.subplots(figsize=(7.3, 4.4), layout='constrained')
    for kind in KINDS:
        error = np.sort([r['absolute_error_m'] for r in validation if r['model'] == kind])
        xx = np.r_[0., error]
        yy = np.r_[0., np.arange(1, len(error) + 1) / len(error)]
        ax.step(xx, yy, where='post', color=COLORS[kind], linewidth=1.8,
                linestyle='--' if kind == 'constant_training_mean' else '-', label=LABELS[kind])
    ax.set(xlabel='Absolute ranging error (m)', ylabel='Empirical CDF',
           title='B validation: all 320 usable sessions', ylim=(0, 1.02), xlim=(0, None))
    ax.legend(fontsize=9, loc='lower right')
    save_figure(fig, directory, '03_validation_error_cdf')

    fig, ax = plt.subplots(figsize=(8.8, 4.5), layout='constrained')
    for kind in KINDS:
        means = [np.mean([r['absolute_error_m'] for r in validation
                          if r['model'] == kind and r['true_distance_m'] == d]) for d in distances]
        ax.plot(distances, means, 'o--' if kind == 'constant_training_mean' else 'o-',
                color=COLORS[kind], label=LABELS[kind], markersize=4)
    ax.set(xlabel='Ground-truth distance (m)', ylabel='Mean absolute error (m)',
           title='B validation: error by measured distance', xticks=distances)
    ax.legend(fontsize=9, loc='upper left')
    save_figure(fig, directory, '04_validation_mae_by_distance')


def report(metrics, per_distance, selection, models, out):
    index = {(r['split'], r['model']): r for r in metrics}
    text = ['# 单链路 CIR 拟合测距：论文初稿结果', '',
            '本报告基于 recordings(4).zip 的真实采集数据。所有图表都由随包代码生成；'
            '没有使用合成测量值，没有事后删除高误差距离，没有把负预测裁成零。', '',
            '## 数据与评估边界', '',
            '- 距离：1、2、3、4、6、9、12、15 m。每个距离有 A/B 两次采集，共 16 组，各约 120 秒。',
            '- 643 个成功 FTM 会话中，638 个可提取有效同帧 CIR；覆盖率 99.22%。'
            '638 个会话包含 18,625 对有效同帧 FTM/CSI。A 组 318 个、B 组 320 个。',
            '- 原始 FTM 与各模型均在同一批可用 CIR 会话上比较；另外 5 个会话的剔除原因保存在 selection.json。'
            '99.22% 是以已成功的 FTM 会话为分母的特征覆盖率，不是总体测距成功率。',
            '- A 组用于训练；B 组用于选择正则化强度，同时展示验证性能。B 组不是独立最终测试集。'
            '未将相邻会话随机混合后分成训练/测试。',
            '- 每组采集的训练总权重相同；特征均值、标准差只用 A 组计算。'
            '正则化候选为 1、10、100，按 B 组每次采集等权的 MAE 选择。',
            '- 尚无 C 组独立最终测试；本报告是单链路测距结果，不是六 Anchor 的定位误差。',
            '- 这些数据中没有已核实的六板 A1–A6 身份映射；A/B 是采集组名，不能等同于 Anchor 编号。', '',
            '## 处理流程与模型', '',
            '频域 CSI → 非 HT、20 MHz LLTF 的 IQ 解析及子载波映射 → 未观测频点的平滑 L1 重建'
            ' → 频域补零与 16 倍采样 IFFT → 相对 CIR 形状与频率选择性特征 → 回归。'
            '补零提供更密的时延采样，不增加原始信号带宽或物理分辨率。'
            'CIR 时间参考未做绝对传播时延校准，因此不把峰值索引直接换算成真实距离。', '',
            '仅 CIR 模型使用 8 个特征：主峰邻域能量比例、早期能量比例、尾部能量比例、'
            '相对 RMS 时延扩展、归一化能量熵、相对前沿偏移、频谱平坦度和子载波幅度变异系数。'
            '后两项从同一 CSI 频谱计算。FTM＋CIR 模型另外使用原始 FTM 距离、'
            '帧 RTT 距离中位数、帧距离 MAD 和 FTM RSSI。', '',
            '三种模型：FTM 线性校准；仅 CIR 特征的岭回归；FTM＋CIR 的残差岭回归。'
            '残差模型先预测真实距离减去原始 FTM 距离，再加回原始 FTM 距离。'
            '实际推理只使用测量特征，不输入真实距离、文件名、点号或板子坐标。', '',
            f"选定 alpha：FTM 线性={selection['selected_alphas']['ftm_affine']:g}，"
            f"仅 CIR={selection['selected_alphas']['cir_direct']:g}，"
            f"FTM＋CIR={selection['selected_alphas']['cir_residual']:g}。", '',
            '## 主要结果', '',
            '| 方法 | A 训练 MAE (m) | B 验证 MAE (m) | B RMSE (m) | B 中位绝对误差 (m) | B P90 (m) |',
            '|---|---:|---:|---:|---:|---:|']
    for kind in KINDS:
        a, b = index['training_fit', kind], index['validation_selection', kind]
        text.append(f"| {CHINESE[kind]} | {a['mae_m']:.3f} | {b['mae_m']:.3f} | {b['rmse_m']:.3f} | "
                    f"{b['median_abs_error_m']:.3f} | {b['p90_abs_error_m']:.3f} |")
    raw, combined, constant = [index['validation_selection', k] for k in ['raw_ftm', 'cir_residual', 'constant_training_mean']]
    reduction = (raw['mae_m'] - combined['mae_m']) / raw['mae_m'] * 100
    constant_reduction = (constant['mae_m'] - combined['mae_m']) / constant['mae_m'] * 100
    text += ['', f"FTM＋CIR 在 B 组的 MAE 相对原始 FTM 下降 {reduction:.1f}%，"
             f"但相对固定均值基线仅下降 {constant_reduction:.1f}%。"
             '仅 CIR 模型的 B 组 MAE 高于该常数基线。因此当前证据不能支持纯 CIR '
             '具备可靠的通用测距能力，也不能支持分米级泛化精度。', '',
             'FTM＋CIR 的 A 组 MAE 为 0.468 m，这是训练回代误差。B 组 MAE 为 4.009 m、'
             'P90 为 9.680 m，显示跨采集组的性能退化。'
             '这种退化与模型过拟合、链路条件变化相容，但日志不足以确定唯一物理原因。', '',
             '## B 组逐距离 MAE', '',
             '| 距离 (m) | 会话数 | 原始 FTM | FTM 线性 | 仅 CIR | FTM＋CIR |',
             '|---:|---:|---:|---:|---:|---:|']
    for distance in sorted({r['true_distance_m'] for r in per_distance if r['split'] == 'validation_selection'}):
        subset = {r['model']: r for r in per_distance if r['split'] == 'validation_selection' and r['true_distance_m'] == distance}
        text.append(f"| {distance:g} | {subset['raw_ftm']['sessions']} | " +
                    ' | '.join(f"{subset[k]['mae_m']:.3f}" for k in ['raw_ftm', 'ftm_affine', 'cir_direct', 'cir_residual']) + ' |')
    affine = models['ftm_affine']
    slope = affine['coefficient'][0] / affine['scale'][0]
    intercept = affine['intercept'] - slope * affine['center'][0]
    text += ['', 'B 组 15 m 的原始 FTM 中位数约 43.8 m，是当前远距误差的重要来源。'
             '该组保留在所有总指标中。不能仅凭日志断言 Anchor 损坏，或在看到结果后将该组从正式指标删除。', '',
             '## 可直接写入初稿的结果段落', '',
             '本研究在 1–15 m 范围内的八个离散距离进行单链路 LOS 测量，采集 A、B 两组数据，'
             '共获得 638 个具有有效同帧 CIR 特征的测距会话。模型仅使用 A 组进行拟合，'
             '使用 B 组选择正则化参数并评估验证性能。原始 FTM 在 B 组的 MAE 为 9.777 m，'
             'FTM 线性校准、仅 CIR 特征回归及 FTM＋CIR 残差修正后的 MAE 分别为 '
             '4.630 m、4.516 m 和 4.009 m。残差修正相对原始 FTM 的 MAE 降低约 59.0%，'
             '但与固定输出训练距离均值的基线相比改善有限；同时，其训练 MAE 为 0.468 m，'
             '明显低于验证 MAE。上述结果表明该方法在已采集条件下具有误差修正潜力，'
             '但跨采集组的泛化能力仍受限制，需要进一步开展独立测试与多 Anchor 验证。', '',
             '## 拟合公式与复现', '',
             f'FTM 线性校准公式（距离单位 m）：`d_hat = {slope:.10f} * d_FTM {intercept:+.10f}`。', '',
             'CIR 直接回归：`d_hat = b + sum(beta_j * (f_j - mu_j) / sigma_j)`。'
             'FTM＋CIR 残差修正：`d_hat = d_FTM + b + sum(beta_j * (f_j - mu_j) / sigma_j)`。'
             '完整系数、标准化参数与模型元数据在 models/ 和 tables/model_coefficients.csv。', '',
             '```powershell', 'python -m pip install numpy matplotlib', r'python .\reproduce_results.py', '```', '',
             '复核原始 ZIP 的特征提取：', '',
             '```powershell', r'python .\reproduce_results.py --raw-recordings "C:\你的目录\recordings(4).zip"', '```', '',
             '默认复现从随包的会话特征表开始，同时重新执行 A 训练/B 选参，核对冻结模型预测，'
             '再生成表格和图。原始 ZIP 参数会重新从 IQ 数据提取 CIR 特征并与已保存特征逐会话核对。'
             '原始测量 ZIP 未在此结果包中重复打包。', '',
             '## 图注', '',
             '- 图 1（01_raw_ftm_distance）：A/B 两组原始 FTM 距离随真实距离的变化，点为中位数，'
             '误差棒为 P10–P90；同一会话筛选口径与后续模型一致。',
             '- 图 2（02_training_validation_mae）：训练回代与模型选择验证 MAE 的比较。'
             '虚线为 B 组固定 6.5 m 基线。',
             '- 图 3（03_validation_error_cdf）：B 组全部 320 个可用会话的绝对误差经验 CDF。'
             '包含负预测与域外预测对应的误差，横轴展示完整误差范围。',
             '- 图 4（04_validation_mae_by_distance）：B 组逐距离平均绝对误差，保留全部八个测量距离。', '',
             '图提供 300 dpi PNG、矢量 PDF 和 SVG。MAE/RMSE/P90 均按会话计算；'
             '另附 trial_macro_mae_m，表示每次采集等权平均，避免不同会话数量改变采集组权重。', '',
             '## 后续使用', '',
             '根目录 models/ 对应本次八距离 A/B 拟合；不要与固件工具包根目录的旧三距离演示模型混用。'
             '模型仅在原测量链路上进行了上述验证，不应直接宣布可迁移至六块不同 Anchor。'
             '编号 A1–A6 后保持 20 MHz、信道 6、FTM offset=0，并记录每块 AP BSSID；'
             '在少量独立标定点检查各板偏差后，再冻结标定参数开展多 Anchor 测试。', '',
             '本报告保留 20 个 FTM＋CIR 负距离预测和 85 个 OUT_OF_DOMAIN 标签的预测。'
             '质量标签仅用于诊断，未用于筛除样本；OUT_OF_DOMAIN 也不是经校准的失败概率。', '']
    (out / 'README_论文结果.md').write_text('\n'.join(text), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--raw-recordings')
    args = parser.parse_args()
    features = read_csv(ROOT / 'data/train_validation_features.csv')
    for r in features:
        for k in NUMERIC:
            r[k] = float(r[k])
        r['round_id'] = int(r['round_id'])
    train = [r for r in features if r['experiment_split'] == 'train']
    validation = [r for r in features if r['experiment_split'] == 'validation']
    assert len(train) == 318 and len(validation) == 320
    assert not ({r['capture_fingerprint'] for r in train} & {r['capture_fingerprint'] for r in validation})
    selection = json.loads((ROOT / 'data/selection.json').read_text(encoding='utf-8'))
    models = {kind: load_model(ROOT / 'models' / (kind + '.json')) for kind in KINDS[2:]}
    for kind, model in models.items():
        assert hashlib.sha256((ROOT / 'models' / (kind + '.json')).read_bytes()).hexdigest() == selection['model_hashes'][kind]
        candidates = []
        for alpha in ([0.] if kind == 'ftm_affine' else [1., 10., 100.]):
            fresh = fit_model(train, kind=kind, alpha=alpha)
            rows = [r for r in estimates(validation, {kind: fresh}, 'validation_selection') if r['model'] == kind]
            candidates.append((summarize(rows)['trial_macro_mae_m'], -alpha, fresh))
        best = min(candidates, key=lambda x: x[:2])
        assert -best[1] == selection['selected_alphas'][kind]
        for r in train + validation:
            np.testing.assert_allclose(predict(model, r)['distance_m'], predict(best[2], r)['distance_m'], rtol=0, atol=1e-8)
    if args.raw_recordings:
        from cir_io import extract_trial, read_trials
        observed = {}
        for trial in read_trials([args.raw_recordings]):
            # Frozen references came from extracted text files with universal
            # newlines. Normalize fingerprint text only; retain the original
            # IQ values and frame contents during actual feature extraction.
            names = ['ftm.csv', 'ftm_detail.csv', 'csi.csv', 'csi_ftm_frame.csv']
            canonical = ''.join(trial['files'].get(n, '').replace('\r\n', '\n').replace('\r', '\n') for n in names)
            trial['fingerprint'] = hashlib.sha256(canonical.encode()).hexdigest()
            rows, _ = extract_trial(trial)
            for r in rows:
                key = (r['trial'], int(r['round_id']))
                if key in observed:
                    raise ValueError('Duplicate capture/session in raw input: ' + str(key))
                observed[key] = r
        assert len(observed) == len(features), 'Raw input does not contain exactly the 16 reference captures.'
        for r in features:
            raw = observed[r['trial'], r['round_id']]
            assert raw['capture_fingerprint'] == r['capture_fingerprint']
            for k in NUMERIC:
                np.testing.assert_allclose(float(raw[k]), r[k], rtol=0, atol=1e-8)
        print('Raw IQ/FTM extraction reproduced all 638 session features.', flush=True)
    results = estimates(train, models, 'training_fit') + estimates(validation, models, 'validation_selection')
    reference = {(r['trial'], int(r['round_id']), r['model']): float(r['predicted_m'])
                 for r in read_csv(ROOT / 'data/validation_predictions_reference.csv')}
    current = {(r['trial'], r['round_id'], r['model']): r['predicted_m']
               for r in results if r['split'] == 'validation_selection'}
    assert reference.keys() == current.keys()
    for key in reference:
        np.testing.assert_allclose(reference[key], current[key], rtol=0, atol=1e-8)
    metrics, per_distance = [], []
    for split in ['training_fit', 'validation_selection']:
        for kind in KINDS:
            rows = [r for r in results if r['split'] == split and r['model'] == kind]
            metrics.append(dict(split=split, model=kind, **summarize(rows)))
            for d in sorted({r['true_distance_m'] for r in rows}):
                group = [r for r in rows if r['true_distance_m'] == d]
                per_distance.append(dict(split=split, model=kind, true_distance_m=d, **summarize(group)))
    tables, figures = ROOT / 'tables', ROOT / 'figures'
    tables.mkdir(exist_ok=True)
    figures.mkdir(exist_ok=True)
    write_csv(tables / 'all_session_predictions.csv', results)
    write_csv(tables / 'overall_metrics.csv', metrics)
    write_csv(tables / 'metrics_by_distance.csv', per_distance)
    coefficients = []
    for kind, model in models.items():
        for name, center, scale, coefficient in zip(model['feature_names'], model['center'], model['scale'], model['coefficient']):
            coefficients.append(dict(model=kind, feature=name, center=center, scale=scale,
                                     coefficient=coefficient, intercept=model['intercept'], alpha=model['alpha']))
    write_csv(tables / 'model_coefficients.csv', coefficients)
    report(metrics, per_distance, selection, models, ROOT)
    draw_figures(results, metrics, figures)
    for r in metrics:
        print(f"{r['split']:22} {r['model']:24} MAE={r['mae_m']:.6f} m n={r['sessions']}")
    print('Verified frozen models, repeated A-only fitting/B-only selection, and regenerated all tables/figures.')


if __name__ == '__main__':
    main()
