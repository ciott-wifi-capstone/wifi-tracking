#!/usr/bin/env python3
"""Resumable multi-distance V5 capture, validation-only selection and frozen testing.

The final test partition is never opened by fit(). Truth labels are used for
training/evaluation only; deployed models still require no true-distance input.
"""
import argparse
import hashlib
import json
import math
import random
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
import numpy as np

from cir_core import fit_model, load_model, save_model
from cir_distance import KINDS, collect as collect_features, predictions, summarize, write_csv
from record_serial_2min import run_capture

PROJECT = Path(__file__).resolve().parents[1]
PARTITIONS = ['train', 'validation', 'test']
SITES = {'train': 'A', 'validation': 'B', 'test': 'C'}
ALPHAS = [1.0, 10.0, 100.0]


class CaptureStopped(Exception):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n',
                         encoding='utf-8')
    temporary.replace(path)


def load_plan(root):
    path = Path(root) / 'experiment.json'
    if not path.exists():
        raise ValueError('Experiment not initialized: ' + str(path))
    plan = json.loads(path.read_text(encoding='utf-8'))
    if plan.get('schema') != 'qar_multi_capture_v1':
        raise ValueError('Unsupported experiment schema.')
    return plan


def save_plan(root, plan):
    write_json(Path(root) / 'experiment.json', plan)


def distance_tag(value):
    return f'{float(value):g}'.replace('.', 'p')


def capture_quality(path):
    rows, audits = collect_features([path])
    if len(audits) != 1:
        raise ValueError('Expected exactly one capture in ' + str(path))
    audit = audits[0]
    if len(rows) < 10:
        raise ValueError(f'Only {len(rows)} usable rounds; need at least 10. '
                         'Check V5 FTM_DETAIL/CSI_FTM_FRAME output and the COM port.')
    return rows, audit


def import_existing(root, slot, archive_path):
    """Copy the existing V5 capture bytes, retaining its original metadata."""
    destination = Path(root) / 'captures' / (slot['id'] + '_imported')
    if destination.exists():
        raise ValueError('Import destination already exists: ' + str(destination))
    with zipfile.ZipFile(archive_path) as archive:
        members = [m for m in archive.infolist() if not m.is_dir()]
        roots = {str(PurePosixPath(m.filename.replace('\\', '/')).parent)
                 for m in members if PurePosixPath(m.filename.replace('\\', '/')).name == 'metadata.json'}
        if len(roots) != 1:
            raise ValueError('Existing archive must contain one capture.')
        source_root = next(iter(roots))
        destination.mkdir(parents=True)
        for member in members:
            name = PurePosixPath(member.filename.replace('\\', '/'))
            if str(name.parent) == source_root:
                (destination / name.name).write_bytes(archive.read(member))
    rows, audit = capture_quality(destination)
    if not math.isclose(audit['true_distance_m'], slot['nominal_distance_m'], abs_tol=1e-6):
        raise ValueError('Imported label differs from the planned distance.')
    slot.update(status='complete', capture=str(destination.relative_to(root)),
                location_id='historical_A', audit=audit, imported_from=str(archive_path),
                actual_distance_m=audit['true_distance_m'], completed_utc=utc_now())


