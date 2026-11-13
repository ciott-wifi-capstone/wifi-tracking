#!/usr/bin/env python3
"""Train, replay and stream CIR-derived distance models on the existing V5."""
import argparse
import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path
import numpy as np

from cir_core import CIR_FEATURES, FTM_FEATURES, fit_model, load_model, predict, save_model
from cir_io import SerialRoundAdapter, extract_trial, read_trials

KINDS = ['ftm_affine', 'cir_direct', 'cir_residual']


def write_csv(path, rows):
    if not rows:
        return
    names = list(dict.fromkeys(k for row in rows for k in row))
    with Path(path).open('w', encoding='utf-8-sig', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=names)
        writer.writeheader()
        writer.writerows(rows)


def collect(inputs):
    rows = []
    audits = []
    for trial in read_trials(inputs):
        features, audit = extract_trial(trial)
        rows.extend(features)
        audits.append(audit)
    return rows, audits


def predictions(rows, models, split):
    out = []
    for row in rows:
        base = {k: row[k] for k in ['trial', 'round_id', 'true_distance_m']}
        base.update(split=split, model='raw_ftm', predicted_m=row['ftm_distance_m'],
                    quality='RAW', paired_cir_frames=row['paired_cir_frames'])
        out.append(base)
        first_model = next(iter(models.values()))
        out.append({**base, 'model': 'constant_training_mean',
                    'predicted_m': first_model['training_label_mean_m'], 'quality': 'CONSTANT_BASELINE'})
        for kind, model in models.items():
            estimate = predict(model, row)
            out.append({**base, 'model': kind, 'predicted_m': estimate['distance_m'],
                        'quality': estimate['quality']})
    return out


def metrics(rows):
    errors = np.asarray([float(r['predicted_m']) - float(r['true_distance_m']) for r in rows])
    return {'sessions': len(rows), 'mae_m': float(np.mean(np.abs(errors))),
            'rmse_m': float(np.sqrt(np.mean(errors ** 2))),
            'median_abs_error_m': float(np.median(np.abs(errors))),
            'p90_abs_error_m': float(np.percentile(np.abs(errors), 90)),
            'mean_signed_error_m': float(np.mean(errors)),
            'out_of_domain_sessions': sum(r['quality'] == 'OUT_OF_DOMAIN' for r in rows),
            'negative_predictions': sum(float(r['predicted_m']) < 0 for r in rows)}


def summarize(prediction_rows):
    result = []
    for split in dict.fromkeys(r['split'] for r in prediction_rows):
        for kind in ['raw_ftm', 'constant_training_mean'] + KINDS:
            rows = [r for r in prediction_rows if r['split'] == split and r['model'] == kind]
            if not rows:
                continue
            by_trial = [[r for r in rows if r['trial'] == name] for name in sorted({r['trial'] for r in rows})]
            result.append({'split': split, 'model': kind, 'trials': len(by_trial),
                           **metrics(rows),
                           'trial_macro_mae_m': float(np.mean([metrics(g)['mae_m'] for g in by_trial]))})
    return result


