# Speaker verification feasibility

Run: 2026-09-10T19:31:34.636591+00:00; seed=20260910; CPU provider, one inference thread; torch absent.

Model: [wespeaker_en_voxceleb_CAM++.onnx](https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/wespeaker_en_voxceleb_CAM%2B%2B.onnx), 29292684 bytes.
File: `C:\Users\Morne\Projects\Samsara-dev\perf_artifacts\sv_models\wespeaker_en_voxceleb_CAM++.onnx`; SHA256: `c46fad10b5f81e1aa4a60c162714208577093655076c5450f8c469e522ec54ef`.

## Leave-one-out enrollment

- Fold 1 (speech_owner_01.wav): 0.793467; VAD retained=True
- Fold 2 (speech_owner_02.wav): 0.673577; VAD retained=True
- Fold 3 (speech_owner_03.wav): 0.679840; VAD retained=True
- Fold 4 (speech_owner_04.wav): 0.835944; VAD retained=True
- Fold 5 (speech_owner_05.wav): 0.788111; VAD retained=True
- Fold 6 (speech_owner_06.wav): 0.695371; VAD retained=True

## Metrics

Zero-media-FA threshold: `0.66525790305050625` (accept >= threshold).

| Condition | Kept/total | VAD dropped | Mean | Min | Max | SV FRR / FAR |
|---|---:|---:|---:|---:|---:|---:|
| clean_LOO_whole | 6/6 | 0 | 0.7444 | 0.6736 | 0.8359 | 0.0% FRR |
| speech_owner_over_media_whole | 3/3 | 0 | 0.7464 | 0.6735 | 0.8688 | 0.0% FRR |
| wake_over_media_whole | 0/2 | 2 | n/a | n/a | n/a | n/a |
| clean_0.8s | 29/43 | 14 | 0.2597 | 0.1449 | 0.3925 | 100.0% FRR |
| clean_1.2s | 19/28 | 9 | 0.1950 | 0.1145 | 0.2734 | 100.0% FRR |
| clean_2.0s | 11/16 | 5 | 0.4853 | 0.3825 | 0.6517 | 100.0% FRR |
| clean_3.0s | 8/9 | 1 | 0.7074 | 0.6212 | 0.7788 | 25.0% FRR |
| overlap_+6dB | 12/16 | 4 | 0.5766 | 0.4797 | 0.6629 | 100.0% FRR |
| overlap_+0dB | 12/16 | 4 | 0.5927 | 0.5486 | 0.6366 | 100.0% FRR |
| overlap_-6dB | 12/16 | 4 | 0.5873 | 0.4900 | 0.6664 | 91.7% FRR |
| overlap_-12dB | 11/16 | 5 | 0.5258 | 0.4006 | 0.6262 | 100.0% FRR |
| duck_whole_2.0s | 12/16 | 4 | 0.5385 | 0.4206 | 0.6729 | 91.7% FRR |
| duck_post_500ms | 12/16 | 4 | 0.4435 | 0.3741 | 0.4945 | 100.0% FRR |
| nonspeech_body_2.0s | 0/10 | 10 | n/a | n/a | n/a | n/a |
| nonspeech_room_2.0s | 0/8 | 8 | n/a | n/a | n/a | n/a |
| silence_2.0s | 0/28 | 28 | n/a | n/a | n/a | n/a |
| media_only_2.0s | 13/45 | 32 | 0.5410 | 0.4626 | 0.6653 | 0.0% FAR |

Pooled EER: 56.46% (147 genuine, 13 impostor scores).
VAD dropped: 56 genuine and 78 impostor trials.

VERDICT: At zero observed media false accepts (threshold=0.66525790305050625), owner SV false-reject rates after VAD are: 1.2 s clean=100.0% (19/19); 2.0 s at 0 dB SIR=100.0% (12/12); duck-simulated post-500 ms=100.0% (12/12).

## CPU embedding latency

- 0.8 s: p50=31.21 ms, p95=86.19 ms; n=145 across 29 windows
- 3.0 s: p50=104.88 ms, p95=148.10 ms; n=40 across 8 windows

## Interpretation limits

Full enrollment reuses the clean clips sliced/mixed for evaluation, so these scores are optimistic. Zero FA is observed on this small corpus only; not a deployment guarantee. Overlap/duck pairs share a seeded media slice. No source separation or augmentation beyond the requested mixing.

Silero v6; complete 512-sample frames; probability > 0.5; retain >= 50% voiced frames; no audio trimming.

SV FRR/FAR and EER exclude VAD drops; combined genuine rejection including VAD is in each metrics row.

create stream + features + embedding compute, CPU one thread; excludes VAD/I/O/loading; three warmups then repeated retained clean windows.

The three verdict rates measure false rejection of retained owner trials; any VAD rejection is reported separately. A high rate in any of them argues against using this model as a hard pre-decode gate. The duck comparison changes both interference and analyzed duration; it cannot by itself prove a discontinuity-specific failure.
