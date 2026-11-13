#!/usr/bin/env python3
"""Optional ESP-IDF v6.0.2/S3 RX-start metadata header patch.
This is not required for action-frame identity diagnostics.
It does not by itself establish clock synchronization or accuracy.
Uses reserved-bit interpretations similar to Wi-PRO's public Apache-2.0 patch.
"""
import argparse
import difflib
import subprocess
from pathlib import Path

MACRO = '''
/* Optional sub-microsecond RX-start timestamp metadata for ESP32-S3.
 * Only valid for OFDM, modem sleep and light sleep disabled.
 */
#if CONFIG_IDF_TARGET_ESP32S3
#define WIFI_PKT_RX_CYCDEC_ADJUST(_p) \\
    (((_p)->rxstart_time_cyc_dec >= 1024U) ? \\
     (2048U - (_p)->rxstart_time_cyc_dec) : (_p)->rxstart_time_cyc_dec)
#define WIFI_PKT_RX_TIMESTAMP_NSEC(_p) \\
    ((uint64_t)((_p)->timestamp)*1000ULL + \\
     ((uint64_t)((_p)->rxstart_time_cyc)*12500ULL)/1000ULL + \\
     ((uint64_t)WIFI_PKT_RX_CYCDEC_ADJUST(_p)*1562ULL)/1000ULL - 20800ULL)
#endif
'''

def patch_text(original):
    # Exact locations in native wifi_pkt_rx_ctrl_t in v6.0.2.
    marker='    unsigned secondary_channel: 4; /**< secondary channel'
    p=original.find(marker)
    if p<0:raise ValueError('secondary_channel not found')
    prefix,tail=original[:p],original[p:]
    reserved8='    unsigned : 8; /**< reserved */'
    if reserved8 not in tail[:350]:raise ValueError('Reserved 8-bit field not found near channel')
    tail=tail.replace(reserved8,
        '    unsigned rxstart_time_cyc: 7; /**< RX start cycles */\n'
        '    unsigned : 1; /**< reserved */',1)
    s=prefix+tail
    reserved31='    unsigned : 31; /**< reserved */'
    if s.count(reserved31)!=1:raise ValueError('31-bit reserved metadata not unique')
    s=s.replace(reserved31,
        '    unsigned : 20; /**< reserved */\n'
        '    unsigned rxstart_time_cyc_dec: 11; /**< RX start fine cycles */',1)
    m='} wifi_pkt_rx_ctrl_t;'
    if s.count(m)!=1:raise ValueError('wifi_pkt_rx_ctrl_t missing/not unique')
    return s.replace(m,m+MACRO,1)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--idf-path',required=True)
    ap.add_argument('--apply',action='store_true')
    args=ap.parse_args()
    root=Path(args.idf_path)
    p=root/'components/esp_wifi/include/local/esp_wifi_types_native.h'
    if not p.exists():raise SystemExit('Not found: '+str(p))
    original=p.read_text(encoding='utf-8')
    if 'WIFI_PKT_RX_TIMESTAMP_NSEC' in original:
        raise SystemExit('Existing macro found; no changes made')
    try: modified=patch_text(original)
    except ValueError as e:raise SystemExit('Wrong source version? No changes: '+str(e))
    tag=subprocess.run(['git','-C',str(root),'describe','--tags','--always'],capture_output=True,text=True)
    if tag.returncode==0 and not tag.stdout.strip().startswith('v6.0.2'):
        raise SystemExit('STOP: expected v6.0.2, got '+tag.stdout.strip())
    print(''.join(difflib.unified_diff(original.splitlines(True),modified.splitlines(True),fromfile=str(p),tofile=str(p)+' [modified]')))
    if not args.apply:
        print('DRY RUN only. Add --apply after review.');return
    bak=p.with_suffix(p.suffix+'.qar_backup')
    if bak.exists():raise SystemExit('Backup already exists: '+str(bak))
    bak.write_text(original,encoding='utf-8')
    p.write_text(modified,encoding='utf-8')
    print('Patch applied. Original backup:',bak)
if __name__=='__main__':main()