def write_report(output, summary, audits, model_paths):
    text = [
        '# 现有 V5 的 CIR 距离拟合结果', '',
        '处理链：20MHz LLTF CSI → 正确子载波映射 → 缺失频点的平滑L1重建 → '
        '16倍时延采样 → 相对CIR前沿/能量特征 → 距离回归。', '',
        '已有Anchor/Tag固件与采集格式继续使用，拟合和实时预测在电脑端执行。'
        'CIR时延参考未做绝对传播时延标定；本项目学习CIR特征与距离的关系。', '',
        '## 数据', '',
        '| 采集 | 真实距离(m) | 可用/全部会话 | 同帧CIR帧数 |',
        '|---|---:|---:|---:|',
    ]
    for audit in audits:
        text.append(f"| {audit['trial']} | {audit['true_distance_m']:.2f} | "
                    f"{audit['usable_sessions']}/{audit['sessions']} | {audit['paired_cir_frames']} |")
    text += ['', '## 评估', '',
             '所有误差均包含越界或负数预测，不按质量标签删除难样本，也不把预测钳制到1—3m。'
             '原始FTM和模型使用同一批可提取CIR的会话；缺失CIR的会话数量见数据审核。', '',
             '| 划分 | 模型 | MAE(m) | 中位绝对误差(m) | P90(m) | 会话数 | 域外数 |',
             '|---|---|---:|---:|---:|---:|---:|']
    for row in summary:
        text.append(f"| {row['split']} | {row['model']} | {row['mae_m']:.3f} | "
                    f"{row['median_abs_error_m']:.3f} | {row['p90_abs_error_m']:.3f} | "
                    f"{row['sessions']} | {row['out_of_domain_sessions']} |")
    text += ['', '- `training_fit`：训练回代，仅显示是否完成拟合，不是测距精度结论。',
             '- `leave_capture_out`：每次整组留出一个采集；本次三组分别只有1/2/3m，'
             '因此也测试未见距离和外推，不是同一距离在新场景的完整验证。',
             '- `external_capture_holdout`：更早的独立2m采集，未参与训练或模型参数选择。',
             '- `cir_direct`：仅用CIR形状/CSI频率选择性特征，直接拟合距离。',
             '- `cir_residual`：用CIR和FTM特征拟合剩余距离修正，最终值为原始FTM加预测修正。',
             '- `ftm_affine`：只用FTM做线性校准，用来检查CIR是否带来额外信息。',
             '- `constant_training_mean`：始终输出训练集各采集真实距离的均值。'
             '本次训练均值恰好是2m，因此它在独立2m测试上误差为0；'
             '这说明只用一个2m测试不能证明CIR模型获得了通用分米级精度。',
             '- 固定回归强度alpha=10，未依据外部测试误差选参；每个采集的总训练权重相同。',
             '', '## 使用限制', '',
             '三组训练采集只有三个已知距离，并且距离与摆放条件混在一起。'
             '上千条CSI不是上千个独立场景，现阶段模型均为实验模型。'
             '一次外部2m测试也不足以证明其他房间、NLOS或更远距离的精度。', '',
             '实时输出同时显示原始FTM、CIR直接拟合、CIR+FTM修正和质量状态。'
             '`OUT_OF_DOMAIN`只表示特征超出训练分布的简单检查，不是误差概率。'
             '预测需要有效同帧CIR，缺失时保留原始FTM并将拟合结果留空。', '',
             '模型不读取真实距离标签、试验名或round_id来计算距离。'
             '无真实距离输入的原始串口日志回放已用于检查实时接口。', '',
             '## 模型文件', '']
    text.extend('- ' + str(p) for p in model_paths)
    (output / 'fit_report.md').write_text('\n'.join(text) + '\n', encoding='utf-8')


