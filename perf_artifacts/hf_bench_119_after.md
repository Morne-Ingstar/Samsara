# Language gate applied to the hands-free bench (queue 119)

Floor 0.9. `before` is the bench's own number (decode + segment gates); `after` adds the production language gate.

| label | n | before false-accept | after false-accept | rejected |
|---|---:|---:|---:|---:|
| nonspeech_body | 5 | 0.00 | **0.00** | 0 |
| nonspeech_room | 4 | 0.00 | **0.00** | 0 |
| silence | 3 | 0.00 | **0.00** | 0 |
| media_only | 3 | 1.00 | **0.00** | 3 |

| owner label | n | false REJECT rate | rejected clips |
|---|---:|---:|---|
| speech_owner | 6 | 0.00 | none |
| speech_owner_over_media | 3 | 0.00 | none |
| wake_over_media | 2 | 0.00 | none |

## Per clip

| label | file | probability | chars | text? | gate |
|---|---|---:|---:|---|---|
| speech_owner | speech_owner_01.wav | 0.9844 | 60 | yes | passed |
| speech_owner | speech_owner_02.wav | 0.9897 | 70 | yes | passed |
| speech_owner | speech_owner_03.wav | 0.9932 | 57 | yes | passed |
| speech_owner | speech_owner_04.wav | 0.9902 | 68 | yes | passed |
| speech_owner | speech_owner_05.wav | 0.9902 | 72 | yes | passed |
| speech_owner | speech_owner_06.wav | 0.9927 | 69 | yes | passed |
| nonspeech_body | nonspeech_body_01.wav | 0.5415 | 0 | no | passed |
| nonspeech_body | nonspeech_body_02.wav | 0.5415 | 0 | no | passed |
| nonspeech_body | nonspeech_body_03.wav | 0.5415 | 0 | no | passed |
| nonspeech_body | nonspeech_body_04.wav | 0.8247 | 0 | no | passed |
| nonspeech_body | nonspeech_body_05.wav | 0.5415 | 0 | no | passed |
| nonspeech_room | nonspeech_room_01.wav | 0.5415 | 0 | no | passed |
| nonspeech_room | nonspeech_room_02.wav | 0.5415 | 0 | no | passed |
| nonspeech_room | nonspeech_room_03.wav | 0.5415 | 0 | no | passed |
| nonspeech_room | nonspeech_room_04.wav | 0.5415 | 0 | no | passed |
| silence | silence_01.wav | 0.5415 | 0 | no | passed |
| silence | silence_02.wav | 0.5415 | 0 | no | passed |
| silence | silence_03.wav | 0.5415 | 0 | no | passed |
| media_only | media_only_01.wav | 0.8403 | 362 | yes | low_confidence |
| media_only | media_only_02.wav | 0.8862 | 516 | yes | low_confidence |
| media_only | media_only_03.wav | 0.8413 | 450 | yes | low_confidence |
| speech_owner_over_media | speech_owner_over_media_01.wav | 0.9849 | 65 | yes | passed |
| speech_owner_over_media | speech_owner_over_media_02.wav | 0.9946 | 65 | yes | passed |
| speech_owner_over_media | speech_owner_over_media_03.wav | 0.9834 | 65 | yes | passed |
| wake_over_media | wake_over_media_01.wav | 0.9819 | 35 | yes | passed |
| wake_over_media | wake_over_media_02.wav | 0.9849 | 18 | yes | passed |
