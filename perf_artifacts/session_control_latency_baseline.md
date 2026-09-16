| Stage | n | median ms | p95 ms | min | max |
|---|---|---|---|---|---|
| VAD/endpoint wait (silence after the word) | 21 | 670 | 768 | 570 | 788 |
| buffer assembly + Whisper lock wait | 21 | 1 | 292 | 0 | 329 |
| Whisper decode of the end word | 21 | 144 | 339 | 131 | 341 |
| speech gate | 21 | 2 | 3 | 1 | 3 |
| commit re-decode of whole session | 19 | 364 | 964 | 141 | 2328 |
| smart corrections + formatting + paste | 21 | 318 | 336 | 303 | 349 |
| after paste (bookkeeping, not visible) | 21 | 14 | 34 | 8 | 38 |
| capture closed -> Ctrl+V | 21 | 831 | 1415 | 485 | 2804 |
| speech offset -> [PASTE] logged (after clipboard restore) | 21 | 1541 | 2177 | 1213 | 3404 |
| SPEECH OFFSET -> TEXT ON SCREEN (Ctrl+V, est. [PASTE] - 239 ms) | 21 | 1302 | 1938 | 974 | 3165 |
