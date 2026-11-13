#!/usr/bin/env python3
"""Extract raw same-frame CIR, fit on A, select on B, and inspect offline ranges.

Windows: python run_distance_validation.py
Command-line only: python run_distance_validation.py --no-window
Other data: python run_distance_validation.py --input path/to/recordings.zip
"""
import argparse
from collections import defaultdict
from datetime import datetime
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'code'))
from cir_core import fit_model, predict, save_model
from cir_io import extract_trial, read_trials

KINDS = ['raw_ftm', 'ftm_affine', 'cir_direct', 'cir_residual', 'constant_training_mean']
MODELS = ['ftm_affine', 'cir_direct', 'cir_residual']
NAMES = {'raw_ftm': 'Raw FTM', 'ftm_affine': 'FTM linear', 'cir_direct': 'CIR only',
         'cir_residual': 'FTM + CIR', 'constant_training_mean': 'Constant baseline'}
SPLITS = {'A': 'training_fit', 'B': 'validation_selection'}
COLORS = {'raw_ftm': '#64748B', 'ftm_affine': '#C27618', 'cir_direct': '#148496',
          'cir_residual': '#7653A4', 'constant_training_mean': '#949494'}


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def write_csv(path, rows):
    if not rows:
        return
    fields = list(dict.fromkeys(k for row in rows for k in row))
    with Path(path).open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def canonical_fingerprint(trial):
    names = ['ftm.csv', 'ftm_detail.csv', 'csi.csv', 'csi_ftm_frame.csv']
    text = ''.join(trial['files'].get(n, '').replace('\r\n', '\n').replace('\r', '\n') for n in names)
    return hashlib.sha256(text.encode()).hexdigest()


def load_features(source):
    train, validation, audits, skipped = [], [], [], []
    seen, trial_names = set(), set()
    for trial in read_trials([source]):
        meta = json.loads(trial['files']['metadata.json'])
        name = meta['trial']
        match = re.search(r'_(A|B)_R\d+$', name)
        if not match or meta.get('condition', '').upper() != 'LOS':
            skipped.append({'trial': name, 'reason': 'Requires LOS and a trial name ending _A_Rn or _B_Rn.'})
            print(f'SKIP {name}: not an A/B LOS capture.', flush=True)
            continue
        fingerprint = canonical_fingerprint(trial)
        if fingerprint in seen or name in trial_names:
            raise ValueError('Duplicated capture or trial name: ' + name)
        seen.add(fingerprint)
        trial_names.add(name)
        trial['fingerprint'] = fingerprint
        rows, audit = extract_trial(trial)
        if not rows:
            raise ValueError(f'No usable same-frame CIR sessions in {name}; inspect the original recording.')
        group = match.group(1)
        audit['group'] = group
        audits.append(audit)
        for row in rows:
            if not math.isfinite(row['true_distance_m']) or row['true_distance_m'] <= 0:
                raise ValueError('Missing/invalid ground-truth distance in ' + name)
            row['group'] = group
        (train if group == 'A' else validation).extend(rows)
        print(f"LOAD {name:<18} group={group} distance={audit['true_distance_m']:g}m "
              f"usable={audit['usable_sessions']}/{audit['sessions']}", flush=True)
    if len({r['true_distance_m'] for r in train}) < 2 or not validation:
        raise ValueError('Need A training at two or more distances and at least one B validation capture.')
    if {r['capture_fingerprint'] for r in train} & {r['capture_fingerprint'] for r in validation}:
        raise ValueError('Training and validation contain the same capture.')
    order = lambda r: (r['true_distance_m'], r['trial'], r['round_id'])
    return sorted(train, key=order), sorted(validation, key=order), audits, skipped


