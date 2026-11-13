# v5 frame-match validation status

- C Tag source: changed (802.11 Public Action/FTM response type + dialog/followup token extraction); ESP-IDF 6.0.2 hardware compiler not available in this container. **NOT binary-verified or flashed**.
- Anchor source: unchanged from v4, no need to reflash if running normally.
- ESP-IDF optional patch: safe dry-run + backup, adapted to observed v6.0.2 public native header; untested on hardware. Does not guarantee accurate ToF.
- Offline Python: syntax checked; parser synthetic test and token/clock matching synthetic test passed.
- Matching: action33 frame identity; clock-offset coherence is preliminary only; corrected distance is intentionally not fabricated.
- Original v4 data from probe time windows cannot be retroactively treated as same FTM-frame CSI.
