#!/usr/bin/env python3
"""Build/flash one numbered Anchor, or one explicitly selected single-link Tag.

Run in an ESP-IDF 6.0.2 PowerShell terminal. Each role/ID has its own build and
sdkconfig, so flashing A2 cannot silently reuse A1's compiled number.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys


def commands(root, role, anchor_id, port, idf_path, monitor=True):
    project = Path(root) / role
    build = project / ('build_numbered_' + anchor_id)
    config = project / ('sdkconfig.numbered_' + anchor_id)
    base = [sys.executable, str(Path(idf_path) / 'tools' / 'idf.py'),
            '-B', str(build), '-DIDF_TARGET=esp32s3',
            '-DSDKCONFIG=' + str(config),
            '-DSDKCONFIG_DEFAULTS=' + str(project / 'sdkconfig.numbered_uart.defaults'),
            '-DLAB_ANCHOR_NUMBER=' + anchor_id[1:]]
    return project, [base + ['build'], base + ['-p', port, 'flash'] + (['monitor'] if monitor else [])]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--role', choices=['anchor', 'tag'], default='anchor')
    parser.add_argument('--id', choices=[f'A{i}' for i in range(1, 7)], required=True)
    parser.add_argument('--port', required=True, help='The UART/CP210x port of the connected board')
    parser.add_argument('--no-monitor', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    idf_path = os.environ.get('IDF_PATH')
    if not idf_path and not args.dry_run:
        parser.error('Open the ESP-IDF PowerShell terminal first; IDF_PATH is not set.')
    idf_path = idf_path or '<IDF_PATH>'
    if not args.dry_run and not (Path(idf_path) / 'tools' / 'idf.py').is_file():
        parser.error('IDF_PATH/tools/idf.py does not exist.')
    cwd, calls = commands(root, args.role, args.id, args.port, idf_path, not args.no_monitor)
    print(f'{args.role.upper()} {args.id}: SSID=RTT_CIR_{args.id}; UART0=115200', flush=True)
    if args.role == 'tag':
        print('Single-link Tag: this build measures only the selected Anchor.', flush=True)
    print('Connect the intended physical board and check ANCHOR_READY / TARGET_READY after flashing.', flush=True)
    for command in calls:
        print(subprocess.list2cmdline(command), flush=True)
        if not args.dry_run:
            subprocess.run(command, cwd=cwd, check=True)


if __name__ == '__main__':
    main()