def train(args):
    output = Path(args.out)
    output.mkdir(parents=True, exist_ok=True)
    model_dir = output.parent / 'models' if output.name == 'results' else output / 'models'
    model_dir.mkdir(parents=True, exist_ok=True)
    rows, audits = collect(args.train)
    external, external_audits = collect(args.holdout) if args.holdout else ([], [])
    if not rows:
        raise ValueError('No usable training sessions.')
    fingerprints = {r['capture_fingerprint'] for r in rows}
    if fingerprints & {r['capture_fingerprint'] for r in external}:
        raise ValueError('Training and external holdout contain the same capture bytes.')
    groups = sorted({r['trial'] for r in rows})
    if set(groups) & {r['trial'] for r in external}:
        raise ValueError('Training and holdout have overlapping trial names.')
    models = {kind: fit_model(rows, kind=kind) for kind in KINDS}
    for model in models.values():
        model['training_fingerprints'] = sorted(fingerprints)
        model['independent_holdout_trials'] = sorted({r['trial'] for r in external})
    prediction_rows = predictions(rows, models, 'training_fit')
    if len(groups) >= 3:
        for group in groups:
            fit_rows = [r for r in rows if r['trial'] != group]
            held_rows = [r for r in rows if r['trial'] == group]
            if len({r['true_distance_m'] for r in fit_rows}) < 2:
                continue
            fold_models = {kind: fit_model(fit_rows, kind=kind) for kind in KINDS}
            prediction_rows.extend(predictions(held_rows, fold_models, 'leave_capture_out'))
    if external:
        prediction_rows.extend(predictions(external, models, 'external_capture_holdout'))
    paths = []
    for kind, model in models.items():
        path = model_dir / (kind + '.json')
        save_model(path, model)
        paths.append(path)
    summary = summarize(prediction_rows)
    write_csv(output / 'round_features.csv', rows + external)
    write_csv(output / 'predictions.csv', prediction_rows)
    write_csv(output / 'metrics.csv', summary)
    (output / 'data_audit.json').write_text(json.dumps(audits + external_audits, indent=2,
                                                     ensure_ascii=False) + '\n', encoding='utf-8')
    (output / 'evaluation.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    write_report(output, summary, audits + external_audits, paths)
    for row in summary:
        print(f"{row['split']:26s} {row['model']:14s} MAE={row['mae_m']:.3f}m "
              f"P90={row['p90_abs_error_m']:.3f}m n={row['sessions']}")
    print('Models:', model_dir.resolve())
    print('Report:', (output / 'fit_report.md').resolve())


def load_models(args):
    model_path = Path(args.models)
    return {kind: load_model(model_path / (kind + '.json')) for kind in KINDS}


def one_prediction(row, models, primary='cir_direct'):
    result = {'round_id': row['round_id'], 'epoch': row.get('epoch', 0),
              'raw_ftm_m': row['ftm_distance_m'], 'paired_cir_frames': row['paired_cir_frames']}
    for kind, model in models.items():
        value = predict(model, row)
        result[kind + '_m'] = value['distance_m']
        result[kind + '_quality'] = value['quality']
        result[kind + '_for_use_m'] = value['distance_for_use_m']
    result['primary_model'] = primary
    result['distance_m'] = result[primary + '_m']
    result['distance_for_use_m'] = result[primary + '_for_use_m']
    result['quality'] = result[primary + '_quality']
    return result


def print_prediction(result):
    def number(value):
        return '--' if value is None else f'{value:.3f}'
    print(f"ROUND={result['round_id']}  DISTANCE={number(result['distance_m'])}m "
          f"[{result['quality']}]  RAW_FTM={result['raw_ftm_m']:.3f}m  "
          f"pairs={result['paired_cir_frames']}", flush=True)


def missing_cir_prediction(adapter, primary='cir_direct'):
    result = {'round_id': adapter.round_id, 'epoch': adapter.epoch,
              'raw_ftm_m': adapter.last_raw_ftm, 'paired_cir_frames': 0}
    for kind in KINDS:
        result[kind + '_m'] = None
        result[kind + '_quality'] = 'CIR_UNAVAILABLE'
        result[kind + '_for_use_m'] = None
    result.update(primary_model=primary, distance_m=None, distance_for_use_m=None,
                  quality='CIR_UNAVAILABLE')
    return result


def replay(args):
    models = load_models(args)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    results = []
    if args.log:
        adapter = SerialRoundAdapter()
        with Path(args.log).open(encoding='utf-8', errors='replace') as file:
            for line in file:
                row = adapter.feed(line)
                if row:
                    result = one_prediction(row, models, args.primary)
                    results.append(result)
                elif line.startswith('ROUND_END') and adapter.last_error and adapter.last_raw_ftm is not None:
                    results.append(missing_cir_prediction(adapter, args.primary))
        row = adapter.finish()
        if row:
            results.append(one_prediction(row, models, args.primary))
    else:
        rows, audits = collect(args.input)
        for row in rows:
            result = one_prediction(row, models, args.primary)
            result.update(trial=row['trial'], true_distance_m=row['true_distance_m'])
            results.append(result)
        (out / 'audit.json').write_text(json.dumps(audits, indent=2, ensure_ascii=False), encoding='utf-8')
    write_csv(out / 'predictions.csv', results)
    for result in results[:5]:
        print_prediction(result)
    print(f'Replayed {len(results)} rounds. Output: {(out / "predictions.csv").resolve()}')


def live(args):
    try:
        import serial
    except ImportError as exc:
        raise SystemExit('Install serial support: python -m pip install pyserial') from exc
    models = load_models(args)
    output = Path(args.out)
    if output.exists() and any(output.iterdir()):
        raise SystemExit('Output directory already nonempty; choose a new --out.')
    output.mkdir(parents=True, exist_ok=True)
    adapter = SerialRoundAdapter()
    started = time.monotonic()
    started_utc = datetime.now(timezone.utc).isoformat()
    count = 0
    # DTR/RTS are held inactive before opening to reduce unintended USB-UART resets.
    # Some native USB configurations may still reset; epochs handle round-ID reuse.
    device = serial.Serial(port=None, baudrate=args.baud, timeout=1)
    device.dtr = False
    device.rts = False
    device.port = args.port
    print('EXPERIMENTAL CIR model; no true-distance input is used. Ctrl+C stops.')
    print('Writing:', output.resolve())
    with (output / 'serial_raw.log').open('w', encoding='utf-8') as raw, \
         (output / 'predictions.csv').open('w', encoding='utf-8-sig', newline='') as csv_file:
        writer = None
        try:
            device.open()
            while args.seconds <= 0 or time.monotonic() - started < args.seconds:
                line = device.readline().decode('utf-8', errors='replace').strip()
                if not line:
                    continue
                raw.write(line + '\n')
                raw.flush()
                row = adapter.feed(line)
                result = one_prediction(row, models, args.primary) if row else None
                if result is None and line.startswith('ROUND_END') and adapter.last_error:
                    if adapter.last_raw_ftm is not None:
                        result = missing_cir_prediction(adapter, args.primary)
                    print('CIR unavailable:', adapter.last_error, flush=True)
                if result is not None:
                    result['elapsed_s'] = round(time.monotonic() - started, 3)
                    if writer is None:
                        writer = csv.DictWriter(csv_file, fieldnames=list(result))
                        writer.writeheader()
                    writer.writerow(result)
                    csv_file.flush()
                    count += 1
                    print_prediction(result)
        except KeyboardInterrupt:
            print('\nStopped.')
        finally:
            device.close()
    metadata = {'mode': 'live_inference', 'label_required': False, 'port': args.port,
                'baud': args.baud, 'prediction_rounds': count,
                'started_utc': started_utc, 'ended_utc': datetime.now(timezone.utc).isoformat(),
                'model_files': str(Path(args.models).resolve())}
    (output / 'inference_metadata.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    print('Finished:', count, 'predictions.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('train', help='Fit on complete captures, then evaluate held-out captures.')
    p.add_argument('--train', nargs='+', required=True)
    p.add_argument('--holdout', nargs='+', default=[])
    p.add_argument('--out', default='results')
    p.set_defaults(run=train)
    p = commands.add_parser('replay', help='Run deployed models on recordings or raw serial logs.')
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument('--input', nargs='+')
    group.add_argument('--log')
    p.add_argument('--models', default='models')
    p.add_argument('--primary', choices=KINDS, default='cir_direct')
    p.add_argument('--out', default='replay_results')
    p.set_defaults(run=replay)
    p = commands.add_parser('live', help='Predict on COM12 without a true-distance label.')
    p.add_argument('--port', required=True)
    p.add_argument('--baud', type=int, default=115200)
    p.add_argument('--models', default='models')
    p.add_argument('--primary', choices=KINDS, default='cir_direct')
    p.add_argument('--seconds', type=float, default=0)
    p.add_argument('--out', default='live_' + datetime.now().strftime('%Y%m%d_%H%M%S'))
    p.set_defaults(run=live)
    args = parser.parse_args()
    args.run(args)


if __name__ == '__main__':
    main()
