# oMLX + GLM-5.3-Flash trial on Studio: review and runbook

**Status (2026-10-16): trial closed; not practical for Bryan's current Studio workload.** The
governing plan is
[`.hermes/plans/2026-10-16_113658-omlx-glm53-trial.md`](../.hermes/plans/2026-10-16_113658-omlx-glm53-trial.md).
§9 is the execution record.

This repository change adds a disabled `services.omlx` capability, and Studio imports it with
`enable = false`. With it disabled, Studio's system derivation is identical to the one at
`9fd29a4`, so applying it changes nothing on the host.

After Bryan approved the install bundle, the approved installation workers ran phases 3 and 4
and then the bounded live trial (phases 6-8) against a **foreground** server started with the
module's exact preflight, arguments and environment, not the launchd agent. Results are in §9.

- The runtime is installed from the pinned source, with native kernels, and verified (phase 3).
- The checkpoint is downloaded and verified 28/28 (phase 4).
- GLM-5.3-Flash loaded at 0.10 expert residency and passed the short generation, the
  two-turn tool round trip and the graded repository task (phases 7-8).
- The foreground server is stopped and port 8000 is free.
- **Not executed:** phase 5 (enabling through clean, pushed main), the image fixture (8b),
  fraction escalation, any context beyond 8K, and phase 10. No service was activated.

Phase 3c's block was corrected during execution (§9). The blocks pass `bash -n`.

This is a trial of a second, on-demand inference runtime beside Ollama. It is not a
migration. Ollama, its Tailscale route, its models and every client default stay as they are.

## 1. Verdict at a glance

| Gate | Result | Basis |
|---|---|---|
| **Safe enough to propose installing** | **Conditionally yes.** Loopback-only, single-user, operator-attended, under the conditions in §3.6. **Not** with oMLX's defaults, which leave the admin plane claimable from any web page. | Static source review of oMLX `53ab0e66`, §3 |
| **Compatibility established statically** | **Yes**, for the checkpoint against the pinned loader and the MoE-offload validator: every check passes and none is unresolved. Live loading and bounded performance are now exercised separately in §9. | Hub manifest, all 18 shard headers, pinned source, §4 |
| **Executed** | Phases 3-4 (installs, runtime, kernels, commissioning, checkpoint). Phases 6-8 against a foreground loopback server: load at 0.10 residency, short generation, tool round trip and graded repository task all passed, with no swap and normal kernel memory pressure. Decode ran at 2.2-2.6 tok/s, SSD-bound (§9) | §9 |
| **Not executed** | Phase 5 (launchd through clean, pushed main), the image fixture, fraction escalation, and contexts beyond 8K (the 64K workflow is **not** claimed). These are not pending tasks. | §9 |
| **Parent acceptance** | Disabled preparation accepted at `db72c6c`. Publication of this execution record records parent acceptance of the bounded trial, not activation or a decision to keep it. | Plan, "Approved execution continuation"; §9, "Parent verification" |

There was no blocker at the preparation stage, and none remains for the bounded trial.
Bryan decided this model is not practical for the Studio's current shared workload (§7).
The persistent service stays disabled; a stronger future Mac is a possible revisit, not a commitment.

## 2. Baseline, identities and pins

### Host baseline

Observed read-only on Studio, 2026-10-16:

| Item | Value |
|---|---|
| Hardware | `Bryans-Mac-Studio.local`, Apple M2 Ultra (GPU generation 14), 137,438,953,472 B (128 GiB). macOS 27.0.1 (26A434). `iogpu.wired_limit_mb` = 0 (Apple default) |
| Data volume | 459.8 GiB free |
| Memory | ~42 GiB free, ~35 GiB inactive, ~45 GiB anonymous (desktop and services), 9.7 GiB wired, no swap in use |
| Ollama | 0.40.5 on `127.0.0.1:11434`; `/api/ps` empty. Installed: `qwen3.8:27b-mlx` 16.9 GiB, `gemma4:31b-mlx` 17.4 GiB, `qwen3.6:35b-a3b-coding-nvfp4` 20.4 GiB. Studio sets context 65536, one parallel request and Tailscale Serve; the module defaults keep models loaded forever (`keepAlive -1`) and allow 2 loaded models |
| oMLX | Absent: not on PATH; no `oMLX.app`, process, launchd job, Homebrew formula or tap. `~/.omlx`, `~/.local/share/omlx-trial` and `~/Library/Application Support/oMLX` do not exist |
| Port 8000 | No listener. The trial uses it (oMLX's own default) |
| Toolchain | Full Xcode 27.0 (27A266a) at `/Applications/Xcode.app`. The Metal Toolchain component was missing at review; phase 3a installed it (§9) |
| Rust | No Rust toolchain on PATH (Studio does not import `dev/rust.nix`). Phase 3c uses build-time-only store paths (§9) |
| Python | uv 0.13.1 (Homebrew); uv-managed CPython 3.12.14 is installed at `~/.local/share/uv/python/cpython-3.12.14-macos-aarch64-none` |

### Pins

All pins are immutable, resolved 2026-10-16.

| What | Pin | Notes |
|---|---|---|
| oMLX source | `github.com/jundot/omlx` @ `53ab0e66d75fe46e3cfa7ed7a7762d3bbcf21e9c` | `git describe` gives `v0.7.1rc1-6-g53ab0e66`, committed 2026-10-16. It was main's HEAD at review time. Apache-2.0; ~22.9k stars (context, not safety). Six commits past the rc tag, so it is **not** what the Homebrew formula (`v0.7.1rc1` tarball) or the app ship. GitHub could not verify the commit signature |
| MLX | `mlx==0.32.4` + `mlx-metal` 0.32.4 (PyPI, uploaded 2026-10-14) | A `cp312 macosx_26_0_arm64` wheel exists. The kernels are ABI-coupled to this exact version and to `nanobind==3.1.0` |
| Git-pinned dependencies | `mlx-lm@94cdcae1`, `mlx-vlm@97902318`, `mlx-audio@94c77162`, `mlx-embeddings@32981fa4` | From `pyproject.toml` at the oMLX pin |
| Floating dependencies | `transformers>=5.14,<5.18`, `huggingface-hub>=1.19`, `fastapi`, `uvicorn` and others | There is no upstream lockfile. Phase 3 freezes them with `--exclude-newer 2026-10-16T00:00:00Z` and a hashed lock |
| Checkpoint (preferred) | `pipenetwork/GLM-5.3-Flash-MLX-mixed-4_8bit` @ `d43ea8b407ce4e9c25e6ac9baec3feab70d9f5f3` | 28 files (18 safetensors shards plus `tokenizer.json` as LFS/Xet). Safetensors total 181,923,899,861 B; repo total 181,944,533,258 B (169.45 GiB). MIT. A community conversion, not official weights. The manifest was re-fetched independently and matches the audit file-for-file |
| Official parent | `zai-org/GLM-5.3-Flash` @ `46a050bec13002ad29ee795c16e61cf4fa3c4a9c`; conversion source `zai-org/GLM-5.3-Flash-BF16` @ `f9fed3bbc82291073373d67452e9f25317ef7577` | MIT. The BF16 weight files are sha256-identical at every revision from 2026-08-26 to today. The checkpoint's chat template matches BF16 `b196718` |
| Converter | `github.com/PipeNetwork/glm53-flash-mlx` @ `a61a7c7d2fbdf3d218a9909365a24bd794f3a247` | Apache-2.0. The quantizer has been unchanged since `eeda563`, which predates the upload. **The checkpoint records no parent revision, converter commit or conversion log** |
| Fallback (not an approved substitute) | `TensorFold/GLM-5.3-Flash-MLX-oQ2-MTP` @ `dc55627da0b79fb514c5cfc3fde9b8cf8103fff3` | 110,128,198,215 B of safetensors. 2-bit routed experts with an MTP head. Its mHC `base`/`scale` and router bias are stored BF16, so precision was lost. Using it needs a separate decision |

Matching Hub hashes will establish **integrity** (these exact bytes). They do not establish
**derivation**, meaning that the weights were faithfully converted from zai-org's.

## 3. Source and security review: oMLX `53ab0e66`

This review followed `homelab-change-management` and its third-party-package-review
reference. It was static: nothing from the checkout or from fetched code was imported,
built or run. Citations are `path:line` in the pinned tree.

### 3.1 Findings that shape this design

| # | Finding | Severity | Control in this design |
|---|---|---|---|
| R1 | `POST /admin/api/setup-api-key` has no auth until a key exists. It checks only that the bind and the TCP peer are loopback (`omlx/admin/routes.py:1895-1976`). CORS defaults to `["*"]` with every method and header allowed (`omlx/settings.py:196`, `omlx/server.py:2386-2394`), and nothing checks Host, Origin or CSRF. **Any web page in the operator's browser can claim the admin key.** The admin plane is equivalent to code execution: MCP command config (`routes.py:5315-5318`), `splash_path` exec (`routes.py:5529-5541`), per-model `trust_remote_code`, and accuracy benchmarks that run model output (`omlx/eval/humaneval.py:114-145`). Reasoned from code, not exercised | High | Before the first start, commissioning writes `settings.json` with an API key and `cors_origins: []`. **The preflight refuses to start without them.** `--headless` removes the web UI |
| R2 | With no API key on loopback, every `/v1/*` route is open to local processes and to cross-origin pages (`server.py:374-377`). That includes `/v1/web/fetch`, a server-side fetch | High | Same as R1. The preflight also refuses `skip_api_key_verification` or `allow_unauthenticated_inference` |
| R3 | GLM-5.3-Flash (`glm5_next`) loads through the VLM engine (`omlx/model_discovery.py:80,90,800-804`; `omlx/engine/vlm.py:2204`). Pinned mlx-vlm imports `config.json`'s `model_file` from the model directory **whatever `trust_remote_code` says** (mlx-vlm `utils.py:230-247` at `9790231`). oMLX guards only the mlx-lm path (`omlx/utils/model_loading.py:41-55`) | High (latent) | The pinned checkpoint has no `model_file`, no `auto_map` and no `.py`. **The preflight refuses any model directory that has one.** Phase 4 checks the exact file set. Weights are never fetched with oMLX's downloader, which takes no revision (`routes.py:796-800`) |
| R5 | Benchmark and accuracy results are uploaded to omlx.ai with a stable hardware-derived `owner_hash`, and there is no opt-out (`omlx/admin/benchmark.py:1380,1553-1560,2233-2237`; `omlx/admin/accuracy_upload.py:47`) | Medium (privacy) | Operating rule: no oMLX benchmarks, accuracy runs, oQ or uploads. Measurements in this runbook use plain API calls |
| R6 | `/v1/web/search` and `/v1/web/fetch` are always registered, and the SSRF guard has an acknowledged DNS race (`server.py:765-769`; `omlx/websearch.py:531-538`) | Medium | The API key is required. Commissioning disables search (`web_search_provider: "searxng"` with an empty URL). Fetch has no off switch: residual |
| R8 | CLI flags are persisted into `settings.json` at every start (`omlx/cli.py:287-293`). A missing `secret_key` or empty `server_aliases` triggers a full runtime save at first start (`server.py:2373-2379, 472-491`) | Low | Intended: the Nix-owned flags are re-asserted at each start. Commissioning seeds `secret_key` and `server_aliases` |
| R9 | Every start writes `~/.omlx/bin/omlx-cluster-python` (0755) and reads `~/.omlx/cluster/runtime`, regardless of `--base-path` (`server.py:502-531`; `omlx/cluster/worker_shim.py:33-117`) | Low | Accepted and documented; rollback leaves it in place (§6, phase 10) |
| R10 | The SSD KV cache defaults to `auto`, half of free space (`settings.py:385-388`), and stores prompt-derived KV plus up to 1000 Responses records in plaintext | Medium | A fixed 20 GiB (`--paged-ssd-cache-max-size 20GB`) under the 0700 state directory |
| R11 | `~/.cache/huggingface/hub` is discovered as a model directory by default, and admin delete can `rmtree` it (`settings.py:715`; `routes.py:7837-7862`) | Low-Medium | `--no-hf-cache`, plus `HF_HOME` under the state directory |
| R12 | Sessions: remember-me tokens last 30 days with no server-side revocation, and settings and stats return keys in plaintext (#929) | Low | Headless, no browser sessions |
| R15 | Supply chain: no lockfile; floating `transformers`, `huggingface-hub` and `fastapi`; git-SHA dependencies whose build backends run at install; `omlx` is unregistered on PyPI (squattable); ~57% of commits from one maintainer; unprotected `main`; unsigned tags | Medium | Install from the pinned commit only, never `pip install omlx`. Lock with a date cutoff and hashes, and review the lock before installing |
| R16 | Native kernels are opt-in (`setup.py:7-21`) and need the Metal toolchain. Without them GLM-5.3's sparse-MLA/indexer/weighted-sum kernels fall back to MLX ops, and the 4096-token wide prefill floor is off (`omlx/scheduler.py:1797-1820`) | Info / performance | Build them (phase 3) after the Metal Toolchain download is approved |
| R17 | GLM-5.3-Flash stability: #3421 (boundary snapshots → jetsam and a **host reboot**) and #3956 (prefill memory guard not applied on the VLM engine) are open | Medium (availability) | One request at a time, short context, operator present, and an explicit stop criterion (phase 7). Do not rely on the memory guard alone |
| R20 | `serve` makes no privileged OS changes: no `sysctl -w`, no sudo, no launchd/brew registration. `iogpu.wired_limit_mb` is only read (`omlx/process_memory_enforcer.py:171-191`) | Positive | — |

Further detail:

- **Egress at startup with the commissioned settings:** none to the internet. Startup only
  does a reverse-DNS lookup when `server_aliases` is empty.
- **Default-off features:** MCP (`mcp.config_path=None`), the cluster (mDNS, multicast,
  Tailscale probing) and the GitHub update check (web UI only).
- **Model loading:** it uses local files. `HF_HUB_OFFLINE=1` closes the glm5_next
  processor's `hf_hub_download` fallback for a missing `processor_config.json`.
- **Chat templates:** they render in transformers' `ImmutableSandboxedEnvironment`.
- **Usage history:** it is a local SQLite file.

### 3.2 Native kernels and the build

- Prebuilt `.metallib`/`.so` files are not committed. A plain `pip install` skips the kernels.
  `make install` builds them with `OMLX_WITH_CUSTOM_KERNEL=1 python setup.py build_ext
  --inplace` (`Makefile:30-37`).
- The Homebrew formula's `--with-custom-kernel` builds them the same way. The macOS app
  ships them precompiled.
- On macOS ≥ 27 the formula builds `cohere_melody`, `pydantic-core`, `rpds-py`, `tiktoken`
  and `tokenizers` from source, with stripping off. dyld 27 rejected their prebuilt wheels
  with "mis-aligned LINKEDIT string pool" (`Formula/omlx.rb:68-83`; issue #2110). Whether
  27.0.1's dyld still rejects them is **unresolved**, so phase 3 follows the formula.
- **Studio's M2 Ultra is GPU generation 14.** GLM-5.3's runtime-compiled fused decode
  kernels require generation 15 (M3) or newer
  (`omlx/patches/mlx_vlm_glm5_next_compat/decode_kernels.py:2260-2269`), so they never
  apply on Studio, offload or not.

### 3.3 Why not Homebrew or the macOS app

- **Homebrew:** the tap floats, and `homebrew.onActivation.upgrade = true` would upgrade it
  at every activation. `cleanup = "zap"` removes undeclared installs. The formula pins the
  rc tarball, not this commit.
- **The app:** it runs its own server and auto-updater, which would be a second daemon.
- **Chosen route:** a user-managed venv built from the pinned commit, supervised by Nix.
  This is the route the repository already uses for Open WebUI (an isolated uv venv),
  without Open WebUI's auto-install.

### 3.4 Release and maintenance context

- Releases are frequent; this pin is a post-rc development snapshot. The project is very
  active: 22.9k stars and pushed today.
- `SECURITY.md` treats local users and processes as trusted, so a holder of the API key is
  effectively an admin.
- An OSV scan of the 85 wheel-only resolved packages found 0 known vulnerabilities. The
  git dependencies and extras were **not** scanned (unresolved).

### 3.5 Residual risks after the controls

- **The key is admin.** Any same-user process that can read the 0600 `settings.json` has
  admin rights, which amount to code execution.
- **No egress kill switch.** `/v1/web/fetch` stays available to key holders. Only a network
  policy is a hard control on egress.
- **R3 stays in the code.** Safety depends on the pinned, verified checkpoint and on nothing
  else being added to `models/`; the preflight is defence in depth.
- **Writes outside the state directory.** `~/.omlx/bin/omlx-cluster-python` (R9) and
  `~/.cache/uv` are untouched only if phase 3's `UV_CACHE_DIR` is used.
- **Availability.** Host-level memory pressure (R17) is a risk to Studio and to Ollama.
- **Unresolved items:**
  - browser Local Network Access behaviour;
  - MarkItDown and STS third-party fetches (disabled or unused here);
  - transformers 5.15-5.17 sandbox details;
  - a vulnerability scan of the git dependencies.
- **Resolved in execution (§9).** The kernel build downloads nothing: `mlx.extension`
  generates a CMake project with no FetchContent, and nanobind and MLX come from the venv.
- **tiktoken's crates float.** Its sdist ships no `Cargo.lock`, so its five crates resolve
  within their semver ranges at build time. They are checksum-verified by crates.io but
  not date-frozen. The other four Rust sdists ship a `Cargo.lock`.

### 3.6 Verdict

**Conditionally acceptable** for a loopback-only, single-user, operator-attended trial, if all of the following hold:

1. `settings.json` is commissioned before the first start (phase 3d). The preflight
   enforces the security-critical keys.
2. The server runs headless, with HF offline and telemetry off, MCP and cluster off, and no
   `OMLX_SUPERVISED`. The module enforces all of this.
3. The checkpoint is fetched outside oMLX at the pinned revision, and every file is
   verified (phase 4).
4. There are no oMLX benchmarks, accuracy runs, oQ, uploads, oMLX downloads or browser
   admin sessions.
5. The runtime comes from the pinned commit, with a recorded, reviewed, hashed resolution
   (phase 3).
6. A memory-pressure stop criterion is applied, and GLM is unloaded or idle-timed-out when
   not in use (phase 7).

**Not recommended** for any non-loopback bind, a shared host, or oMLX's default settings.

## 4. Checkpoint compatibility: static

### 4.1 Identity and config

`config.json` at the pin declares:

- `model_type: glm5_next` (`Glm5NextForConditionalGeneration`), with a vision tower;
- 45 layers: 34 KDA linear-attention and 11 DSA/MLA layers;
- 3 dense MLP layers (0-2) and 42 MoE layers (3-44);
- 288 routed experts, top-8, 1 shared expert;
- hidden size 4096, expert intermediate size 2048;
- mHC hyper-connections (`hc_mult` 4);
- a 1,048,576-token maximum context.

Quantization:

- Global: 4-bit affine, group 64, with no `mode` key.
- 542 overrides at 8-bit, group 64.
- Result: routed experts are 4-bit; everything else that is quantizable is 8-bit.

Integrity:

- In all 18 shards, header plus payload equals the file size.
- The index matches the headers exactly.
- The small files' git oids match.

### 4.2 Loader and offload eligibility at the oMLX pin

| Check (pinned source) | Verdict |
|---|---|
| `glm5_next` routes to the VLM engine; the vendored glm5_next model is loaded by `patches/mlx_vlm_glm5_next_compat/__init__.py:29-80` | Pass |
| Header-only offload validator `moe_offload_compat._inspect` (`omlx/patches/moe_offload_compat.py:25-185`). The admin toggle and the loader (`utils/model_loading.py:563-568`) share it. Checks: type supported; geometry; `mlp_layer_types` (42 sparse); `quantization` dict | Pass |
| Dense first-k layers skipped; **stacked** `language_model.model.layers.N.mlp.switch_mlp.{gate,up,down}_proj` present in all 42 layers | Pass |
| Expert spec: 4 bits / group 64 / affine; packing math; no per-expert `.bias` | Pass |
| 378 shape/dtype checks. gate/up `(288,2048,512)` U32 with scales/biases `(288,2048,64)` BF16; down `(288,4096,256)` U32 with `(288,4096,32)` BF16 | Pass |
| Runtime adapter `resolve_view` (`omlx/patches/deepseek_v4/moe_offload.py:82-110`): `QuantizedSwitchLinear`, checkpoint shape equals module shape | Pass (static, from constructors) |
| gate/up stay split (sanitize fuses only for `glm_moe_dsa`; runtime fusion is skipped under offload, `engine/vlm.py:2336-2341`) | Pass |
| top-k 8 ≤ capacity floor `max(8, top_k)` | Pass |
| Shared experts and dense MLPs are not wrapped, so they stay resident | Pass |
| Every tensor maps to a model parameter; all 668 quantized modules agree with the config dict | Pass (static; not instantiated) |
| Float32-sensitive state stored F32: mHC `base`/`scale`, KDA `A_log`/`dt_bias`, router `e_score_correction_bias` (295,230 params, matching the Hub count). mHC `fn` and router `gate.weight` are BF16 as in the parent and are upcast at load (`language.py:2441-2455`) | Pass |
| MTP: config declares `num_nextn_predict_layers: 1`, but the converter dropped the head (no `mtp.*`, no `layers.45.*`). oMLX attaches no head (`utils/model_loading.py:1049-1050`) | Pass (MTP not applicable) |

Notes:

- `omlx/patches/deepseek_v4/moe_offload.py` is the glm5_next offload adapter. It was added
  by #3720 and extended by #3841; `apply_moe_expert_offload` calls it
  (`omlx/patches/moe_expert_offload.py:1084-1088`).
- Contradictions in `docs/experimental/MoE_Expert_Offload.md`, resolved from the code:
  - The V4.1 section's "glm5_next not supported" refers to the V4.1 adapter only.
  - Lightning MTP with offload *is* allowed for glm5_next (`omlx/model_settings.py:43,63-69`),
    but it is moot here because the checkpoint has no MTP weights.
  - VLM MTP and DFlash are always rejected with offload.

### 4.3 Kernel and performance path under offload

Report this as the expected consequence, not as "MLX supported":

- **Compiled FFN graphs are disabled for every wrapped layer**
  (`deepseek_v4/moe_offload.py:376-388`).
- **The adapter synchronizes the GPU to the host at every MoE layer, every step**
  (`.tolist()` at `:151`): about 42 syncs per token, which breaks asynchronous pipelining.
- **Expert misses are read with `pread` through Python bytes on LRU eviction.** This adapter
  lacks the common adapter's GPU overlap, staging buffers and prefill slot release.
- **Routed experts run through stock `mx.gather_qmm`.** GLM-5.3's fused one-token decode
  kernels need a `SwitchGLU`, which the wrapper is not (`language.py:2020,2062`), and on
  Studio they need gen 15 anyway. The native `glm_moe_weighted_sum` runs only on sorted
  prefill.
- **Expect** decode to be slower than fully resident even at a 100% hit rate, and SSD-bound
  below that. oMLX publishes **no** glm5_next offload measurement; its GLM-5.2 numbers
  (1.5-1.8 tok/s on an M5 Max) are a different model and must not be quoted as expectations.
- **The SSD KV/prefix cache (`cache/`) is separate from expert offload.** Offload reads the
  checkpoint's own shards.

### 4.4 Template, reasoning effort and tools

- **`reasoning_effort`.** The chat template keeps it only if it is `low` or `high`.
  Anything else, **including omitting it, becomes `Max`** (`chat_template.jinja:2-3`). The
  upstream model card says the same.
- **How oMLX forwards it.** It passes a request's top-level `reasoning_effort` into the
  template kwargs unless `chat_template_kwargs` already sets it (`omlx/api/utils.py:92-100`).
  Per-model `chat_template_kwargs` are the default, and requests override them
  (`omlx/model_settings.py:1835-1862`). Phase 6 sets a per-model default of `low`; requests
  name their effort explicitly.
- **Tool calls.** They are parsed by `glm47` (the template has `<arg_key>`).
- **Open tool issues:**
  - #3271: the model sometimes samples `<|assistant|>` and talks to itself.
  - #4559: the finish reason is wrong when `max_tokens` cuts a tool call.
  - #3794: the streaming filter holds back tool markers that appear in prose.
- **Template age.** The checkpoint's template is the 2026-08-26 version; upstream updated
  it on 2026-10-15, and oMLX already normalizes the main difference (null content).

### 4.5 Byte accounting and offload sizing

| Group (from shard headers) | GiB |
|---|---:|
| Routed experts (42 layers × 3.797 GiB; 13.5 MiB per expert) | 159.469 |
| Attention (KDA + MLA/DSA) | 6.002 |
| Vision tower | 1.050 |
| Shared experts | 1.046 |
| Embeddings + lm_head | 1.256 |
| Dense MLP (layers 0-2) | 0.448 |
| Router + mHC | 0.158 |
| **Everything except routed experts** | **9.961** |

The resident-weight estimate follows oMLX's admission formula
(`omlx/patches/moe_expert_offload.py:1164-1272`):

```text
capacity(f) = min(288, max(8, round(288 * f)))          # experts kept per layer; floor 8 = top-k
resident(f) = 9.961 + 42 * 3.797 * capacity(f) / 288   GiB (weights only; KV and prefill transients add to it)
```

| f | experts/layer | resident weights |
|---:|---:|---:|
| 0.10 | 29 | 26.02 GiB |
| 0.125 | 36 | 29.90 GiB |
| 0.15 | 43 | 33.77 GiB |
| 0.20 | 58 | 42.08 GiB |
| 0.25 | 72 | 49.83 GiB |

**Starting fraction: 0.10.** It is derived from these bytes and from headroom, not copied
from another model:

1. Memory oMLX can use under the `safe` guard tier is free + inactive − the tier reserve.
   That was ~42 + ~35 − 16 ≈ **61 GiB** on 2026-10-16. The tier reserve is 20% of RAM,
   clamped to 6-16 GiB (`omlx/process_memory_enforcer.py:61-85, 684-760`).
2. Keep **25 GiB** for one Ollama model loading uninvited mid-trial. The largest installed
   is 20.4 GiB, plus its 64K-context KV. The two servers cannot coordinate eviction.
3. Keep **8 GiB** for GLM's KV and prefill transients at an 8K cap. This is an estimate to
   be replaced by phase 7's measurement.
4. That leaves 61 − 25 − 8 = **28 GiB** for resident weights, and **0.10 (26.0 GiB)** is the
   largest tabulated fraction that fits.

Lower residency also leaves RAM to the page cache, which serves repeated misses faster than
the SSD. That is oMLX's measured experience on another model; it is not evidence for this
one.

Phase 7 recomputes the budget from live `vm_stat`. Escalation is one step at a time, at most
to 0.20, and only when there is no memory pressure.

## 5. The Nix capability

These files make up the capability:

- `modules/services/omlx.nix` (Darwin; NixOS stub)
- `modules/services/omlx-preflight.sh`
- `modules/hosts/studio.nix` (import, explicit disabled settings)
- `scripts/test_omlx_module.py`

When enabled, Nix owns exactly one launchd user agent, `org.nixos.omlx`:

```text
bash omlx-preflight.sh <jq> <stateDir> <stateDir>/runtime/venv/bin/omlx serve
  --base-path <stateDir> --model-dir <stateDir>/models --host 127.0.0.1 --port 8000
  --max-concurrent-requests 1 --memory-guard safe
  --paged-ssd-cache-dir <stateDir>/cache --paged-ssd-cache-max-size 20GB
  --no-hf-cache --headless --log-level info
env: HOME, PATH=/usr/bin:/bin:/usr/sbin:/sbin, HF_HOME=<stateDir>/huggingface,
     HF_HUB_OFFLINE=1, HF_HUB_DISABLE_TELEMETRY=1, TRANSFORMERS_OFFLINE=1
RunAtLoad; KeepAlive.Crashed (a refusal or configuration error stays down); logs /tmp/omlx.log, /tmp/omlx.error.log
```

Every flag maps to the pinned CLI (`omlx/cli.py:1156-1376`):

- **Bind and memory.** `--host` keeps oMLX's interactive saved-host migration prompt out of
  play (`cli.py:87-160`). The `safe` tier never compresses other apps' memory.
- **What the module does not declare:** no install or download, no activation script, no
  firewall registration, no Tailscale route, no MCP config, HF endpoint or proxy, no
  `OMLX_SUPERVISED`, and no credential. Enabling the module leaves every activation script
  byte-identical (tested).
- **Allowed settings.** `memoryGuard` accepts only `safe` or `balanced`. `ssdCacheGB` is a
  positive integer, never `auto`. `port` must be ≥ 1024 and differ from Ollama's.
- **Preflight refusals.** Before `exec`, `omlx-preflight.sh` exits 78 with one line on
  stderr when:
  - the state directory or runtime is missing;
  - `settings.json` is missing, unreadable, or not commissioned: API key ≥ 32 characters,
    `cors_origins: []`, `skip_api_key_verification` and `allow_unauthenticated_inference`
    false, cluster off, `mcp.config_path` null;
  - a model is pinned (pinned models load at startup);
  - any `models/*/` carries `model_file`, `auto_map` or `*.py`.

  It never prints the key, creates nothing, and uses only shell builtins plus the jq it is
  given.
- **Per-model settings** (offload, context cap, effort default) are oMLX's own mutable state
  in `model_settings.json`. They are set through its admin API in phase 6; activation never
  writes them.

## 6. Commissioning and rollback runbook

Rules for the whole runbook:

- Commands are sourced from the pinned interfaces cited above, and are **not executed**.
- Run each phase's blocks in order in **one** Studio terminal, as Bryan, in an attended
  session.
- Stop at the first failed check and keep the evidence.
- None of these steps touches Ollama, Open WebUI, Hermes, or any client or provider
  configuration.

### Phase 1: parent review and publication (not the worker)

1. An independent review of this candidate and of §3-§4. Acceptance is the parent's decision.
2. Publish per nix-configs policy: commit the task-owned paths directly to `main` and
   `git push origin main`. If `origin/main` has diverged, stop and report; do not rebase.
   Publishing changes nothing on Studio, because the service is disabled.

```bash
set -euo pipefail
cd /Users/bryan/code/nix-configs
git fetch origin
git status --short --branch
# After acceptance and the commit: confirm origin/main has it.
git fetch origin && git log --oneline -1 origin/main -- modules/services/omlx.nix
```

### Phase 2: Bryan approves the exact installs and the download

Present this list and get an explicit yes for each item:

| # | What | Where | Size (est.) | What runs |
|---|---|---|---|---|
| a | Xcode **Metal Toolchain** component (Apple) | Xcode component store | ~1 GiB (estimate) | `xcodebuild -downloadComponent MetalToolchain` |
| b | **Rust** `cargo` + `rustc` 1.98.1 from nix-configs' locked nixpkgs (build-time only, not installed globally) | `/nix/store` | ~1 GiB | 5 Rust extensions built from source (macOS 27 dyld workaround) |
| c | **oMLX** source at `53ab0e66`, plus a dependency resolution frozen to 2026-10-16 and hashed | `~/.local/share/omlx-trial/runtime/{src,venv,uv-cache}` | several GiB (record actual) | build backends of the git-pinned deps and oMLX's `setup.py`, including the native kernel build |
| d | **Checkpoint** `pipenetwork/GLM-5.3-Flash-MLX-mixed-4_8bit@d43ea8b4` | `~/.local/share/omlx-trial/models/GLM-5.3-Flash-MLX-mixed-4_8bit` | **181.94 GB = 169.45 GiB** | download only |

Disk budget:

- Weights 169.5 GiB + KV cache 20 GiB + runtime, cache and toolchain ~20 GiB ≈ **210 GiB**.
- Proceed only if `/System/Volumes/Data` shows **≥ 230 GiB free** before phase 4. It showed
  459.8 GiB on 2026-10-16.
- Stop if free space ever falls below 50 GiB.

Never download the source FP8 or BF16 weights, or a second copy.

### Phase 3: install and verify the runtime (executed 2026-10-16; see §9)

**3a. Preconditions and the approved Metal Toolchain** (executed 2026-10-16; results in §9)

```bash
set -euo pipefail
test "$(sysctl -n hw.memsize)" = 137438953472
sysctl -n machdep.cpu.brand_string                    # expect: Apple M2 Ultra
df -k /System/Volumes/Data | awk 'NR == 2 { printf "%.1f GiB free\n", $4 / 1048576 }'
xcodebuild -downloadComponent MetalToolchain           # approved item (a)
xcodebuild -showComponent MetalToolchain
/usr/bin/xcrun -sdk macosx metal -v                   # must now run
```

**3b. Pinned source and isolated state**

```bash
set -euo pipefail
OMLX_REV=53ab0e66d75fe46e3cfa7ed7a7762d3bbcf21e9c
STATE="$HOME/.local/share/omlx-trial"
RUNTIME="$STATE/runtime"
umask 077
mkdir -p "$STATE/models" "$STATE/cache" "$STATE/huggingface" "$RUNTIME"
chmod 700 "$STATE"
git -c core.hooksPath=/dev/null clone --no-checkout https://github.com/jundot/omlx.git "$RUNTIME/src"
git -C "$RUNTIME/src" -c advice.detachedHead=false checkout "$OMLX_REV"
test "$(git -C "$RUNTIME/src" rev-parse HEAD)" = "$OMLX_REV"
test -z "$(git -C "$RUNTIME/src" status --porcelain)"
```

**3c. Locked install, Rust extensions from source, native kernels**

Executed 2026-10-16 (§9). Three corrections were found during execution and are in the
block below: tiktoken's `setuptools-rust` in the build allowlist, the PyO3 macOS link
flags that tokenizers' sdist does not deliver, and the locked `cmake` on `PATH` for the
kernel build. `CARGO_HOME` also keeps crate downloads inside the runtime directory.

The extensions are built from source on macOS 27, following `Formula/omlx.rb:68-83`. The
kernels are built the `make install` way (`Makefile:30-37`), against the venv's Python. MCP
and the other extras are deliberately not installed.

Resolve the build-tool allowlist first, using wheels only so this step cannot run an
sdist's build backend. It includes mlx-lm's `setuptools>=80` and `setuptools-scm>=9`,
the other pinned Git projects' setuptools/wheel requirements, the Rust extensions'
Maturin backend, tiktoken's `setuptools-rust>=1.5.2`, and oMLX's native-kernel requirements. Inspect the exact selected
sdists' build declarations against this allowlist before compiling the runtime lock.
The resulting hashed lock must constrain **isolated** build environments during both
runtime metadata resolution and installation; merely installing it into the target
venv does not constrain those environments.

Missing or incompatible build dependencies are a **STOP**, not permission to remove
constraints or hash checks. Extend and re-review the build allowlist/lock if the
selected sources require another backend or dynamic build dependency. Metadata
resolution can execute the pinned projects' build hooks, so it is itself within the
approved installation window. Runtime execution remains untested here.

```bash
set -euo pipefail
STATE="$HOME/.local/share/omlx-trial"
RUNTIME="$STATE/runtime"
VENV="$RUNTIME/venv"
PYTHON="$HOME/.local/share/uv/python/cpython-3.12.14-macos-aarch64-none/bin/python3.12"
CUTOFF=2026-10-16T00:00:00Z
export UV_CACHE_DIR="$RUNTIME/uv-cache" UV_PYTHON_DOWNLOADS=never
export CARGO_PROFILE_RELEASE_STRIP=false MATURIN_STRIP=false
export CARGO_HOME="$RUNTIME/cargo-home"                # crate downloads stay out of ~/.cargo
# PyO3 extension modules resolve libpython at load time on macOS. tokenizers' sdist keeps
# these flags in bindings/python/.cargo/config.toml, which Cargo never reads from the
# sdist root, so its link fails without them (2026-10-16). Cargo merges them with
# pydantic-core's identical root config.
export CARGO_TARGET_AARCH64_APPLE_DARWIN_RUSTFLAGS="-C link-arg=-undefined -C link-arg=dynamic_lookup"
for pkg in cargo rustc; do                            # approved item (b), pinned by nix-configs' flake.lock
  out=$(nix build --no-link --print-out-paths --inputs-from /Users/bryan/code/nix-configs "nixpkgs#$pkg.out")
  PATH="$out/bin:$PATH"
done
export PATH
NO_BINARY=(--no-binary cohere-melody --no-binary pydantic-core --no-binary rpds-py --no-binary tiktoken --no-binary tokenizers)

[ -x "$VENV/bin/python" ] || uv venv --python "$PYTHON" "$VENV"   # uv 0.13 refuses an existing venv
uv pip compile - --python "$VENV/bin/python" --only-binary :all: \
  --exclude-newer "$CUTOFF" --generate-hashes --output-file "$RUNTIME/build-requirements.lock" <<'BUILD'
setuptools>=80
setuptools-scm>=9
wheel
cmake>=3.27
nanobind==3.1.0
mlx==0.32.4
maturin>=1,<2
setuptools-rust>=1.5.2
BUILD
# STOP: review the build lock (versions, sources, hashes) and selected source build requirements.
uv pip compile "$RUNTIME/src/pyproject.toml" --python "$VENV/bin/python" \
  --build-constraints "$RUNTIME/build-requirements.lock" --require-build-hashes \
  --exclude-newer "$CUTOFF" --generate-hashes "${NO_BINARY[@]}" --output-file "$RUNTIME/requirements.lock"
# STOP: review the runtime lock and confirm all Git requirements use the reviewed immutable commits.
uv pip sync --python "$VENV/bin/python" "${NO_BINARY[@]}" \
  --build-constraints "$RUNTIME/build-requirements.lock" --require-build-hashes --exclude-newer "$CUTOFF" \
  "$RUNTIME/requirements.lock" "$RUNTIME/build-requirements.lock"
# Editable oMLX uses the already installed reviewed tools; uv may not resolve/download build deps.
uv pip install --offline --python "$VENV/bin/python" --no-deps --no-build-isolation -e "$RUNTIME/src"
(
  cd "$RUNTIME/src"
  # mlx.extension runs a bare `cmake` (generated project: cmake_minimum_required 3.25);
  # use the locked cmake 4.4.4, not whatever PATH holds (Studio has none).
  PATH="$VENV/bin:$PATH" OMLX_WITH_CUSTOM_KERNEL=1 CMAKE_ARGS="-DPython_EXECUTABLE=$VENV/bin/python" \
    "$VENV/bin/python" setup.py build_ext --inplace --force
)
```

**3c'. Verify the runtime.** The two Python lines are the first time oMLX code runs.

```bash
set -euo pipefail
STATE="$HOME/.local/share/omlx-trial"
VENV="$STATE/runtime/venv"
"$VENV/bin/omlx" --version
"$VENV/bin/python" -I -c 'import importlib.metadata as m; print({p: m.version(p) for p in ("mlx", "mlx-metal", "mlx-lm", "mlx-vlm", "transformers", "huggingface-hub", "omlx")})'
"$VENV/bin/python" -I -c 'import mlx.core as mx; i = mx.device_info(); print(i["architecture"], i["max_recommended_working_set_size"])'
"$VENV/bin/python" -I -c 'import pydantic_core, rpds, tiktoken, tokenizers, cohere_melody; print("rust extensions load")'
"$VENV/bin/python" -I -c 'from omlx.custom_kernels import native_kernel_status as s; bad = {k: v["import_error"] for k, v in s().items() if not v["available"]}; print(bad or "all native kernels available"); raise SystemExit(1 if bad else 0)'
"$VENV/bin/omlx" serve --help | grep -E -- '--no-hf-cache|--headless|--paged-ssd-cache-max-size|--memory-guard '
uv pip freeze --python "$VENV/bin/python" > "$STATE/runtime/installed.txt"
```

Record for the trial log:

- the versions;
- the GPU architecture (expect `applegpu_g14…`);
- `max_recommended_working_set_size`, which is the Metal cap phase 7 budgets against;
- the kernel status.

A failed kernel build is a **stop**: do not run the trial on the fallback path without
deciding to.

**3d. Commission `settings.json` before the first start** (closes R1/R2/R6/R8/R11).
Executed 2026-10-16: `settings.json` exists, mode 0600.

This step creates the file and refuses to overwrite one. It writes a random 64-hex key that
is never echoed, with mode 0600, and uses only the system Python's standard library.

```bash
set -euo pipefail
STATE="$HOME/.local/share/omlx-trial"
test ! -e "$STATE/settings.json"
/usr/bin/python3 -I - "$STATE" <<'PY'
import json, os, secrets, sys

state = sys.argv[1]
settings = {
    "version": "1.0",
    "server": {
        "cors_origins": [],
        "server_aliases": ["127.0.0.1"],
        "distributed_inference_enabled": False,
    },
    "auth": {
        "api_key": secrets.token_hex(32),
        "secret_key": secrets.token_hex(32),
        "skip_api_key_verification": False,
        "allow_unauthenticated_inference": False,
        "sub_keys": [],
    },
    "mcp": {"config_path": None, "expose_tools": False},
    "huggingface": {"endpoint": "", "hf_cache_enabled": False},
    "idle_timeout": {"idle_timeout_seconds": 900},
    "integrations": {
        "web_search_provider": "searxng",
        "web_search_searxng_url": "",
        "markitdown_enabled": False,
    },
}
fd = os.open(os.path.join(state, "settings.json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as f:
    json.dump(settings, f, indent=2)
print("settings.json commissioned (key not shown)")
PY
stat -f '%Sp %N' "$STATE/settings.json"                 # expect -rw-------
```

`idle_timeout_seconds: 900` unloads an idle model after 15 minutes, so GLM is not left
resident beside Ollama.

### Phase 4: download and verify the checkpoint (executed 2026-10-16; see §9)

**Done: the checkpoint is present and verified 28/28.** Do not download it again. To
re-check it, rerun only the verification block below.

The download used a different tool from the one below, because the venv had no `hf` while
the runtime install was blocked. Each of the 28 files was fetched with `curl` from the
revision-pinned URL (`…/resolve/d43ea8b4…/<file>`), with no token sent. The verification
block then ran verbatim and is the integrity gate.

As written, the step uses the venv's own pinned `hf`, never oMLX's downloader, and keeps HF
state private to the trial. `hf download` resumes an interrupted run; rerun the same command.

```bash
set -euo pipefail
STATE="$HOME/.local/share/omlx-trial"
MODEL_DIR="$STATE/models/GLM-5.3-Flash-MLX-mixed-4_8bit"
df -k /System/Volumes/Data | awk 'NR == 2 { exit ($4 / 1048576 < 230) }'   # >= 230 GiB free, else stop
HF_HOME="$STATE/huggingface" HF_HUB_DISABLE_TELEMETRY=1 \
  "$STATE/runtime/venv/bin/hf" download pipenetwork/GLM-5.3-Flash-MLX-mixed-4_8bit \
  --revision d43ea8b407ce4e9c25e6ac9baec3feab70d9f5f3 --local-dir "$MODEL_DIR"
```

Next, verify **every** file before the first load.

- The 19 LFS files are checked by SHA-256 and size.
- The 9 small files are checked by git blob id and size.
- The directory must hold exactly these 28 files, apart from `hf`'s own `.cache/`.

A partial or extra file blocks phase 5.

```bash
set -euo pipefail
MODEL_DIR="$HOME/.local/share/omlx-trial/models/GLM-5.3-Flash-MLX-mixed-4_8bit"
cd "$MODEL_DIR"
fail=0
while read -r sha size path; do
  [ "$(stat -f %z "$path")" = "$size" ] || { echo "SIZE MISMATCH $path"; fail=1; }
  [ "$(shasum -a 256 "$path" | cut -d ' ' -f 1)" = "$sha" ] || { echo "SHA256 MISMATCH $path"; fail=1; }
done <<'LFS'
73a4510722d6a9f0b21f04b72be754148f325490932c9636d815a0dffa333419 10768135545 model-00001.safetensors
d94f0f7d0f0ce1a640a0160e8ca61a368591911cd8ed8be747694a830859d895 10013270484 model-00002.safetensors
a713c14e0a9becaeb97a33ed5df6f68b469588f833d433e4e83fb94d1fb12d98 11243573810 model-00003.safetensors
993a7ab0211a0d00c5c29c012e6e59be1348310521eaeda8e1265eda799a64ba 10030900511 model-00004.safetensors
786789beaf0327c274da1edf3ee56a56d29e5062f575091cee1005b2c5c49bc2 11225944103 model-00005.safetensors
b605ec556e7c62233bac13094cc3f38222bbecdbd24e35f3422f534dcc1f4b85 10030900512 model-00006.safetensors
6df1fd4602b10b76276d796e5a7be775e4a4f15cf7f685a3c3a026cd2ad9df8b 11195073231 model-00007.safetensors
47cae75ca2e192ad30c6bec1edf7d7c0c3412157bbe302a533e4e99a19ca22d7 10061771298 model-00008.safetensors
74c874cb13e2eb39a6b21605487e81badd0b8b352ed0249db0f5ba21d68ecc11 11212703107 model-00009.safetensors
b4f993a19def03fe33a6e4cb5590b7f3c9d2ecbe5e5358923401aef8d5bac7ac 10013270568 model-00010.safetensors
bbaf71eac6fcb5f5816bf17ce3db990a2c5a4bfd019af32268fa9c1296e817e8 11243573945 model-00011.safetensors
ca6a71bbcb8ec31c09ba7454c328bc997a25ca0a2fb4fbfd4431d75c16fa24c5 10030900540 model-00012.safetensors
a53b55e0ed565b664061ca27db35cbeece84cffbe0913e75e65aff60ef6779d9 11225944091 model-00013.safetensors
6b1759f7c7d018d4eec1a21fd4a4539f7369fcea49de0ec4bb58304af7a54a27 10030900546 model-00014.safetensors
0555b5b47d7e9417530b9468d24c79cde2ddcf8b1631ed30e0be10eacfc62f7c 11195073261 model-00015.safetensors
934e4ed47caa958df71206c3a4b7bb63241ed2b8ec9f3854e2c52009ebe96314 10061771366 model-00016.safetensors
4f6a94d9068cbb955c31ee258fb4432368fcc51421a21c0a7d248155c12191da 11212703101 model-00017.safetensors
19c2269851ad9d0197124dde38f085778ac04460a1032293bb0a9b6fe6e98c95 1127489842 model-00018.safetensors
19e773648cb4e65de8660ea6365e10acca112d42a854923df93db4a6f333a82d 20217442 tokenizer.json
LFS
while read -r oid size path; do
  [ "$(stat -f %z "$path")" = "$size" ] || { echo "SIZE MISMATCH $path"; fail=1; }
  [ "$(git hash-object "$path")" = "$oid" ] || { echo "BLOB MISMATCH $path"; fail=1; }
done <<'GIT'
52373fe24473b1aa44333d318f578ae6bf04b49b 1570 .gitattributes
986b06fb2916b3518b82c7dcc0870723b91f2f92 1070 LICENSE
1cfe2a0773bd7adc5de3ca7062dbdc08f922131b 5444 README.md
fb94d40d72a2eefa0c7140cb66d941656be16969 10644 chat_template.jinja
232e9b0fbd99efd10feb772085678f680c286983 124463 config.json
637ee6afa5c6ebfba4eb24bd5b9df68550143597 194 generation_config.json
adfa841c0c27900f8b7347a93f0dc81f77bf514e 270900 model.safetensors.index.json
3ec2a058b2cdec556eca0f621e8c5164eec213c2 909 processor_config.json
e375fa0a4e16660ce0e2026e39374d35dad4c079 761 tokenizer_config.json
GIT
[ "$(find . -path ./.cache -prune -o -type f -print | wc -l | tr -d ' ')" = 28 ] || { echo "UNEXPECTED FILE SET"; fail=1; }
[ "$fail" = 0 ] && echo "checkpoint verified: 28/28 files" || { echo "VERIFICATION FAILED: do not load"; exit 1; }
```

The full verification reads 169 GiB, so expect minutes of SSD time.

### Phase 5: enable through clean, pushed main (not executed)

This is the exact opt-in edit, made only after phases 3-4 pass:

```text
modules/hosts/studio.nix, services.omlx:   enable = false;   ->   enable = true;
```

Commit it directly to `main`, push `origin main`, and verify that `origin/main` contains it.
Then apply it with Bryan's standard `update-system`, from the clean, pushed checkout at
`/Users/bryan/code/nix-configs`.

Never activate from a worktree, an alternate flake path, or a temporary store output. The
agent starts at activation. It loads **no** model, because none is pinned and the preflight
refuses a pinned one.

### Phase 6: verify one loopback server, then commission the model (executed in the foreground; see §9)

On 2026-10-16 these checks ran against a foreground server, not the launchd agent, so the
`launchctl` and `/tmp/omlx.error.log` lines did not apply. The settings `PUT` ran verbatim
and persists in `model_settings.json` for the agent.

`omlx_api` reads the key from `settings.json` and passes it to curl on stdin (`-K -`), so it
never appears in `ps`.

The pinned CLI sets the native process title to `omlx-server`
(`omlx/cli.py:184`, `omlx/process_title.py:8-27`). Process probes below match that
exact title, not the original `omlx serve` command line.

```bash
set -euo pipefail
STATE="$HOME/.local/share/omlx-trial"
omlx_api() {
  local method=$1 path=$2
  shift 2
  /usr/bin/python3 -I -c 'import json, sys; print("header = \"Authorization: Bearer %s\"" % json.load(open(sys.argv[1]))["auth"]["api_key"])' "$STATE/settings.json" |
    curl -fsS -K - -X "$method" "$@" "http://127.0.0.1:8000$path"
}
launchctl print "gui/$(id -u)/org.nixos.omlx" | grep -E 'state = |pid = |last exit code'
tail -n 5 /tmp/omlx.error.log                             # a preflight refusal says why here
/usr/sbin/lsof -nP -iTCP:8000 -sTCP:LISTEN                # exactly one listener, 127.0.0.1:8000
pgrep -x 'omlx-server' | wc -l | tr -d ' '                # 1
curl -fsS http://127.0.0.1:8000/health
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/v1/models   # 401: the key is enforced
curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' \
  -d '{"api_key":"x","api_key_confirm":"x"}' http://127.0.0.1:8000/admin/api/setup-api-key   # 400: already configured
omlx_api GET /v1/models                                   # note the exact advertised model ID
omlx_api GET /api/status                                  # native kernel status and memory ceiling
```

The checkpoint directory name should be the ID (`GLM-5.3-Flash-MLX-mixed-4_8bit`). Use
whatever `/v1/models` advertises.

Set the per-model trial settings through the verified admin API
(`PUT /admin/api/models/{id}/settings`, `omlx/admin/routes.py:2691`). The request model
forbids unknown fields:

- offload at the derived fraction;
- MTP off (it is not applicable to this checkpoint);
- not pinned;
- an 8K prompt cap;
- a `low` effort default, so nothing silently runs at Max.

```bash
set -euo pipefail
STATE="$HOME/.local/share/omlx-trial"
MODEL_ID=GLM-5.3-Flash-MLX-mixed-4_8bit
omlx_api() {
  local method=$1 path=$2
  shift 2
  /usr/bin/python3 -I -c 'import json, sys; print("header = \"Authorization: Bearer %s\"" % json.load(open(sys.argv[1]))["auth"]["api_key"])' "$STATE/settings.json" |
    curl -fsS -K - -X "$method" "$@" "http://127.0.0.1:8000$path"
}
omlx_api PUT "/admin/api/models/$MODEL_ID/settings" -H 'Content-Type: application/json' --data-binary '{
  "moe_expert_offload_enabled": true,
  "moe_expert_offload_resident_fraction": 0.10,
  "mtp_enabled": false,
  "is_pinned": false,
  "max_context_window": 8192,
  "max_tokens": 4096,
  "chat_template_kwargs": {"reasoning_effort": "low"}
}'
```

### Phase 7: first load and memory evidence (executed in the foreground; see §9)

**Before loading GLM, confirm Ollama is idle**, and leave its service and routes alone.

- If `/api/ps` is not empty, wait or postpone; do not stop Ollama.
- A maintenance window that unloads Ollama models is a separate, explicit operator decision.

Recompute the budget from live memory, and keep the start at 0.10 unless the budget says to
go lower.

```bash
set -euo pipefail
curl -fsS http://127.0.0.1:11434/api/ps                 # must be {"models":[]}
sysctl -n vm.swapusage
memory_pressure -Q | tail -n 1
/usr/bin/python3 -I - <<'PY'
import re, subprocess

out = subprocess.run(["/usr/bin/vm_stat"], capture_output=True, text=True, check=True).stdout
page = int(re.search(r"page size of (\d+) bytes", out).group(1))
pages = {k: int(v) for k, v in re.findall(r"Pages (free|inactive):\s+(\d+)", out)}
gib = 1024 ** 3
available = (pages["free"] + pages["inactive"]) * page / gib
budget = available - 16 - 25 - 8   # safe-tier reserve, one Ollama model, GLM KV/prefill (re-measure)
fits = [f for f in (0.10, 0.125, 0.15, 0.20)
        if 9.961 + 42 * 3.797 * min(288, max(8, round(288 * f))) / 288 <= budget]
print(f"free+inactive {available:.1f} GiB, weight budget {budget:.1f} GiB, fractions that fit: {fits}")
PY
```

If no fraction fits, stop and postpone.

Load the model explicitly, then send one short request with an explicit effort.

- `usage` carries oMLX's timing fields (`omlx/api/openai_models.py:457-469`): load duration,
  time to first token, prefill and decode durations and rates.
- The server log carries the per-request expert-cache line
  (`MoE offload: request=… hit_rate=… misses=… fetched=…`, `omlx/scheduler.py:11066`).

```bash
set -euo pipefail
STATE="$HOME/.local/share/omlx-trial"
MODEL_ID=GLM-5.3-Flash-MLX-mixed-4_8bit
omlx_api() {
  local method=$1 path=$2
  shift 2
  /usr/bin/python3 -I -c 'import json, sys; print("header = \"Authorization: Bearer %s\"" % json.load(open(sys.argv[1]))["auth"]["api_key"])' "$STATE/settings.json" |
    curl -fsS -K - -X "$method" "$@" "http://127.0.0.1:8000$path"
}
time omlx_api POST "/v1/models/$MODEL_ID/load"
grep -E 'moe expert offload: wrapped|Lightning MTP|native|fused' "$STATE/logs/server.log" | tail -n 20
ps -o pid,rss,vsz,command -p "$(pgrep -x 'omlx-server')"
omlx_api POST /v1/chat/completions -H 'Content-Type: application/json' --data-binary '{
  "model": "GLM-5.3-Flash-MLX-mixed-4_8bit",
  "messages": [{"role": "user", "content": "In two sentences, what is a mixture-of-experts model?"}],
  "reasoning_effort": "low",
  "max_tokens": 512,
  "temperature": 0
}' | /usr/bin/python3 -I -c 'import json, sys; r = json.load(sys.stdin); print(r["usage"]); print(r["choices"][0]["message"]["content"][:400])'
grep 'MoE offload: request=' "$STATE/logs/server.log" | tail -n 1
sysctl -n vm.swapusage
memory_pressure -Q | tail -n 1
```

Record each of these:

- settings, the fraction and its budget;
- load time;
- RSS after load and after the request;
- the Metal cap from phase 3c';
- free and inactive memory before and after;
- the change in swap;
- the expert hit/miss counters;
- the backend path: wrapped-layer count, kernel status, "Lightning MTP is inactive", and
  that fused decode is unavailable on gen 14;
- prompt tokens, time to first token, prefill and decode rates, and total time.

The context cap stays at 8K, and the existing 64K workflow is **not** claimed until it has
been exercised.

**Stop criteria.** Unload GLM and keep the evidence if any of these occurs:

- swap use grows;
- `memory_pressure` reports a warning or critical state;
- free memory falls below 10%;
- Ollama or the desktop becomes unresponsive;
- an engine error or crash occurs.

```bash
set -euo pipefail
STATE="$HOME/.local/share/omlx-trial"
omlx_api() {
  local method=$1 path=$2
  shift 2
  /usr/bin/python3 -I -c 'import json, sys; print("header = \"Authorization: Bearer %s\"" % json.load(open(sys.argv[1]))["auth"]["api_key"])' "$STATE/settings.json" |
    curl -fsS -K - -X "$method" "$@" "http://127.0.0.1:8000$path"
}
omlx_api POST /v1/models/GLM-5.3-Flash-MLX-mixed-4_8bit/unload
# If the server itself is unresponsive, stopping the trial agent is a deliberate operator action:
#   launchctl bootout "gui/$(id -u)/org.nixos.omlx"   (it returns at the next activation or login)
```

Escalate the fraction at most one step per session (0.125, then 0.15, then 0.20), and only
after a clean session, by repeating the settings `PUT`, which reloads the engine.

### Phase 8: task evidence (8a and 8c executed in the foreground; 8b not run; see §9)

All three tasks keep one GLM request at a time, use the manual trial client only, and change
no client configuration. Record cold or warm state, prompt tokens, time to first token,
prefill and decode rates, and **total task time** separately. There is no numeric pass
threshold. Report:

- correctness;
- tool fidelity;
- memory pressure;
- whether Bryan finds it usable.

**8a. Two-turn function call and result continuation.** This uses the fleet probe. The key
is passed by environment variable name and never printed. The per-model effort default
(`low`) applies.

```bash
set -euo pipefail
STATE="$HOME/.local/share/omlx-trial"
OMLX_TRIAL_KEY=$(/usr/bin/python3 -I -c 'import json, sys; print(json.load(open(sys.argv[1]))["auth"]["api_key"])' "$STATE/settings.json")
export OMLX_TRIAL_KEY
time /usr/bin/python3 -I "$HOME/.hermes/skills/mlops/local-model-fleet-governance/scripts/openai_tool_roundtrip.py" \
  --base-url http://127.0.0.1:8000/v1 --model GLM-5.3-Flash-MLX-mixed-4_8bit \
  --api-key-env OMLX_TRIAL_KEY --max-tokens 4096 --timeout 1800
unset OMLX_TRIAL_KEY
```

A pass means: one valid requested call, acceptance of the tool result, and a final answer
that uses it without inventing or repeating calls. A protocol pass is not a quality pass.

**8b. Image fixture.** Run this only if image input is proposed as a use. The fixture is a
deterministic red-left/blue-right PNG; the expected answer is "red on the left, blue on the
right".

```bash
set -euo pipefail
STATE="$HOME/.local/share/omlx-trial"
omlx_api() {
  local method=$1 path=$2
  shift 2
  /usr/bin/python3 -I -c 'import json, sys; print("header = \"Authorization: Bearer %s\"" % json.load(open(sys.argv[1]))["auth"]["api_key"])' "$STATE/settings.json" |
    curl -fsS -K - -X "$method" "$@" "http://127.0.0.1:8000$path"
}
/usr/bin/python3 -I - > "$STATE/fixture-request.json" <<'PY'
import base64, json, struct, zlib

w, h = 64, 32
pixel = lambda x: b"\xff\x00\x00" if x < w // 2 else b"\x00\x00\xff"   # red left, blue right
rows = b"".join(b"\x00" + b"".join(pixel(x) for x in range(w)) for _ in range(h))
chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")
url = "data:image/png;base64," + base64.b64encode(png).decode()
print(json.dumps({
    "model": "GLM-5.3-Flash-MLX-mixed-4_8bit",
    "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": url}},
        {"type": "text", "text": "Which colour is on the left half and which on the right half? Answer in one short sentence."},
    ]}],
    "reasoning_effort": "low", "max_tokens": 256, "temperature": 0,
}))
PY
omlx_api POST /v1/chat/completions -H 'Content-Type: application/json' --data-binary @"$STATE/fixture-request.json" |
  /usr/bin/python3 -I -c 'import json, sys; r = json.load(sys.stdin); print(r["choices"][0]["message"]["content"]); print(r["usage"])'
```

**8c. Read-only repository investigation with graded answers.** This is symbol tracing with
cited file and line, run at `high` effort.

The prompt is built from nix-configs' `modules/services/omlx-preflight.sh` on published main,
numbered with `nl -ba`, so the answers are fixed.

- **Q1.** Which line refuses a pinned model, and with what exit status?
- **Q2.** Which `settings.json` conditions must hold before start?
- **Q3.** What happens when the runtime executable is missing?

Expected answers, from the candidate file:

- **A1.** Line 47 calls `refuse` (lines 44-48), and `refuse` exits **78** at line 26.
- **A2.** Lines 33-41: an API key string of 32 or more characters; `cors_origins == []`;
  `skip_api_key_verification`, `allow_unauthenticated_inference` and
  `distributed_inference_enabled` false or absent; `mcp.config_path` null.
- **A3.** Line 30 refuses with exit 78 before anything runs; `exec` (line 59) is never
  reached.

If main's line numbers differ, regrade from `nl -ba`.

```bash
set -euo pipefail
STATE="$HOME/.local/share/omlx-trial"
omlx_api() {
  local method=$1 path=$2
  shift 2
  /usr/bin/python3 -I -c 'import json, sys; print("header = \"Authorization: Bearer %s\"" % json.load(open(sys.argv[1]))["auth"]["api_key"])' "$STATE/settings.json" |
    curl -fsS -K - -X "$method" "$@" "http://127.0.0.1:8000$path"
}
nl -ba /Users/bryan/code/nix-configs/modules/services/omlx-preflight.sh |
  /usr/bin/python3 -I -c '
import json, sys
source = sys.stdin.read()
question = ("You are reviewing this numbered shell script, modules/services/omlx-preflight.sh. "
            "Cite file:line for every claim.\n\n" + source + "\n\n"
            "Q1. Which line refuses a pinned model, and with what exit status?\n"
            "Q2. Which settings.json conditions must hold before start?\n"
            "Q3. What happens when the runtime executable is missing?")
print(json.dumps({"model": "GLM-5.3-Flash-MLX-mixed-4_8bit", "messages": [{"role": "user", "content": question}],
                  "reasoning_effort": "high", "max_tokens": 4096, "temperature": 0}))' > "$STATE/repo-task.json"
time omlx_api POST /v1/chat/completions -H 'Content-Type: application/json' --data-binary @"$STATE/repo-task.json" |
  /usr/bin/python3 -I -c 'import json, sys; r = json.load(sys.stdin); print(r["choices"][0]["message"]["content"]); print(r["usage"])'
```

A real agent-harness run against the trial endpoint, with tools and multiple steps, is a
later canary and needs a separate decision (phase 9).

### Phase 9: production stays unchanged

During the trial:

- No Ollama model, route, Tailscale Serve entry or setting changes.
- No Hermes, Pi, OpenCode, Open WebUI or cron provider or default changes.
- No scheduler points at port 8000.

Pointing one client at the trial is a later, explicitly accepted canary. One model serving
correctly does not show that every client works.

### Phase 10: rollback (not executed)

1. Unload GLM if it is loaded (phase 7, stop block).
2. Set `services.omlx.enable = false;` in `modules/hosts/studio.nix`, commit to `main`, push,
   and run `update-system` from the clean, pushed checkout. nix-darwin removes the agent.
3. Verify the trial is gone and Ollama is intact:

```bash
set -euo pipefail
launchctl print "gui/$(id -u)/org.nixos.omlx" >/dev/null 2>&1 && echo "agent still loaded" || echo "agent removed"
/usr/sbin/lsof -nP -iTCP:8000 -sTCP:LISTEN || echo "no listener on 8000"
pgrep -x 'omlx-server' || echo "no omlx process"
curl -fsS http://127.0.0.1:11434/api/version
curl -fsS http://127.0.0.1:11434/api/tags | /usr/bin/python3 -I -c 'import json, sys; print(sorted(m["name"] for m in json.load(sys.stdin)["models"]))'
tailscale serve status | grep -n 11434
```

Expected results:

- Ollama's tags are the three listed in §2.
- The 11434 TCP forward is unchanged.
- The trial preserves `~/.local/share/omlx-trial`: runtime, weights, settings, logs and cache.
- The trial also leaves oMLX's `~/.omlx/bin/omlx-cluster-python` (R9) in place.

Nothing is deleted automatically. Removing weights, the runtime or `~/.omlx` is a separate,
explicit choice: `rm -rf` of exactly those paths, never other models.

## 7. Decision table

**Bryan's decision (2026-10-16): do not pursue GLM-5.3-Flash on the current setup.** The
Studio already has other responsibilities; pushing RAM usage to make this model faster
does not fit that shared workload. The measured SSD-offload speed is not usable for him.
Treat this as a possible future-hardware option, not ongoing tuning, a smaller-model
canary, an activation task, or an Ollama migration. No replacement Mac purchase is decided.
The trial server is already stopped and the service disabled. Runtime, checkpoint and
evidence are retained; this decision does not authorize deleting them or uninstalling tools.

These are the possible outcomes after phases 6-8. The trial does not migrate anything.

| Evidence | Decision | Next action |
|---|---|---|
| Loads within budget, tool round trip passes, repository answers correct, no memory pressure, and Bryan finds the speed usable for on-demand large-model work | **Keep oMLX on demand** | Leave the service enabled with the idle timeout. Any client canary is a separate decision |
| GLM is correct but too slow, or memory-bound, at the fractions that fit | **Assess one smaller-model canary** on oMLX (one model, explicitly chosen and downloaded under its own approval), or keep the service disabled | Do not assume Ollama NVFP4 checkpoints port to oMLX |
| Load or compatibility failure, wrong tool protocol, memory pressure at 0.10, instability (R17) or an unacceptable security residual | **Abandon the trial** | Phase 10 rollback. Weights and runtime are kept until a separate deletion decision |

Each of these needs its own decision, made after the evidence: retiring Ollama, changing
its Tailscale route, adding more large models, deleting weights, or changing any agent
default.

## 8. Candidate validation (2026-10-16, this change only)

These checks ran locally without activation; the parent's report lists exact output.

- **Module tests.** `python3 scripts/test_omlx_module.py` covers:
  - the Studio default;
  - the Ollama and Open WebUI settings unchanged;
  - other hosts;
  - the NixOS stub;
  - the enabled plist contract;
  - refusals of unsafe options;
  - the preflight against a stand-in executable, with no `PATH`, under the plist's bash and
    jq;
  - this runbook's bash syntax, and its phase-3d seed accepted by the preflight.
- **Existing tests.** The other Studio-evaluating tests were run.
- **`nix flake check`** was run.
- **Studio system derivation.** It was evaluated disabled and with the enabled fixture. The
  disabled derivation equals the one at `9fd29a4`.
- **Shell syntax.** `nixpkgs-fmt`, `shellcheck` and `git diff --check`; `bash -n` over every
  bash block here; the Python heredocs compiled.

## 9. Execution record (2026-10-16, approved installation workers)

Bryan approved the four-item bundle (phase 2) and the live trial, per the plan's "Approved
execution continuation". Raw nonsecret evidence is under
`~/.local/share/omlx-trial/evidence/`. The workers did not commit, push, activate, register
launchd or touch Ollama.

### Preconditions, rechecked at 19:54Z

| Check | Result |
|---|---|
| Hardware | M2 Ultra, 137,438,953,472 B; macOS 27.0.1 (26A434) |
| Disk | 458.6 GiB free |
| Port 8000 | No listener |
| Ollama | `/api/ps` was `{"models":[]}` |
| Swap | 0 |
| `iogpu.wired_limit_mb` | 0 |

### Installs

| Item | Result | Size |
|---|---|---|
| (a) Metal Toolchain | **Installed.** `xcodebuild -showComponent` reports Build 27A266a and `Status: installed` (toolchain `com.apple.dt.toolchain.Metal.32023.921.5`). `xcrun -sdk macosx metal -v` prints `Apple metal version 32023.921 (metalfe-32023.921.6)` and exits 0. The 838.9 MB download took 14 s | 872 MiB asset |
| (b) Rust build tools | **Built** from nix-configs' locked nixpkgs: `cargo-1.98.1` (reports `cargo 1.98.0 (797e8a9bc 2026-08-05)`) and `rustc-1.98.1` (`48a229cea`). They are in the store with `--no-link` only, nothing on PATH | ~0.95 GiB |
| (c) oMLX runtime | **Installed** from `53ab0e66` with native kernels and verified; see "Runtime" below | 5.4 GiB |
| (d) Checkpoint | **Downloaded and verified.** See below | 169.59 GiB |

### Checkpoint

- **Source.** `pipenetwork/GLM-5.3-Flash-MLX-mixed-4_8bit` at
  `d43ea8b407ce4e9c25e6ac9baec3feab70d9f5f3` (the server's `x-repo-commit` header matched).
- **Download.** Ran from 19:57:16Z to 20:31:01Z (33 min 45 s) at ~94 MiB/s, 4 files at a
  time, all fetches successful.
- **Verification.**
  - The phase-4 block ran verbatim: `checkpoint verified: 28/28 files` in 8 min 52 s.
  - All 19 LFS files matched SHA-256 and size; all 9 small files matched git blob id and size;
    the file set was exactly 28.
  - A separate incremental pass during the download also matched all 28 files.
- **Totals.** 181,944,533,258 B, equal to the Hub repo total. Disk free went from 458.6 to
  287.8 GiB.
- **Loaded** by oMLX at 0.10 residency on 2026-10-16; see "Foreground trial" below.

### Blocker history: the runtime install (resolved)

The first worker's Claude Code auto-mode permission gate refused the phase 3b clone and the
phase 3d seed ("Untrusted Code Integration", then "Auto-Mode Bypass"). That held when the
parent relayed Bryan's approval, and again under a `.claude/settings.local.json` exception,
which the auto-mode classifier does not read. The parent relaunched the worker with the
supported inline `--settings` form of a pinned-source, action-scoped exception. Under it,
phase 3b passed the normal gate on its first presentation at 21:30Z. No permission setting
was changed by a worker.

### Runtime (21:30Z-21:39Z)

- **3b.** `jundot/omlx` cloned with hooks disabled. HEAD is
  `53ab0e66d75fe46e3cfa7ed7a7762d3bbcf21e9c` (`v0.7.1rc1-6-g53ab0e66`), and the tree is
  clean. It was still clean after the kernel build.
- **Build-requirement review.**
  - The four Git dependencies, at their pinned commits, are plain setuptools projects. mlx-lm
    also needs setuptools-scm.
  - All 104 registry packages in the resolution have a usable wheel. Only the five intended
    Rust packages build from source.
  - Their sdists declare maturin (cohere-melody `>=1.9,<2`, pydantic-core `>=1.15.0,<2`,
    rpds-py `>=1.9,<2`, tokenizers `>=1.0,<2`). tiktoken declares setuptools
    `>=62.4`, wheel and **`setuptools-rust>=1.5.2`**. The last one was missing from the
    allowlist.
- **Build lock, re-resolved** wheels-only with the same cutoff and hashes, 12 packages:
  `cmake 4.4.4`, `maturin 1.15.0`, `mlx 0.32.4`, `mlx-metal 0.32.4`, `nanobind 3.1.0`,
  `packaging 26.3`, `semantic-version 2.10.0`, `setuptools 84.0.0`, `setuptools-rust 1.13.0`,
  `setuptools-scm 10.3.4`, `vcs-versioning 2.6.0`, `wheel 0.48.0`. Every declaration above is
  satisfied.
- **Runtime lock.** 109 packages, including `transformers 5.17.0`, `huggingface-hub 1.33.0`
  and `numpy 2.3.5`, with no torch. The four Git requirements are at exactly the reviewed
  commits.
- **Sync.**
  - The first attempt failed linking tokenizers 0.23.3 with undefined `_Py*` symbols. Its
    sdist keeps `-undefined dynamic_lookup` in `bindings/python/.cargo/config.toml`, which
    Cargo never reads from the sdist root.
  - The retry passed `CARGO_TARGET_AARCH64_APPLE_DARWIN_RUSTFLAGS` with those two flags.
    Those are PyO3's documented macOS setting and the same as pydantic-core's root config.
    No pin, hash or constraint changed.
  - The retry took 63 s and installed 118 packages.
- **Editable install.** Offline, with no build isolation: `omlx 0.7.1rc1`.
- **Kernels.** `mlx.extension` generates a CMake project requiring 3.25, with no
  FetchContent. Studio has no `cmake` on `PATH`, so the build put the venv's locked cmake
  first. In 31 s it built 5 extensions and 6 metallibs, with deprecation warnings only.
- **3c'.**
  - Versions: `mlx`/`mlx-metal` 0.32.4, `mlx-lm 0.31.4.dev132+g94cdcae13`,
    `mlx-vlm 0.7.8`, `transformers 5.17.0`, `huggingface-hub 1.33.0`, `omlx 0.7.1rc1`.
  - GPU `applegpu_g14d`; `max_recommended_working_set_size` 115,448,725,504 B (107.5 GiB).
  - The source-built Rust extensions load under macOS 27.0.1's dyld, and all five native
    kernels are available.
- **3d.** `settings.json` was commissioned 0600. The key was never printed.
- **Size.** The runtime directory is 5.4 GiB.

### Foreground trial (21:39Z-21:50Z)

The server was started outside launchd with exactly the module's `ProgramArguments` and
`EnvironmentVariables`. They were evaluated from the published module with `enable` forced
on, and the preflight's store copy is byte-identical to `origin/main`. It was healthy after
~4 s. The memory enforcer started at `safe`, with a 61.9 GB ceiling. oMLX advised raising
`iogpu.wired_limit_mb`; that was not done (§3, R20; kernel limits are out of scope). Startup
published `~/.omlx/bin/omlx-cluster-python` (R9).

**Phase 6.** All checks passed:

- one listener, on `127.0.0.1:8000`, owned by the single `omlx-server` process (the
  preflight `exec`s);
- `/v1/models` without the key returned 401;
- `setup-api-key` returned 400;
- a cross-origin preflight returned 400 with no allow-origin header;
- `/v1/models` advertises `GLM-5.3-Flash-MLX-mixed-4_8bit`;
- `/api/status` shows all five native kernels available.

The settings `PUT` returned success, with `requires_reload: false`. It persisted offload
0.10, MTP off, unpinned, an 8192 context, 4096 max tokens and effort `low`, so the
preflight's pin check still passes.

**Phase 7.**

- Ollama was idle. Live free+inactive was 76.7 GiB, leaving a weight budget of 27.7 GiB, so
  only 0.10 fits, as derived.
- The load took **5.0 s**. The log shows "wrapped 42 layers at 10.0% residency (expert
  tables: 171.23 GB total, 17.24 GB resident)", which matches §4.5's 159.47 GiB and 16.06
  GiB. It also shows "skipping gate/up fusion on the VLM path". No Lightning MTP line
  appeared.
- The short generation answered correctly in two sentences:

| Request | Prompt | TTFT | Output | Decode | Total | Expert cache |
|---|---:|---:|---:|---:|---:|---|
| 7: short generation (cold, `low`) | 25 | 15.21 s | 77 | 2.61 tok/s | 44.7 s | hit 47.3%, 16,111 misses, 228,064 MB fetched |
| 8a turn 1 (`tool_choice: required`) | 215 | 28.25 s | 13 | 2.18 tok/s | 34.2 s | hit 17.6%, 176,282 MB |
| 8a turn 2 (213 cached) | 247 | 9.32 s | 10 | 2.49 tok/s | 13.3 s | hit 29.8%, 84,439 MB |
| 8c: repository task (`high`) | 1007 | 36.78 s | 834 (188 reasoning) | 2.15 tok/s | 424.2 s | hit 41.5%, 2,426,088 MB |

**8a: pass.** There was exactly one `lookup_canonical_fact` call with `{"topic": "fleet"}`,
it accepted the result, the continuation repeated no call, and the final answer was exactly
the sentinel. Total time was 47.6 s.

**8c: correct, 3 of 3**, with file:line citations:

- A1: line 47 → `refuse` → `exit 78` at line 26.
- A2: all six conditions at lines 34-39, with the refusal at 41. It also cited the header
  rationale.
- A3: line 30, exit 78, and `exec` at 59 is never reached.

One imprecision: it called line 40 "the `if !`" (the `if` opens at 33).

**8b (image) was not run**, because image input is not proposed as a use.

**Memory** (127 samples, every 5 s):

- Swap stayed 0. The kernel pressure level stayed 1 (normal), and a 5 s watch never fired.
- The minimum free percentage was 58%. Ollama never loaded a model.
- Wired memory went from 7.4-10.1 GiB at baseline to a **44.75 GiB peak**. oMLX reported
  34.49 GB of model memory against a 65.94 GB limit. Process RSS peaked at 11.0 GiB.
- The compressor grew from 3.3 to 8.8 GiB.
- Free pages fell to ~0.1 GiB as the page cache filled with checkpoint reads.

No stop criterion was met.

**Reading.** Behaviour, tool protocol and grounded code reading were correct at 0.10.
Throughput is SSD-bound: decode ran at 2.2-2.6 tok/s, and the expert cache hit 18-47%. The
repository answer took 7 minutes. Whether that is usable is Bryan's call (§7). Escalating
the fraction is not possible within today's budget. The 64K workflow is not claimed.

**Shutdown.**

- The model was unloaded, and `SIGTERM` stopped the server in ~2 s.
- Afterwards, nothing listens on 8000, no `omlx-server` process exists, and
  `org.nixos.omlx` is not loaded.
- Ollama is 0.40.5, idle, and its three tags are unchanged.
- The KV cache holds 580 MiB, and 283.6 GiB of disk is free.

Raw nonsecret evidence is under `~/.local/share/omlx-trial/evidence/` (`03b-*`, `03c-*`,
`03d-*`, `06-*`, `07-*`, `08a-*`, `08c-*`, `09-shutdown.txt`). API responses there passed a
key-redaction filter.

### Closeout

- The accepted execution record is published at `168a07a`.
- Bryan's §7 decision closes the experiment for the current setup; no activation or
  further tuning is pending.
- Runtime, checkpoint and evidence remain on disk; any cleanup is a separate decision.
- **Not to rerun:** phases 3 and 4.

### Parent verification (2026-10-16)

The parent independently inspected the raw load, generation, tool round-trip, repository
answer and memory/shutdown evidence, checked the repository answer against the real
preflight, and re-ran the installed runtime/version and native-extension checks. All five
native kernels are available. The runtime source still has the approved HEAD and a clean
tree. All 92 repository tests and `nix flake check` passed; 17 runbook Bash blocks passed
`bash -n`, and 18 embedded Python fragments parsed. Existing Nix evaluation/deprecation
warnings and the omitted incompatible Linux system were reported, not treated as failures.

The parent also rechecked the live post-trial state: no port-8000 listener or `omlx-server`,
no loaded trial launchd agent, zero swap, and Ollama 0.40.5 idle. This is evidence review
plus direct runtime and post-trial verification, not a second inference benchmark.