def choose_models(train, validation):
    models, candidates = {}, []
    for kind in MODELS:
        best = None
        for alpha in ([0.] if kind == 'ftm_affine' else [1., 10., 100.]):
            model = fit_model(train, kind=kind, alpha=alpha)
            errors = defaultdict(list)
            for row in validation:
                errors[row['trial']].append(abs(predict(model, row)['distance_m'] - row['true_distance_m']))
            score = float(np.mean([np.mean(e) for e in errors.values()]))
            candidates.append({'model': kind, 'alpha': alpha, 'B_trial_macro_mae_m': score})
            key = (score, -alpha)
            if best is None or key < best[0]:
                best = (key, model)
        models[kind] = best[1]
        models[kind].update(selected_on='B_model_selection_validation', refit_on_B=False)
        print(f"FIT  {NAMES[kind]:<12} alpha={models[kind]['alpha']:g}; fitted on A only", flush=True)
    return models, candidates


def prediction_rows(rows, models, group):
    predictions = []
    constant = next(iter(models.values()))['training_label_mean_m']
    for row in rows:
        # Only predict()'s explicitly listed measured features enter inference.
        # Ground truth, filenames and IDs are used below for evaluation/display.
        values = {'raw_ftm': {'distance_m': row['ftm_distance_m'], 'quality': 'RAW'},
                  'constant_training_mean': {'distance_m': constant, 'quality': 'CONSTANT_BASELINE'}}
        values.update({kind: predict(model, row) for kind, model in models.items()})
        for kind in KINDS:
            value = float(values[kind]['distance_m'])
            error = value - row['true_distance_m']
            predictions.append({'group': group, 'split': SPLITS[group], 'trial': row['trial'],
                                'round_id': row['round_id'], 'true_distance_m': row['true_distance_m'],
                                'model': kind, 'predicted_m': value, 'signed_error_m': error,
                                'absolute_error_m': abs(error), 'quality': values[kind]['quality'],
                                'paired_cir_frames': row['paired_cir_frames']})
    return predictions


def metrics(rows):
    errors = np.asarray([r['signed_error_m'] for r in rows])
    groups = defaultdict(list)
    for r in rows:
        groups[r['trial']].append(r['absolute_error_m'])
    return {'sessions': len(rows), 'captures': len(groups), 'mae_m': float(np.mean(np.abs(errors))),
            'rmse_m': float(np.sqrt(np.mean(errors**2))),
            'median_abs_error_m': float(np.median(np.abs(errors))),
            'p90_abs_error_m': float(np.percentile(np.abs(errors), 90)),
            'trial_macro_mae_m': float(np.mean([np.mean(e) for e in groups.values()])),
            'negative_predictions': sum(r['predicted_m'] < 0 for r in rows),
            'out_of_domain_sessions': sum(r['quality'] == 'OUT_OF_DOMAIN' for r in rows)}


def process(source, destination):
    train, validation, audits, skipped = load_features(source)
    models, candidates = choose_models(train, validation)
    rows = prediction_rows(train, models, 'A') + prediction_rows(validation, models, 'B')
    summary, by_distance = [], []
    for group in ['A', 'B']:
        for kind in KINDS:
            selected = [r for r in rows if r['group'] == group and r['model'] == kind]
            summary.append({'group': group, 'split': SPLITS[group], 'model': kind, **metrics(selected)})
            for distance in sorted({r['true_distance_m'] for r in selected}):
                capture_rows = [r for r in selected if r['true_distance_m'] == distance]
                by_distance.append({'group': group, 'model': kind, 'true_distance_m': distance, **metrics(capture_rows)})
    destination.mkdir(parents=True, exist_ok=False)
    (destination / 'models').mkdir()
    for kind, model in models.items():
        save_model(destination / 'models' / (kind + '.json'), model)
    write_csv(destination / 'session_features.csv', train + validation)
    write_csv(destination / 'session_predictions.csv', rows)
    write_csv(destination / 'metrics.csv', summary)
    write_csv(destination / 'metrics_by_distance.csv', by_distance)
    write_csv(destination / 'candidate_scores.csv', candidates)
    write_json(destination / 'data_audit.json', {'captures': audits, 'skipped_unmatched_captures': skipped})
    write_json(destination / 'run_info.json', {
        'source': str(Path(source).resolve()),
        'source_sha256': hashlib.sha256(Path(source).read_bytes()).hexdigest() if Path(source).is_file() else None,
        'mode': 'OFFLINE_RAW_CSI_EXTRACTION_A_TRAIN_B_MODEL_SELECTION_VALIDATION',
        'training_sessions': len(train), 'validation_sessions': len(validation),
        'test_used': False, 'refit_on_validation': False,
        'alpha_candidates': [1., 10., 100.],
        'model_hashes': {k: hashlib.sha256((destination/'models'/(k+'.json')).read_bytes()).hexdigest() for k in models},
        'negative_or_out_of_domain_predictions_removed': False,
        'feature_coverage_denominator': 'all FTM session rows in each accepted capture; inspect audit for skip reasons',
    })
    return {'features': train + validation, 'audits': audits, 'models': models,
            'predictions': rows, 'metrics': summary, 'by_distance': by_distance, 'destination': destination}


