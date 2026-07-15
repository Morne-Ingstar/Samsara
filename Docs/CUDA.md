# Optional NVIDIA CUDA acceleration

The official Samsara v0.22 application archive is a verified CPU build. A
separate CUDA runtime pack can accelerate transcription on compatible NVIDIA
GPUs without changing the application executable.

## Download

Download
[Samsara-CUDA-Pack-v0.20.0.zip](https://github.com/Morne-Ingstar/Samsara/releases/download/v0.20.0/Samsara-CUDA-Pack-v0.20.0.zip).
The runtime files are unchanged and have been hash-verified against the CUDA
environment used to test v0.22.

- Size: 1,128,045,243 bytes
- SHA-256: `5dc752c89ca4e6ad777b545907a7e654471ce3dfe10a3d96bbfc705386db335d`

## Install

1. Close Samsara.
2. Open the extracted Samsara application folder.
3. Extract all ten DLLs from the CUDA pack into
   `Samsara\_internal\ctranslate2\`.
4. Start Samsara, open Settings -> Advanced, and select
   **CUDA (NVIDIA GPU)**.
5. Restart Samsara if prompted.

Do not copy only the two cuBLAS files. v0.22 checks the complete runtime set
and visibly falls back to CPU when any required DLL is missing.

To verify acceleration, open Live Log from the tray. Model startup should say
`Device: cuda, Compute: float16`.

The pack contains NVIDIA runtime libraries and is much larger than Samsara
itself. It is optional; CPU transcription remains fully supported.

The NVIDIA files remain governed by NVIDIA's applicable
[CUDA](https://docs.nvidia.com/cuda/eula/) and
[cuDNN](https://docs.nvidia.com/deeplearning/cudnn/backend/latest/reference/eula.html)
license terms; they are not relicensed under Samsara's AGPL license.
