from pathlib import Path
import csv
import tempfile
from record_serial_2min import parse_measurement, FRAME_HEAD, DETAIL_HEAD
from pair_ftm_action33 import analyze
from patch_idf_6_0_2_rx_timestamp import patch_text

def writecsv(path,fields,rows):
    with path.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)

def run():
    line='CSI_FTM_FRAME,11,345,345001,1,aa:bb:cc:dd:ee:ff,8,7,800,1234,0,-26,128,abcdeeff'
    typ,row=parse_measurement(line,2)
    assert typ=='CSI_FTM_FRAME' and row[5:7]==[8,7],row
    d=[{'round_id':11,'entry_idx':i,'dialog_token':i,'rtt_ps':20000,'rssi_dbm':-30,
        't1_ps':0,'t2_ps':200_000+i*100000,'t3_ps':0,'t4_ps':0} for i in range(5)]
    f=[{'round_id':11,'callback_cpu_time_us':30+i,'hw_rx_time_ns':1000+i*100,
        'hw_timestamp_precise':1,'src_mac':'aa:bb:cc:dd:ee:ff','dialog_token':i,'followup_token':0,
        'mac_sequence_control':100+i,'rx_seq':i,'cwb':0,'rssi_dbm':-30,'csi_len_bytes':128,
        'csi_hex':'abcd'} for i in range(5)]
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        writecsv(root/'ftm_detail.csv',DETAIL_HEAD,d)
        writecsv(root/'csi_ftm_frame.csv',FRAME_HEAD,f)
        out=analyze(root)
        assert out['TOKEN_UNIQUE_TIMING_COHERENT_PRELIMINARY']==5,out
    sample='''    unsigned secondary_channel: 4; /**< secondary channel */
    unsigned : 8; /**< reserved */
    unsigned timestamp: 32;
    unsigned : 31; /**< reserved */
} wifi_pkt_rx_ctrl_t;
'''
    x=patch_text(sample)
    assert 'rxstart_time_cyc_dec' in x and 'WIFI_PKT_RX_TIMESTAMP_NSEC' in x
    print('V5 offline checks passed')
if __name__=='__main__':run()