def initialize(args):
    root = Path(args.root)
    if (root / 'experiment.json').exists():
        raise ValueError('Experiment already exists; use collect/status to resume.')
    distances = sorted(set(float(x) for x in args.distances))
    extra = sorted(set(float(x) for x in args.extra_test_distances) - set(distances))
    if len(distances) < 3 or any(not math.isfinite(d) or d <= 0 for d in distances + extra):
        raise ValueError('Use at least three positive, finite training distances.')
    if not math.isfinite(args.seconds) or args.seconds < 30:
        raise ValueError('Capture duration must be at least 30 seconds; 120 is recommended.')
    root.mkdir(parents=True, exist_ok=True)
    slots = []
    for partition in PARTITIONS:
        values = distances + extra if partition == 'test' else distances
        for distance in sorted(values):
            identifier = f'{partition}_{SITES[partition]}_{distance_tag(distance)}m_R1'
            slots.append({'id': identifier, 'split': partition,
                          'location_id': SITES[partition], 'nominal_distance_m': distance,
                          'status': 'pending', 'attempts': []})
    plan = {'schema': 'qar_multi_capture_v1', 'created_utc': utc_now(),
            'condition': 'LOS', 'seconds': args.seconds, 'distances_m': distances,
            'extra_test_distances_m': extra, 'notes': args.notes, 'slots': slots,
            'method': 'Historical/new A training; distinct B validation; unseen C final test.'}
    save_plan(root, plan)
    if args.reuse_existing:
        for slot in plan['slots']:
            if slot['split'] != 'train' or slot['nominal_distance_m'] not in (1.0, 2.0, 3.0):
                continue
            name = f"V5_LOS_{int(slot['nominal_distance_m'])}m_A_R1.zip"
            source = PROJECT / 'example_data' / name
            if source.exists():
                import_existing(root, slot, source)
                save_plan(root, plan)
                print('Reused training capture:', name)
    status(SimpleNamespace(root=str(root)))


def status(args):
    plan = load_plan(args.root)
    for partition in PARTITIONS:
        slots = [s for s in plan['slots'] if s['split'] == partition]
        ready = [s for s in slots if s['status'] == 'complete']
        todo = [f"{s['nominal_distance_m']:g}m" for s in slots if s['status'] != 'complete']
        print(f'{partition:10s}: {len(ready)}/{len(slots)} complete; pending: ' + (', '.join(todo) or 'none'))
    print('Plan:', (Path(args.root) / 'experiment.json').resolve())


def collect_partition(args):
    root = Path(args.root)
    plan = load_plan(root)
    if args.split == 'test' and not (root / 'fit' / 'selection.json').exists():
        raise ValueError('Fit and freeze models after validation before collecting the final C test.')
    if args.split == 'test':
        verify_frozen(root)
    slots = [s for s in plan['slots'] if s['split'] == args.split and s['status'] != 'complete']
    if slots:
        try:
            __import__('serial')
        except ImportError as exc:
            raise ValueError('Install dependencies first: python -m pip install -r requirements.txt') from exc
    random.Random(17 + PARTITIONS.index(args.split)).shuffle(slots)
    print(f'Partition={args.split}; physical location={SITES[args.split]}.')
    print('Use a genuinely different placement for B/C; renaming files is not a new location.')
    print('Keep antenna height/orientation fixed and LOS clear. Close other serial monitors.')
    for slot in slots:
        requested = slot['nominal_distance_m']
        answer = input(f"\nSet antenna spacing to {requested:g}m at {slot['location_id']}. "
                       'Enter=start, number=actual measured meters, s=skip, q=stop: ').strip().lower()
        if answer == 'q':
            raise CaptureStopped('Stopped; repeat the same command to resume.')
        if answer == 's':
            continue
        actual = requested if answer == '' else float(answer)
        if not math.isfinite(actual) or actual <= 0:
            raise ValueError('Actual measured distance must be positive and finite.')
        attempt = len(slot['attempts']) + 1
        trial = slot['id'] + f'_attempt{attempt}'
        capture = root / 'captures' / trial
        while capture.exists():
            attempt += 1
            trial = slot['id'] + f'_attempt{attempt}'
            capture = root / 'captures' / trial
        entry = {'capture': str(capture.relative_to(root)), 'started_utc': utc_now(),
                 'actual_distance_m': actual, 'status': 'recording'}
        slot['attempts'].append(entry)
        save_plan(root, plan)
        logger = SimpleNamespace(port=args.port, baud=args.baud, condition='LOS',
                                 distance_m=actual, trial=trial, rounds=0, seconds=plan['seconds'],
                                 blocker='none', notes=f"split={args.split};site={slot['location_id']};"
                                 f"nominal_m={requested:g};{plan['notes']}", output=str(root / 'captures'))
        started = time.monotonic()
        try:
            run_capture(logger)
            elapsed = time.monotonic() - started
            rows, audit = capture_quality(capture)
            if elapsed < 0.9 * plan['seconds']:
                raise ValueError(f'Capture was interrupted after {elapsed:.1f}s; keep as partial.')
            annotation = {'split': args.split, 'location_id': slot['location_id'],
                          'nominal_distance_m': requested, 'actual_distance_m': actual,
                          'elapsed_s': elapsed, 'usable_rounds': len(rows)}
            write_json(capture / 'experiment_capture.json', annotation)
            slot.update(status='complete', capture=str(capture.relative_to(root)), audit=audit,
                        actual_distance_m=actual, completed_utc=utc_now())
            entry.update(status='complete', elapsed_s=elapsed)
            print(f"Accepted: {len(rows)} rounds; {audit['paired_cir_frames']} paired CIR frames.")
        except (ValueError, OSError, KeyError) as exc:
            entry.update(status='partial_or_invalid', reason=str(exc))
            slot['status'] = 'pending'
            save_plan(root, plan)
            raise CaptureStopped('Capture retained but not accepted: ' + str(exc)) from exc
        save_plan(root, plan)
    status(args)


