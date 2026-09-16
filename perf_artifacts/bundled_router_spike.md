# Can Samsara ship its own small model, so Ollama is never required?

Queue 94 — feasibility spike, 2026-09-15. Measurement only: nothing in `samsara/` changed,
no dependency was added, and the app was not rebuilt.

Raw data: `perf_artifacts/bundled_router_spike.json`.
Branch checked out while measuring: **`feature/v0.22`**, HEAD `eeebf68` (not switched).

---

## 0. The brief's premises, checked against the tree first

| Claim in the brief | Verdict | Evidence |
|---|---|---|
| "tier-3 command resolution … needs Ollama" | **STALE** | `samsara/intent/resolve.py:1-22`: `TIERS = ("exact", "grammar", "similarity")`. Tier 3 is order-free Dice token overlap over catalog aliases — pure Python, no network, no LLM. Ollama appears nowhere in `samsara/intent/`. |
| Ava needs Ollama | CONFIRMED | `ava_command_session.py`, `ava_readiness.py`, `cloud_llm.py`, `premium.py`, `smart_actions_tools.py` |
| The grammar tier from the decision shipped (queue 25) | CONFIRMED | `samsara/intent/grammar.py`, commit `3c2265f` "deterministic tier-2 grammar over the command catalog (not wired)" |
| The bundled-model half was never built | CONFIRMED | no model loader anywhere in `samsara/`; no GGUF/ONNX router asset in the spec or the manifest |
| `commands_catalog.json` holds the canonical forms | CONFIRMED | 486 records; 440 after dropping destructive, argument-required and whole-utterance commands |
| A shadow log of real utterances exists | CONFIRMED | `~/.samsara/shadow/` — 1371 lines over two days, 1241 unique texts |
| "the CPU ZIP is ~292 MB" | **STALE by 268 MB** | 292.4 MB is **v0.20.0** (`release_staging/`). Current `release/Samsara-Windows-v0.23.0-beta.1.zip` = **560.3 MB**; `dist/Samsara` unpacked = 1387 MB. `RELEASING.md:163` already flags the 292 MB figure as no longer reflecting the tree. |
| `samsara/components.py`, commit `ffa0224` | CONFIRMED | 16 528 bytes; "feat(release): manifest of downloadable components + verified component fetcher" |
| "the design decision recorded on 2026-09-12"; "the Move A tribunal" | **NOT VERIFIABLE IN TREE** | Neither is a document in the repo. `Docs/INTENT_GRAMMAR.md:10` references "Move A's intent-gate shadow inside DICTATE", which is consistent, but the decision and the tribunal themselves live in conversation history. The *substance* — that false positives are the halt condition — is corroborated independently by the shadow data below. |

### One scope correction that changes the work

The brief scopes the bundled model as "the **tier-3** command resolver". Tier 3 already exists
and is not an LLM. A bundled model would be a **tier 4** — reached only after exact, grammar
*and* similarity have all declined. That is what I measured, and it is what sets the eval set:
the candidate utterances are the ones tiers 1–3 let through to dictation.

### And one fact that reframes the whole question

`samsara.intent.resolve` is imported from **exactly one place in the app**: `dictation.py:8323`,
inside `_intent_shadow_observe`, which runs on a background worker *after* dispatch has already
returned and whose only side effect is a JSONL file. `resolve.py`'s own docstring says it:
*"NOT wired into dispatch."*

**There is no live tier-3 seam for a tier 4 to sit behind.** The entire intent stack is an
observer. Nothing in the app acts on what it decides.

---

## 1. Candidates

Four, all in the 0.13B–1.5B range, all GGUF, all CPU-only through llama.cpp in-process
(no service, no daemon, no port). Two obvious candidates were dropped on licence before any
measurement — see §5.

