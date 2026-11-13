#!/usr/bin/env python3
"""ESP-IDF 6.0.2 ESP32-S3: RX CSI timestamp metadata header patch (v2).

Default: READ-ONLY preview; changes only if --apply is supplied.
The ESP-IDF Wi-Fi driver's reserved-bit interpretation and timestamp formula
are *experimental*, adapted from Wi-PRO-style techniques, and must be
independently verified against FTM hardware timestamps.

This is NOT a measurement calibration and does NOT assure accurate distances.
"""
import argparse
import difflib
import re
import subprocess
import sys
from pathlib import Path

MACROS = r'''
/* EXPERIMENTAL ESP32-S3 RX-start metadata extraction.
 * Verify baseband layout, clock domain and wrap-around before relying on it.
 * Keep modem/light sleep disabled during timing validation.
 */
#if CONFIG_IDF_TARGET_ESP32S3
#define WIFI_PKT_RX_CYCDEC_ADJUST(_p) \
    (((_p)->rxstart_time_cyc_dec >= 1024U) ? \
     (2048U - (_p)->rxstart_time_cyc_dec) : (_p)->rxstart_time_cyc_dec)
#define WIFI_PKT_RX_TIMESTAMP_NSEC(_p) \
    ((uint64_t)((_p)->timestamp)*1000ULL + \
     ((uint64_t)((_p)->rxstart_time_cyc)*12500ULL)/1000ULL + \
     ((uint64_t)WIFI_PKT_RX_CYCDEC_ADJUST(_p)*1562ULL)/1000ULL - 20800ULL)
#endif
'''

# Anchored to *neighboring fields*, not literal padding or comment spacing.
RX8 = re.compile(
    r'(?P<a>^[ \t]*unsigned[ \t]+secondary_channel[ \t]*:[ \t]*4;[^\r\n]*\r?\n)'
    r'(?P<b>^[ \t]*unsigned[ \t]*:[ \t]*8;[ \t]*/\*\*<[ \t]*reserved[ \t]*\*/[ \t]*\r?\n)'
    r'(?P<c>^[ \t]*unsigned[ \t]+timestamp[ \t]*:[ \t]*32;)',
    re.M,
)
RX31 = re.compile(
    r'(?P<a>^[ \t]*unsigned[ \t]*:[ \t]*31;[ \t]*/\*\*<[ \t]*reserved[ \t]*\*/[ \t]*\r?\n)'
    r'(?P<b>^[ \t]*unsigned[ \t]+ant[ \t]*:[ \t]*1;)',
    re.M,
)

def patch_text(original: str) -> str:
    if 'WIFI_PKT_RX_TIMESTAMP_NSEC' in original:
        raise ValueError('Existing timestamp macro detected; not modifying.')
    if len(RX8.findall(original)) != 1:
        raise ValueError('Expected ONE reserved 8-bit field between secondary_channel and timestamp.')
    if len(RX31.findall(original)) != 1:
        raise ValueError('Expected ONE reserved 31-bit field immediately before ant.')
    if original.count('} wifi_pkt_rx_ctrl_t;') != 1:
        raise ValueError('Could not uniquely identify wifi_pkt_rx_ctrl_t.')

    nl = '\r\n' if '\r\n' in original else '\n'

    def sub8(m):
        indent = re.match(r'[ \t]*', m.group('b')).group()
        return (m.group('a')
             + indent + 'unsigned rxstart_time_cyc: 7; /**< Experimental RX-start cycle bits */' + nl
             + indent + 'unsigned : 1; /**< reserved */' + nl
             + m.group('c'))

    result = RX8.sub(sub8, original, count=1)
    def sub31(m):
        indent = re.match(r'[ \t]*', m.group('a')).group()
        return (indent + 'unsigned : 20; /**< reserved */' + nl
             + indent + 'unsigned rxstart_time_cyc_dec: 11; /**< Experimental RX-start fractional cycles */' + nl
             + m.group('b'))

    result = RX31.sub(sub31, result, count=1)
    return result.replace('} wifi_pkt_rx_ctrl_t;',
                          '} wifi_pkt_rx_ctrl_t;' + MACROS.replace('\n', nl), 1)

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--idf-path', required=True)
    p.add_argument('--apply', action='store_true', help='Modify IDF header after inspecting preview')
    a = p.parse_args()
    root = Path(a.idf_path).expanduser().resolve()
    h = root / 'components' / 'esp_wifi' / 'include' / 'local' / 'esp_wifi_types_native.h'
    if not h.is_file():
        sys.exit('STOP: Header not found: ' + str(h))
    try:
        original = h.read_text(encoding='utf-8')
        modified = patch_text(original)
    except (OSError, ValueError) as e:
        sys.exit('STOP: No changes: ' + str(e))

    # Refuse mismatched checkouts, but do not require git when ESP-IDF was
    # installed from an archive without .git metadata.
    if (root / '.git').exists():
        git = subprocess.run(['git', '-C', str(root), 'describe', '--tags', '--always'],
                             text=True, capture_output=True)
        if git.returncode == 0 and not git.stdout.strip().startswith('v6.0.2'):
            sys.exit('STOP: Unexpected ESP-IDF git version: ' + git.stdout.strip())

    print('ESP-IDF header:', h)
    print(''.join(difflib.unified_diff(original.splitlines(True), modified.splitlines(True),
                                      fromfile='original', tofile='proposed')))
    if not a.apply:
        print('\nDRY RUN: No files changed. Do NOT add --apply until the diff is reviewed.')
        return
    back = h.with_name(h.name + '.qar_backup')
    if back.exists():
        sys.exit('STOP: Backup already exists: ' + str(back))
    back.write_text(original, encoding='utf-8')
    h.write_text(modified, encoding='utf-8')
    print('Patched header. Backup:', back)
    print('IMPORTANT: This only exposes experimental timestamp bits. Validate them before ToF correction.')

if __name__ == '__main__':
    main()