def load_partition(root, plan, split):
    slots = [s for s in plan['slots'] if s['split'] == split]
    incomplete = [s['id'] for s in slots if s['status'] != 'complete']
    if incomplete:
        raise ValueError(f'{split} incomplete: ' + ', '.join(incomplete))
    rows = []
    audits = []
    for slot in slots:
        capture_rows, audit = capture_quality(Path(root) / slot['capture'])
        for row in capture_rows:
            row.update(experiment_split=split, location_id=slot['location_id'],
                       nominal_distance_m=slot['nominal_distance_m'])
        rows.extend(capture_rows)
        audits.append(audit)
    if len({r['nominal_distance_m'] for r in rows}) < 3:
        raise ValueError(split + ' needs at least three planned distances.')
    return rows, audits


def assert_disjoint(first, second):
    if {r['capture_fingerprint'] for r in first} & {r['capture_fingerprint'] for r in second}:
        raise ValueError('Identical capture bytes across training/validation/test partitions.')
    if {r['trial'] for r in first} & {r['trial'] for r in second}:
        raise ValueError('Overlapping trial identifiers across partitions.')


def model_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fit(args):
    root = Path(args.root)
    plan = load_plan(root)
    target = root / 'fit'
    if (target / 'selection.json').exists():
        raise ValueError('Models already frozen. Use a new experiment for another fitting cycle.')
    # Intentionally do not open the test partition here.
    train_rows, train_audit = load_partition(root, plan, 'train')
    validation_rows, validation_audit = load_partition(root, plan, 'validation')
    assert_disjoint(train_rows, validation_rows)
    selected = {}
    choices = []
    for kind in KINDS:
        candidates = [0.0] if kind == 'ftm_affine' else ALPHAS
        best = None
        for alpha in candidates:
            model = fit_model(train_rows, kind=kind, alpha=alpha)
            result = predictions(validation_rows, {kind: model}, 'validation')
            score = next(s for s in summarize(result) if s['model'] == kind)
            choices.append({'model': kind, 'alpha': alpha, **score})
            key = (score['trial_macro_mae_m'], -alpha)
            if best is None or key < best[0]:
                best = (key, model, alpha)
        selected[kind] = best[1]
        selected[kind].update(selected_on='validation_B_only', selected_alpha=best[2],
                              training_fingerprints=sorted({r['capture_fingerprint'] for r in train_rows}),
                              validation_trials=sorted({r['trial'] for r in validation_rows}),
                              validation_fingerprints=sorted({r['capture_fingerprint'] for r in validation_rows}))
    target.mkdir(parents=True, exist_ok=True)
    (target / 'models').mkdir(exist_ok=True)
    hashes = {}
    for kind, model in selected.items():
        destination = target / 'models' / (kind + '.json')
        save_model(destination, model)
        hashes[kind] = model_hash(destination)
    fit_predictions = predictions(train_rows, selected, 'training_fit')
    val_predictions = predictions(validation_rows, selected, 'validation_selection')
    scores = summarize(fit_predictions + val_predictions)
    write_csv(target / 'candidate_validation_scores.csv', choices)
    write_csv(target / 'validation_predictions.csv', val_predictions)
    write_csv(target / 'validation_metrics.csv', scores)
    write_csv(target / 'train_validation_features.csv', train_rows + validation_rows)
    selection = {'schema': 'qar_frozen_fit_v1', 'frozen_utc': utc_now(), 'model_hashes': hashes,
                 'primary_model': 'cir_direct', 'alpha_candidates': ALPHAS,
                 'training_fingerprints': sorted({r['capture_fingerprint'] for r in train_rows}),
                 'validation_fingerprints': sorted({r['capture_fingerprint'] for r in validation_rows}),
                 'train_audit': train_audit, 'validation_audit': validation_audit,
                 'test_used_for_selection': False,
                 'refit_on_validation': False,
                 'selected_alphas': {kind: selected[kind]['selected_alpha'] for kind in KINDS}}
    write_json(target / 'selection.json', selection)
    print('Models frozen. Test captures have not been read.')
    print('Selected alphas:', selection['selected_alphas'])
    for score in scores:
        if score['split'] == 'validation_selection':
            print(f"validation {score['model']:24s} MAE={score['mae_m']:.3f}m "
                  f"macro-MAE={score['trial_macro_mae_m']:.3f}m")
    print('Next: move the complete setup to C and collect the test partition.')