| | SmolLM2-135M | SmolLM2-360M | Qwen2.5-0.5B | Qwen2.5-1.5B |
|---|---|---|---|---|
| Quantization | Q8_0 | Q8_0 | Q4_K_M | Q4_K_M |
| **1. Size on disk** | **138.1 MB** | **368.5 MB** | **468.6 MB** | **1065.6 MB** |
| …as % of the 560.3 MB ZIP | +23.7% | +65.8% | +83.6% | +190.2% |
| **2. Cold load** (warm file cache) | 0.09 s | 0.12 s | 0.27 s | 0.70 s |
| **2. Latency p50 / p95** | 68 / 85 ms | 148 / 188 ms | 152 / 187 ms | 189 / 225 ms |
| **3. Recall** (best framing) | 70.7% | 71.3% | 62.0% | 69.3% |
| **3. False positives** (best framing) | **81.00%** | **8.75%** ⚠ | **69.00%** | **12.00%** |
| **4. Resident memory** | 207 MB | 474 MB | 540 MB | 1695 MB |
| **5. Licence** | Apache-2.0 | Apache-2.0 | Apache-2.0 | Apache-2.0 |
| **6. Packaging** | PyInstaller or fetcher | PyInstaller or fetcher | PyInstaller or fetcher | fetcher only |

⚠ SmolLM2-360M's 8.75% is not a good operating point — it is a nearly-degenerate "always
answer 0" collapse that also drops recall to **8.0%**. See §4.

Quantized GGUF is essentially incompressible — measured over a 24 MB sample, Q8_0 compresses
to 0.961 and Q4_K_M to 0.999 — so a release ZIP grows by **the whole file size**, not less.

---

## 2. How the eval set was built

**Negatives — real, 1147 of them.** Every unique utterance in `~/.samsara/shadow/` that the
live stack sent to dictation. Re-checked against a resolver built over `commands_catalog.json`:
0 of 1147 resolve, so every one genuinely reaches tier 4. Each must answer "none".

**Positives — synthetic, 150 of them.** The shadow log contains almost no *missed* commands to
hand-label: over two days of real use the owner was dictating prose, not issuing commands the
stack fumbled. So positives were generated by paraphrasing the 440 safe catalog commands
(politeness wrappers, filler prefixes, trailing disfluency, verb synonyms — the shapes the
shadow log actually shows), then **filtered through the live resolver**, keeping only the 229
that tiers 1–3 miss. 150 were sampled from those. Verb-phrase wrappers are applied only to
phrases starting with a real imperative verb; a first pass without that rule produced junk like
*"i want to apostrophe"*.

**Shortlist.** Each item also carries the top-8 candidates tier 3's similarity ranking produces —
the list a tier-4 model would realistically be handed.