def console_report(result, group, distance, kind, count):
    index = {(r['group'], r['model']): r for r in result['metrics']}
    print('\nOFFLINE SINGLE-LINK RANGING: A training / B model-selection validation')
    print('B was used to select alpha. It is NOT an independent final test set.')
    print('Negative and out-of-domain predictions are included in the metrics.\n')
    print(f"{'Method':<20} {'A train MAE':>13} {'B valid MAE':>13} {'B RMSE':>11} {'B P90':>11}")
    for model in KINDS:
        a, b = index['A', model], index['B', model]
        print(f"{NAMES[model]:<20} {a['mae_m']:>11.3f} m {b['mae_m']:>11.3f} m "
              f"{b['rmse_m']:>9.3f} m {b['p90_abs_error_m']:>9.3f} m")
    selected = [r for r in result['predictions'] if r['group'] == group and r['model'] == kind
                and (distance is None or np.isclose(r['true_distance_m'], distance))]
    if not selected:
        raise ValueError('No sessions match the requested group/distance.')
    score = metrics(selected)
    scope = 'all distances' if distance is None else f'true distance {distance:g} m'
    print(f"\nVIEW: group {group}, {scope}, {NAMES[kind]}, n={len(selected)}, subset MAE={score['mae_m']:.3f} m")
    print(f"{'Trial':<20} {'Round':>7} {'True(m)':>9} {'Range(m)':>10} {'AbsErr(m)':>11} {'Quality':>28}")
    for row in selected[:count]:
        print(f"{row['trial']:<20} {row['round_id']:>7} {row['true_distance_m']:>9.3f} "
              f"{row['predicted_m']:>10.3f} {row['absolute_error_m']:>11.3f} {row['quality']:>28}")
    print('\nAll session values, not only the displayed rows, are in session_predictions.csv.')