def verify_frozen(root):
    target = Path(root) / 'fit'
    path = target / 'selection.json'
    if not path.exists():
        raise ValueError('No frozen fit. Complete train/validation and run fit first.')
    selection = json.loads(path.read_text(encoding='utf-8'))
    models = {}
    for kind in KINDS:
        file = target / 'models' / (kind + '.json')
        if model_hash(file) != selection['model_hashes'][kind]:
            raise ValueError('Frozen model changed before final testing: ' + kind)
        models[kind] = load_model(file)
    return selection, models


def evaluate(args):
    root = Path(args.root)
    plan = load_plan(root)
    selection, models = verify_frozen(root)
    rows, audit = load_partition(root, plan, 'test')
    used = set(selection['training_fingerprints']) | set(selection['validation_fingerprints'])
    if used & {r['capture_fingerprint'] for r in rows}:
        raise ValueError('Final test contains training/validation capture bytes.')
    result = predictions(rows, models, 'final_test_C')
    scores = summarize(result)
    target = root / 'fit'
    write_csv(target / 'test_predictions.csv', result)
    write_csv(target / 'test_metrics.csv', scores)
    per_distance = []
    for value in sorted({r['true_distance_m'] for r in rows}):
        subset = [r for r in result if r['true_distance_m'] == value]
        per_distance.extend({'true_distance_m': value, **s} for s in summarize(subset))
    write_csv(target / 'test_by_distance.csv', per_distance)
    write_json(target / 'final_evaluation.json', {'evaluated_utc': utc_now(), 'metrics': scores,
               'test_audit': audit, 'model_hashes': selection['model_hashes'],
               'test_used_for_selection': False})
    report = ['# 多距离最终测试', '',
              '训练A、选参B、最终测试C按完整采集隔离。模型只用A拟合，B选择正则化参数，'
              '模型冻结后才读取C。所有域外和负数预测计入误差。', '',
              '| 模型 | MAE(m) | RMSE(m) | P90(m) | 会话数 |', '|---|---:|---:|---:|---:|']
    for score in scores:
        report.append(f"| {score['model']} | {score['mae_m']:.3f} | {score['rmse_m']:.3f} | "
                      f"{score['p90_abs_error_m']:.3f} | {score['sessions']} |")
        print(f"TEST {score['model']:24s} MAE={score['mae_m']:.3f}m P90={score['p90_abs_error_m']:.3f}m")
    report += ['', '| 测试采集 | 距离(m) | 可用/全部会话 |', '|---|---:|---:|']
    for item in audit:
        report.append(f"| {item['trial']} | {item['true_distance_m']:g} | "
                      f"{item['usable_sessions']}/{item['sessions']} |")
    report += ['', '误差按可提取有效CIR的会话统计；缺少CIR的会话保留在审核记录中，'
               '不能算作成功测距。逐距离结果见 test_by_distance.csv。若因查看C结果而改动模型或参数，'
               'C就成为开发数据；新的最终结论需要新的未使用采集。', '',
               '本轮仍为有限位置的LOS实验，不能直接推断NLOS或其他房间的精度。']
    (target / 'final_report.md').write_text('\n'.join(report) + '\n', encoding='utf-8')
    print('Report:', (target / 'final_report.md').resolve())