> **Privacy.** The negatives are the owner's own dictation. `perf_artifacts/` is inside the git
> repo, so the JSON beside this report carries, for each negative, only an id, word/character
> count and an 8-hex SHA-1 prefix — never the text. The full-text eval set stays in
> `D:\samsara_spike94\data\`. Synthetic positives carry their text; nobody spoke them.

### What the shadow log says before any model is involved

Of the 1371 logged utterances, the live stack *would* have fired a command on 15 and offered a
suggestion on 79. **All 15 are false positives.** A sample, verbatim from the log:

| utterance | would have fired |
|---|---|
| `To, um...` | `window_cube.two` |
| `the light weight?` | `hyperion_lights.lights` |
| `take a look at that, because...` | `health_tracker.took` |
| `Get rid of the demo.` | `windows.bring` |
| `Run it hands-free.` | `app_verbs.open` |
| `Could you copy that, please?` | `builtin.copy` (confidence 1.0) |

The 79 suggestions are the same story (`And...` → `screen_gif.stop_recording`). This is shadow
mode doing exactly its job — and it says the failure mode on real data is **not** missed
commands. It is firing on prose. Any tier-4 proposal has to be judged against that, not against
a recall target.

### The ceiling, with no model at all

| measurement | value |
|---|---|
| tier-3's top-1 is already the correct command, on the positives | **108/150 = 72.0%** |
| tier-3's top-8 contains the correct command (design B's ceiling) | 143/150 = 95.3% |
| items where tier 3 ranks *nothing* → automatic "none", no model call | **183/1297 = 14.1%** |

So a tier-4 model's entire upside is at most **+23 points on a 150-item synthetic positive set**.
Its downside is firing on any of 1147 real dictation utterances.

---

## 3. The task, and how it was measured

Utterance plus the eight tier-3 candidates; answer one digit, `1`–`8` to pick a candidate, `0`
for "this is dictation". Output is **grammar-constrained** (`root ::= [0-8]`), so a malformed
answer cannot be miscounted as a wrong one, and every model is prompted through **its own chat
template**. Greedy decoding, seed 94, 8 threads on a 24-core / 31.8 GB host.

Two system-prompt framings were measured separately — `neutral` ("pick the command, or 0") and
`guarded` ("most utterances are dictation; answer 0 for conversation, questions, fragments and
filler") — because reporting one number from one wording would be cherry-picking.

Design B numbers below use 150 positives + **400** negatives (not all 1147): the first full pass
was on track to consume most of the time budget and was re-scoped so the threshold sweep in §4
could run. At 12% the 95% confidence interval on 400 negatives is about ±3.2 points.

### Results

| model | framing | cold s | RSS MB | p50 ms | p95 ms | recall % | false positives |
|---|---|---|---|---|---|---|---|
| SmolLM2-135M-Q8_0 | guarded | 0.09 | 207 | 68 | 85 | 66.0 | 324/400 = **81.00%** |
| SmolLM2-135M-Q8_0 | neutral | 0.10 | 221 | 65 | 82 | 70.7 | 335/400 = **83.75%** |
| SmolLM2-360M-Q8_0 | guarded | 0.12 | 474 | 148 | 188 | 71.3 | 289/400 = **72.25%** |
| SmolLM2-360M-Q8_0 | neutral | 0.11 | 472 | 143 | 178 | 8.0 | 35/400 = **8.75%** |
| Qwen2.5-0.5B-Q4_K_M | guarded | 0.27 | 540 | 152 | 187 | 61.3 | 276/400 = **69.00%** |
| Qwen2.5-0.5B-Q4_K_M | neutral | 0.29 | 538 | 145 | 182 | 62.0 | 302/400 = **75.50%** |
| Qwen2.5-1.5B-Q4_K_M | guarded | 0.70 | 1695 | 189 | 225 | 68.7 | 60/400 = **15.00%** |
| Qwen2.5-1.5B-Q4_K_M | neutral | 0.71 | 1691 | 187 | 224 | 69.3 | 48/400 = **12.00%** |

The best point anywhere in that table is Qwen2.5-1.5B at **69.3% recall and 12.00% false
positives** — about one in eight ordinary dictation utterances triggering a command.

Look at the two SmolLM2-360M rows: 72.25% FP under one wording, 8.75% under the other, with
recall collapsing from 71.3% to 8.0%. **The prompt's wording moves the operating point further
than the utterance does.** That is the finding in one row: these models are not deciding, they
are defaulting, and which way they default is set by the instruction, not the input.

---

## 4. Is there *any* operating point that works?

Two framings are two points, not a frontier. A model can be badly calibrated at its argmax and
still be separable at some other threshold — so the decisive test is to ask the model a single
binary question (*instruction to the computer, or speech to be typed?*, no candidate list at
all), read its own log-probabilities for the two answer tokens, and sweep `P(command)` across
every threshold. AUC 0.5 would mean the score carries no information whatsoever.

Run on the two candidates with any chance of passing, over 150 positives and 600 negatives.

| | Qwen2.5-0.5B-Q4_K_M | Qwen2.5-1.5B-Q4_K_M |
|---|---|---|
| **AUC** | 0.7181 | **0.9442** |

**That is the one genuinely encouraging number in this spike, and it deserves saying clearly:
as a binary gate, Qwen2.5-1.5B separates commands from dictation well.** AUC 0.944 is not
noise, and it is far better than the same model looked as a shortlist router in §3. The
architecture matters more than the model: asking "is this an instruction?" is a question a 1.5B
model can answer; asking "which of these eight commands is it?" is not.

Then the trade-off curve, which is where it ends.

| false-positive budget | Qwen2.5-0.5B recall | **Qwen2.5-1.5B recall** |
|---|---|---|
| 0.17% (1 in 600) | 6.0% | 2.7% |
| 0.5% | 6.7% | 6.0% |
| 1% | 7.3% | **10.0%** |
| 2% | 12.0% | 32.7% |
| 3% | 13.3% | 50.7% |
| 5% (1 in 20) | 15.3% | **62.0%** |
| 10% | 27.3% | 86.0% |

At a false-positive rate low enough to be safe — 1 in 500-ish — the best model in the spike
recovers **under 3% of the commands tiers 1-3 miss**. It would be a feature that does nothing.
To get useful recall out of it you have to accept **one ordinary sentence in twenty firing a
command**.

### The base rate is what actually kills it

There is a harder version of this argument that does not depend on choosing a threshold at all.

Across 1371 real utterances over two days I could not find a clear case of a command tiers 1-3
missed — that is precisely why §2's positives had to be synthesised. So the base rate of
tier-4-recoverable commands in real use is **approximately zero**, while the base rate of
ordinary dictation reaching tier 4 is essentially 100%.

Run the numbers on the owner's actual two days: at the 5%/62% operating point, a tier-4 gate
would have fired on about **57 of the 1147 dictation utterances** and recovered roughly **none**,
because there was nothing there to recover. Precision would be close to zero however good the
classifier is. A model with AUC 0.944 still loses to a base rate that lopsided.

---

## 5. Licensing

Read from the live Hugging Face model cards during the spike, not from memory.

| model | licence | gated | redistributable inside an AGPL-3.0 app? |
|---|---|---|---|
| SmolLM2-135M-Instruct (+GGUF) | `apache-2.0` | no | **Yes** |
| SmolLM2-360M-Instruct (+GGUF) | `apache-2.0` | no | **Yes** |
| Qwen2.5-0.5B-Instruct (+GGUF) | `apache-2.0` | no | **Yes** |
| Qwen2.5-1.5B-Instruct (+GGUF) | `apache-2.0` | no | **Yes** |
| meta-llama/Llama-3.2-1B-Instruct | `llama3.2` | **manual** | **No — dropped** |
| google/gemma-3-270m-it | `gemma` | **manual** | **No — dropped** |

Apache-2.0 is one-way compatible with AGPL-3.0: Apache-2.0 material may be included in an
AGPL-3.0 work. The obligations are light and mechanical — ship the Apache licence text and any
`NOTICE`, keep attribution, state that the file was not modified. That is a `THIRD_PARTY`
entry, not a legal project.

The two dropped candidates are gated behind manual approval (so they cannot be fetched by a
build, let alone redistributed freely) and carry bespoke terms with acceptable-use riders and,
for Llama, naming requirements. A model the user must accept terms for and download separately
defeats the entire purpose of the exercise, exactly as the brief says.

**One trap worth recording:** Qwen2.5 is *not* uniformly Apache. `Qwen2.5-0.5B-Instruct` and
`Qwen2.5-1.5B-Instruct` are `apache-2.0`; `Qwen2.5-3B-Instruct` is `license: other`. If anyone
later reaches for "the next size up", that is where the licence changes.

The runtime is fine too: `llama-cpp-python` is MIT, llama.cpp is MIT.

---

## 6. Packaging reality

**The runtime is a non-issue.** llama.cpp's CPU DLLs total **9.2 MB** (`llama.dll` 6.4,
`mtmd.dll` 1.2, `ggml-cpu.dll` 0.9, `ggml-base.dll` 0.6, `ggml.dll` 0.1). `scripts/samsara.spec`
already ships models and native libraries this way — `collect_all('openwakeword')`,
`faster_whisper/assets`, `ctranslate2` — so `collect_all('llama_cpp')` plus one `datas` entry
is the whole change. For reference, `dist/Samsara/_internal` is 1387 MB and PySide6 alone is
641 MB of it.

**One real CI cost:** pip found no prebuilt wheel for this platform and **built
llama-cpp-python 0.3.35 from source** (~4 minutes, needs a C++ toolchain). CI would need either
that toolchain or a pin against the project's own wheel index. Worth knowing before anyone
promises a quick integration.

**The weights belong in the fetcher, and that is not a compromise.** `samsara/components.py`
already has everything needed: `kind: "model"` is a declared component kind, downloads are
SHA-256 verified and atomic, `install_dir` is per-component, and `available: false` lets a
component be declared before it is released. More to the point — **the app already downloads a
model after install.** `dictation.py:2807` resolves Whisper out of
`~/.cache/huggingface/hub`, which on this machine holds `faster-whisper-medium` at **1460 MB**
and `faster-whisper-small` at 464 MB. A 138–468 MB router fetch is not a new class of friction;
it is the friction the app already has, for a model an order of magnitude smaller.

So the honest packaging answer is: **either route works.** Bundling 138 MB into the ZIP is
defensible; the fetcher is better, because it keeps the model optional and the download honest.
Packaging is not what blocks this.

---

## 7. Memory alongside Whisper

Measured with Samsara actually running (`pythonw` PID 12356): **593.7 MB working set, 4936 MB
private commit** — the commit figure includes the loaded Whisper model.

| candidate | marginal resident cost |
|---|---|
| SmolLM2-135M-Q8_0 | +207 MB |
| SmolLM2-360M-Q8_0 | +474 MB |
| Qwen2.5-0.5B-Q4_K_M | +540 MB |
| Qwen2.5-1.5B-Q4_K_M | **+1695 MB** |

On a 16 GB machine — Windows ~4 GB, Samsara with Whisper ~5 GB committed, a browser 1–3 GB —
the three small candidates are comfortable and can stay warm. **Qwen2.5-1.5B at 1.7 GB resident
is the one that does not fit that budget** while Whisper is also loaded, and it is the only
candidate whose accuracy is even arguable. That is not a coincidence worth designing around.

---

## 8. Recommendation

**None of these clear the bar. Do not ship a bundled tier-4 router.**

The numbers that say so:

- The best candidate at any prompt, Qwen2.5-1.5B-Q4_K_M, reaches **69.3% recall at a 12.00%
  false-positive rate** — one in eight ordinary dictation utterances firing a command.
- The three candidates that fit a 16 GB machine comfortably are far worse: **69–84% false
  positives**, or a collapse to 8% recall.
- A proper threshold sweep confirms it rather than rescuing it: Qwen2.5-1.5B's binary
  gate reaches **AUC 0.9442** — real, usable separation — but at a 1% false-positive budget
  it recovers only **10.0%** of missed commands, and buying 62% recall costs **5% false
  positives**. Qwen2.5-0.5B (AUC 0.7181) manages 7.3% recall at the same 1% budget.

Every other axis passes, which is worth saying plainly: **size is fine** (138 MB is +23.7% of a
ZIP that already grew to 560 MB), **latency is fine** (p95 85–225 ms, and 14.1% of utterances
need no model call at all), **licensing is clean** (Apache-2.0, redistributable, ungated), and
**packaging is solved** (9.2 MB of runtime, and a verified component fetcher that already
exists for a job the app already does with Whisper).

The blocker is accuracy, on the one axis where being wrong costs the target user the most. For
someone who is motor-impaired, a command fired into the middle of dictation is not a small
annoyance — it is a window closed, a tab switched, text replaced, and a recovery they have to
perform by voice. A 12% rate is not a tuning problem.

### What the data actually argues for

The shadow log's most useful finding is not about models. **All 15 commands the live stack
would have fired are false positives**, and so are the 79 suggestions. Adding a fourth tier to
a stack whose first three already have a 100% false-positive rate on real speech is optimising
the wrong thing. Two cheaper moves are worth more than any model:

1. **Tighten tiers 2–3 against the shadow log.** The grammar tier fires on `To, um...` and
   `the light weight?` at 0.85–0.9 confidence. That is a threshold and a filler-handling
   problem with 1371 real examples already on disk and no new dependency.
2. **Keep the empty-shortlist rule whatever else happens.** 14.1% of utterances give tier 3
   nothing to rank; that is a free, exact "none" and it needs no model.

### If it is pursued anyway

Run it **in shadow, never live**. `samsara/intent/shadow.py` is exactly the right vehicle and it
already exists: it logs what the gate *would* have done without letting it do anything. Add
tier 4 there, let it observe for a few weeks of real use, and let the owner's own false-positive
rate decide. The bar should be set before the measurement, not after. My suggestion, given what
a misfire costs this user: **below 1 in 500 dictation utterances**, roughly 0.2% — forty times
better than the best number measured here.

---

## 9. Integration estimate (an estimate, not work done)

The dominant fact is §0's: **the intent stack is not wired into dispatch.** So "integrate
behind the existing tier-3 seam" is genuinely cheap, because the seam executes nothing.

| step | estimate | notes |
|---|---|---|
| `tier_llm()` in `IntentResolver`, before the final `return Resolution(DICTATION, …)` | 0.5 day | the shortlist is already computed by `tier_similarity`; the empty-shortlist short-circuit is free |
| Model lifecycle — lazy load, warm handle, idle unload, hard timeout, own thread | 1 day | `resolve()` must not block; the house budget `LATENCY_BUDGET_MS = 30.0` covers tiers 1+2 only, and tier 4 is 65–225 ms, so it cannot be synchronous |
| Wire it into `shadow.py` and extend the JSONL schema with a `t4` field | 0.5 day | schema is versioned (`v: 1`); the observer already runs on a worker |
| `components.py` manifest entry + settings UI for download/state | 1 day | fetcher, verification and `unavailable` state all exist |
| `llama-cpp-python` in requirements + spec + CI wheel strategy | 1 day | the source build is the risk, not the code |
| Tests, including a false-positive regression gate over the shadow corpus | 1 day | this is the test that matters |

**≈ 5 days to a shadow-only tier 4.** Making it *execute* is not a size estimate at all — it is
a quality gate, and on today's numbers it would not pass one.

---

## 10. Method, and what would weaken these numbers

- Scratch venv at `D:\samsara_spike94\venv` (Python 3.11.15), outside the repo and outside the
  app's `F:\envs\sami`. Nothing was installed into the app or the repo. C: had 1.7 GB free, so
  everything lives on D:.
- **Total bytes pulled: 2 139 937 120 (2.04 GiB)** — four GGUF files, listed in the JSON.
- Scripts: `fetch.py`, `build_evalset.py`, `make_shortlists.py`, `bench.py`, `bench_gate.py`,
  `bench_roc.py`, `export_report_data.py`, all under `D:\samsara_spike94\`.

Honest limitations:

1. **The positives are synthetic.** Real missed commands may look different from paraphrases of
   canonical forms. The 95.3% shortlist ceiling is partly an artefact of paraphrases sharing
   words with their source. The *negatives*, which drive the conclusion, are entirely real.
2. **One user, two days, mostly dictating into Claude and a browser.** It is the target user,
   but it is one of them.
3. **Cold-load times are warm-file-cache times** — the weights had just been downloaded. First
   load after a reboot will be slower, bounded by disk.
4. **Design B's false-positive rates rest on 400 negatives**, not all 1147 (§3).
5. **The catalog moved under the spike.** `commands_catalog.json` was rewritten by the
   running app at 18:49, mid-measurement, going from 486 records to 487. Every number here
   uses the 486-record snapshot. A 0.2% difference changes nothing, but the snapshot is the
   honest provenance.
6. **Prompt space was sampled, not searched.** Two framings plus a threshold sweep is not an
   exhaustive search; a much better prompt may exist. The threshold sweep in §4 is the
   strongest available evidence that no wording would rescue these models, but it is evidence,
   not proof.