class DistanceViewer:
    """One resizable Matplotlib window for honest offline replay screenshots."""
    def __init__(self, result, group='B', distance=None, model='cir_residual'):
        import matplotlib.pyplot as plt
        from matplotlib.widgets import Button, RadioButtons, Slider
        self.plt, self.result = plt, result
        self.group, self.distance, self.model, self.index = group, distance, model, 0
        self.by_key = defaultdict(dict)
        self.feature_rows = {g: [r for r in result['features'] if r['group'] == g] for g in ['A', 'B']}
        for row in result['predictions']:
            self.by_key[row['trial'], row['round_id']][row['model']] = row
        self.fig = plt.figure(figsize=(14.2, 8.2))
        self.fig.canvas.manager.set_window_title('Single-link CIR distance validation - OFFLINE')
        self.fig.text(.20, .96, 'Single-link distance validation | OFFLINE REPLAY', fontsize=15, weight='bold')
        self.fig.text(.20, .925, f"Raw CSI extracted on this run | A: {len(self.feature_rows['A'])} training sessions | "
                      f"B: {len(self.feature_rows['B'])} validation sessions", fontsize=10)
        self.scope_text = self.fig.text(.20, .485, '', fontsize=10, weight='bold')
        self.footer = self.fig.text(.20, .018, 'B is used for model selection, not final independent testing. '
                                   'Negative / out-of-domain predictions are retained.', fontsize=8.8)
        self.trace = self.fig.add_axes([.20, .59, .46, .28])
        self.scatter = self.fig.add_axes([.735, .59, .235, .28])
        self.overall = self.fig.add_axes([.20, .19, .46, .235])
        self.sample = self.fig.add_axes([.735, .19, .235, .235])
        self.split_radio = RadioButtons(self.fig.add_axes([.025, .80, .14, .11]), ['A: training', 'B: validation'],
                                         active=0 if group == 'A' else 1)
        self.fig.text(.025, .93, 'Data group', fontsize=10, weight='bold')
        self.distance_choices = ['All'] + [f'{d:g} m' for d in sorted({r['true_distance_m'] for r in result['features']})]
        active = 0 if distance is None else self.distance_choices.index(f'{distance:g} m')
        self.distance_radio = RadioButtons(self.fig.add_axes([.025, .49, .14, .275]), self.distance_choices, active=active)
        self.fig.text(.025, .775, 'View true distance', fontsize=10, weight='bold')
        self.model_radio = RadioButtons(self.fig.add_axes([.025, .30, .14, .145]),
                                         [NAMES[k] for k in MODELS], active=MODELS.index(model))
        self.fig.text(.025, .455, 'Displayed model', fontsize=10, weight='bold')
        self.previous_button = Button(self.fig.add_axes([.025, .22, .066, .045]), 'Previous')
        self.next_button = Button(self.fig.add_axes([.099, .22, .066, .045]), 'Next')
        self.save_button = Button(self.fig.add_axes([.025, .14, .14, .05]), 'Save current view')
        self.fig.text(.025, .060, 'Slide to inspect sessions.\nDistances above are ground truth,\nnot model inputs.', fontsize=8.2)
        self.slider = Slider(self.fig.add_axes([.25, .085, .66, .027]), 'Session', 1,
                             max(2, len(self.filtered())), valinit=1, valstep=1)
        self.split_radio.on_clicked(self.change_group)
        self.distance_radio.on_clicked(self.change_distance)
        self.model_radio.on_clicked(self.change_model)
        self.previous_button.on_clicked(lambda _: self.slider.set_val(max(1, self.index)))
        self.next_button.on_clicked(lambda _: self.slider.set_val(min(len(self.filtered()), self.index + 2)))
        self.save_button.on_clicked(lambda _: self.save_view())
        self.slider.on_changed(self.change_session)
        self.redraw()

    def filtered(self):
        return [r for r in self.feature_rows[self.group]
                if self.distance is None or np.isclose(r['true_distance_m'], self.distance)]

    def reset_slider(self):
        count = len(self.filtered())
        if not count:
            # A custom dataset may not include each distance in both groups.
            self.distance = None
            self.distance_radio.eventson = False
            self.distance_radio.set_active(0)
            self.distance_radio.eventson = True
            count = len(self.filtered())
        self.index = 0
        self.slider.eventson = False
        self.slider.valmax = max(2, count)
        self.slider.ax.set_xlim(1, max(2, count))
        self.slider.set_val(1)
        self.slider.eventson = True

    def change_group(self, label):
        self.group = label[0]
        self.reset_slider()
        self.redraw()

    def change_distance(self, label):
        self.distance = None if label == 'All' else float(label.split()[0])
        self.reset_slider()
        self.redraw()

    def change_model(self, label):
        self.model = next(k for k in MODELS if NAMES[k] == label)
        self.redraw()

    def change_session(self, value):
        self.index = min(len(self.filtered()) - 1, max(0, int(value) - 1))
        self.redraw()

    def redraw(self):
        rows = self.filtered()
        row = rows[self.index]
        current = self.by_key[row['trial'], row['round_id']]
        selected = [self.by_key[r['trial'], r['round_id']][self.model] for r in rows]
        score = metrics(selected)
        context = 'A TRAINING FIT' if self.group == 'A' else 'B MODEL-SELECTION VALIDATION'
        scope = 'All measured distances' if self.distance is None else f'True distance {self.distance:g} m only'
        self.scope_text.set_text(f"{context} | {scope} | {len(rows)} sessions | Selected-model MAE: {score['mae_m']:.3f} m")
        ax = self.trace
        ax.clear()
        order = np.arange(1, len(rows) + 1)
        ax.plot(order, [r['ftm_distance_m'] for r in rows], color=COLORS['raw_ftm'], alpha=.7, label='Raw FTM', linewidth=1)
        ax.plot(order, [r['predicted_m'] for r in selected], color=COLORS[self.model], label=NAMES[self.model], linewidth=1.25)
        ax.plot(order, [r['true_distance_m'] for r in rows], '--', color='#C24133', label='Ground truth', linewidth=1.2)
        ax.axvline(self.index + 1, color='#111111', alpha=.5, linewidth=.8)
        ax.set(xlabel='Session order (historical data)', ylabel='Distance (m)', title='Per-session range')
        ax.legend(loc='upper left', fontsize=8)
        ax.grid(alpha=.18)
        ax = self.scatter
        ax.clear()
        full = [r for r in self.result['predictions'] if r['group'] == self.group and r['model'] == self.model]
        truth, prediction = [r['true_distance_m'] for r in full], [r['predicted_m'] for r in full]
        ax.scatter(truth, prediction, s=8, alpha=.4, color=COLORS[self.model])
        lo, hi = min(truth), max(truth)
        ax.plot([lo, hi], [lo, hi], '--', color='#333333', linewidth=1)
        ax.scatter([row['true_distance_m']], [current[self.model]['predicted_m']], s=52,
                   facecolors='none', edgecolors='#D23C2A', linewidths=1.5)
        ax.set(xlabel='Ground truth (m)', ylabel='Predicted range (m)', title=f'All {self.group} sessions: {NAMES[self.model]}')
        ax.grid(alpha=.18)
        ax = self.overall
        ax.clear()
        ax.axis('off')
        index = {(r['group'], r['model']): r for r in self.result['metrics']}
        cells = [[NAMES[k], f"{index['A', k]['mae_m']:.3f}", f"{index['B', k]['mae_m']:.3f}",
                  f"{index['B', k]['p90_abs_error_m']:.3f}"] for k in KINDS]
        table = ax.table(cellText=cells, colLabels=['Method', 'A train\nMAE (m)', 'B validation\nMAE (m)', 'B P90 (m)'],
                         colWidths=[.36, .21, .23, .20], cellLoc='center', bbox=[0, 0, 1, 1])
        table.auto_set_font_size(False)
        table.set_fontsize(9)
        for (i, j), cell in table.get_celld().items():
            cell.set_edgecolor('#D5DCE6')
            if i == 0:
                cell.set_facecolor('#E9EEF5')
            elif i == KINDS.index(self.model) + 1:
                cell.set_facecolor('#EAF4F8')
        ax.set_title('Full-data errors (all distances, independent of the view filter)', fontsize=10, pad=10)
        ax = self.sample
        ax.clear()
        ax.axis('off')
        cells = [[NAMES[k], f"{current[k]['predicted_m']:.3f}", f"{current[k]['absolute_error_m']:.3f}"] for k in KINDS]
        table = ax.table(cellText=cells, colLabels=['Method', 'Range (m)', 'Abs error (m)'],
                         colWidths=[.46, .26, .28], cellLoc='center', bbox=[0, .14, 1, .86])
        table.auto_set_font_size(False)
        table.set_fontsize(8.3)
        for (i, j), cell in table.get_celld().items():
            cell.set_edgecolor('#D5DCE6')
            if i == 0:
                cell.set_facecolor('#E9EEF5')
        ax.set_title(f"Round {row['round_id']} | True distance: {row['true_distance_m']:.3f} m", fontsize=10, pad=10)
        ax.text(0, .03, f"{row['trial']} | {current[self.model]['quality']}", fontsize=7.5, transform=ax.transAxes)
        self.fig.canvas.draw_idle()

    def save_view(self, path=None):
        row = self.filtered()[self.index]
        if path is None:
            scope = 'all' if self.distance is None else f'{self.distance:g}m'
            path = self.result['destination'] / f'view_{self.group}_{scope}_{self.model}_round{row["round_id"]}.png'
        self.fig.savefig(path, dpi=220, bbox_inches='tight')
        print('VIEW SAVED:', Path(path).resolve(), flush=True)
        return Path(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', default=str(ROOT / 'data' / 'recordings_original.zip'), help='A/B raw recording ZIP or directory')
    parser.add_argument('--out', default=str(ROOT / 'runs'), help='Parent folder for a new timestamped result directory')
    parser.add_argument('--split', choices=['A', 'B'], default='B', help='Initial display group; never changes fitting partitions')
    parser.add_argument('--distance', default='all', help='Initial display distance in metres or all; does not change fitting')
    parser.add_argument('--model', choices=MODELS, default='cir_residual')
    parser.add_argument('--rows', type=int, default=8, help='Number of individual sessions printed to the console')
    parser.add_argument('--no-window', action='store_true', help='Save CSV/PNG and exit without opening a GUI')
    args = parser.parse_args()
    try:
        distance = None if args.distance.lower() == 'all' else float(args.distance)
        if distance is not None and (not math.isfinite(distance) or distance <= 0):
            raise ValueError()
    except ValueError:
        parser.error('--distance must be all or a positive number.')
    if args.rows < 0:
        parser.error('--rows must be nonnegative.')
    if not Path(args.input).exists():
        parser.error('Input does not exist: ' + args.input)
    print('Processing original FTM + CSI recordings. This is OFFLINE replay.', flush=True)
    result = process(args.input, Path(args.out) / ('run_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f')))
    available = {r['true_distance_m'] for r in result['features'] if r['group'] == args.split}
    if distance is not None and distance not in available:
        parser.error('Distance not present in this group. Available: ' + ', '.join(f'{d:g}' for d in sorted(available)))
    console_report(result, args.split, distance, args.model, args.rows)
    import matplotlib
    if args.no_window:
        matplotlib.use('Agg')
    elif os.name == 'nt':
        matplotlib.use('TkAgg')
    import matplotlib.pyplot as plt
    try:
        viewer = DistanceViewer(result, args.split, distance, args.model)
    except Exception as exc:
        unavailable = isinstance(exc, (ImportError, RuntimeError)) or type(exc).__name__ == 'TclError'
        if args.no_window or not unavailable:
            raise
        print(f'Interactive window unavailable: {exc}', flush=True)
        print('Saving PNG and CSV instead. A regular Windows Python with Tkinter can open the window.', flush=True)
        plt.close('all')
        plt.switch_backend('Agg')
        args.no_window = True
        viewer = DistanceViewer(result, args.split, distance, args.model)
    viewer.save_view(result['destination'] / 'overview.png')
    print('\nRESULT FOLDER:', result['destination'].resolve(), flush=True)
    print('Window: choose data group/distance/model; slide sessions; Save current view exports a PNG.', flush=True)
    if args.no_window:
        plt.close(viewer.fig)
    else:
        plt.show()


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError) as exc:
        raise SystemExit('ERROR: ' + str(exc))
