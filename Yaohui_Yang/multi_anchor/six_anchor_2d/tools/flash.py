#!/usr/bin/env python3
"""Build and flash ESP32-S3 in the existing ESP-IDF 6.0.2 PowerShell terminal.

Each anchor ID, TAG burst profile and UART baud have isolated build/config paths.
The IDF-managed Python is used even if an analysis (venv) is currently active.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def idf_python():
    environment = os.environ.get('IDF_PYTHON_ENV_PATH')
    if environment:
        for relative in ['Scripts/python.exe', 'bin/python']:
            candidate = Path(environment)/relative
            if candidate.is_file(): return str(candidate)
    return sys.executable


def plan(root, role, aid, port, baud, burst, idf_path, python, monitor=True):
    project = Path(root)/role
    name = f'{aid}_{baud}' if role=='anchor' else f'MULTI_{baud}_burst{burst}'
    defaults = project/('sdkconfig.'+name+'.defaults')
    cmake = ['-DIDF_TARGET=esp32s3', '-DSDKCONFIG='+str(project/('sdkconfig.'+name)),
             '-DSDKCONFIG_DEFAULTS='+str(defaults)]
    cmake += ['-DLAB_ANCHOR_NUMBER='+aid[1:]] if role=='anchor' else ['-DFTM_BURST_PERIOD='+str(burst)]
    base = [python, str(Path(idf_path)/'tools/idf.py'), '-B', str(project/('build_'+name))]+cmake
    return project, defaults, [base+['build'], base+['-p',port,'flash']+(['monitor'] if monitor else [])]


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--role',choices=['anchor','tag'],required=True)
    p.add_argument('--id',choices=[f'A{i}' for i in range(1,7)])
    p.add_argument('--port',required=True)
    p.add_argument('--baud',type=int,choices=[115200,230400,460800,921600])
    p.add_argument('--burst-period',type=int,choices=[0,1,2],default=2,
                   help='2 preserves the working single-link request; 0 means no preference, not guaranteed ASAP.')
    p.add_argument('--no-monitor',action='store_true')
    p.add_argument('--dry-run',action='store_true')
    args=p.parse_args()
    if args.role=='anchor' and not args.id: p.error('Anchor requires --id A1..A6.')
    if args.role=='tag' and args.id: p.error('Multi-anchor TAG has no individual --id.')
    idf=os.environ.get('IDF_PATH')
    if not args.dry_run and (not idf or not (Path(idf)/'tools/idf.py').is_file()):
        p.error('Open IDF_v6.0.2_Powershell. IDF_PATH/tools/idf.py was not found.')
    baud=args.baud or (115200 if args.role=='anchor' else 921600)
    project,defaults,commands=plan(ROOT,args.role,args.id,args.port,baud,args.burst_period,
                                   idf or 'IDF_PATH',idf_python(),not args.no_monitor)
    text=(project/'sdkconfig.defaults').read_text()
    default_baud=115200 if args.role=='anchor' else 921600
    text=text.replace(f'CONFIG_ESP_CONSOLE_UART_BAUDRATE={default_baud}',f'CONFIG_ESP_CONSOLE_UART_BAUDRATE={baud}')
    print(f"{args.role.upper()} {args.id or 'A1-A6 polling'} | UART baud {baud}")
    if args.role=='tag': print(f'32 FTM frames/link; burst_period={args.burst_period}; six links measured sequentially.')
    if not args.dry_run: defaults.write_text(text)
    for command in commands:
        print(subprocess.list2cmdline(command),flush=True)
        if not args.dry_run:
            environment=os.environ.copy()
            environment['IDF_MONITOR_BAUD']=str(baud)
            subprocess.run(command,cwd=project,check=True,env=environment)
    if args.dry_run: print('DRY RUN: no build files changed; nothing flashed.')


if __name__=='__main__': main()