def wizard(args):
    root = Path(args.root)
    if not (root / 'experiment.json').exists():
        initialize(args)
    else:
        print('Resuming the saved plan; distance/duration options apply only at initialization.')
    for partition in ['train', 'validation']:
        collect_partition(SimpleNamespace(root=str(root), split=partition, port=args.port, baud=args.baud))
        if any(s['status'] != 'complete' for s in load_plan(root)['slots'] if s['split'] == partition):
            raise CaptureStopped('Partition incomplete; rerun wizard to resume.')
    if not (root / 'fit' / 'selection.json').exists():
        fit(args)
    verify_frozen(root)
    collect_partition(SimpleNamespace(root=str(root), split='test', port=args.port, baud=args.baud))
    evaluate(args)
    print('Live prediction command:')
    print(f'python tools/cir_distance.py live --port {args.port} --models "{root / "fit" / "models"}"')


def export(args):
    root = Path(args.root)
    load_plan(root)
    destination = Path(args.out)
    if destination.exists():
        raise ValueError('Export already exists; choose a new --out filename.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    files = [p for p in root.rglob('*') if p.is_file() and p.resolve() != destination.resolve()]
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(files):
            archive.write(file, root.name + '/' + file.relative_to(root).as_posix())
    print('Exported:', destination.resolve())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name, function in [('init', initialize), ('status', status), ('collect', collect_partition),
                           ('fit', fit), ('evaluate', evaluate), ('wizard', wizard), ('export', export)]:
        command = commands.add_parser(name)
        command.add_argument('--root', default='experiments/LOS_MULTI')
        if name in ('init', 'wizard'):
            command.add_argument('--distances', nargs='+', type=float, default=[1, 2, 3, 4, 6, 9])
            command.add_argument('--extra-test-distances', nargs='*', type=float, default=[])
            command.add_argument('--seconds', type=float, default=120)
            command.add_argument('--reuse-existing', action='store_true')
            command.add_argument('--notes', default='')
        if name in ('collect', 'wizard'):
            command.add_argument('--port', required=True)
            command.add_argument('--baud', type=int, default=115200)
        if name == 'collect':
            command.add_argument('--split', choices=PARTITIONS, required=True)
        if name == 'export':
            command.add_argument('--out', default='LOS_MULTI_dataset.zip')
        command.set_defaults(run=function)
    args = parser.parse_args()
    try:
        args.run(args)
    except CaptureStopped as exc:
        print(str(exc))
    except (ValueError, FileNotFoundError) as exc:
        parser.exit(2, str(exc) + '\n')


if __name__ == '__main__':
    main()
