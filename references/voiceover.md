# 语音旁白合同

本文说明 `voiceoverMode=edge-tts | minimax | doubao` 的 provider、配置、样音、完整旁白、canonical WAV、真实时间轴、恢复/stale 和外部验收边界。Disabled 模式不安装、不调用语音 provider；其正式交付仍需按 [字幕合同](subtitles.md) 烧录 source SRT。topic/text 的 provider 不由用户选择，而是唯一读取 `config/voice-providers.local.json` 的 `activeProvider`；其内容确认和 source package 合同见 [内容入口合同](content-input.md)。

## 目录

- [能力边界](#能力边界)
- [环境与配置](#环境与配置)
- [项目文件](#项目文件)
- [整轨合成与身份分层](#整轨合成与身份分层)
- [样音关卡](#样音关卡)
- [完整旁白与恢复](#完整旁白与恢复)
- [Canonical 完整音频与时间轴](#canonical-完整音频与时间轴)
- [完整旁白与真实时长批准](#完整旁白与真实时长批准)
- [时间坐标与渲染绑定](#时间坐标与渲染绑定)
- [最终封装与批准](#最终封装与批准)
- [stale 矩阵](#stale-矩阵)
- [退出码](#退出码)
- [自动测试与真实外部验收](#自动测试与真实外部验收)

## 能力边界

当前支持两个语音 provider：

- provider/protocol：`edge-tts`。
- provider contract：`edge-tts-python-7.2.8-v1`。
- 默认 voice：`zh-CN-YunjianNeural`。
- 默认 language：`zh-CN`。
- 默认 rate/pitch/volume：`+0% / +0Hz / +0%`。
- provider 临时格式：`audio-24khz-48kbitrate-mono-mp3`。
- 正式 canonical 格式：WAV `pcm_s16le / mono / 24000Hz`。

MiniMax T2A V2：

- provider/protocol：`minimax` / `MiniMax`；contract：`minimax-t2a-v2-v1`。
- endpoint：`https://api.minimaxi.com/v1/t2a_v2`，使用 `Authorization: Bearer <apiKey>`。
- 配置中的 `+10%` rate、`+10%` volume、`+0Hz` pitch 分别映射为 MiniMax `speed=1.1`、`vol=1.1`、`pitch=0`；speed 限制为 `0.5–2`，pitch 限制为 `-12–12`。
- 请求使用 `speech-2.8-hd`、`output_format=hex`、32 kHz mono MP3；返回的 hex 必须先解码，再经过 FFmpeg/ffprobe 规范化为本合同的 24 kHz mono canonical WAV。
- API Key 只能放在未提交的 `config/voice-providers.local.json`，不写入 plan、manifest、日志或异常；`trace_id` 只保存不可逆摘要。

MiniMax adapter 仅使用 Python 标准库，不需要安装额外 provider 包。可重试范围与 Edge 相同：DNS/连接、timeout、429、502、503、504；401/403、参数/音色错误、协议或媒体格式错误不自动重试，也不会自动切换 Edge。

MiniMax 额外使用 provider 级共享节流器：`requestsPerMinute`（默认 20）与 `queueIntervalMs` 共同生效，实际请求间隔取两者中更保守的值。该节流器在全部 `voiceGeneration` worker 间共享并加锁，不能让每个线程各自计算间隔。HTTP 429、`Retry-After`，以及 HTTP 200 响应体中的 `rate limit exceeded(RPM)` 都属于明确的可重试限流；收到后全部新请求至少延迟 `rateLimitBackoffMs`（默认 35000ms），只在当前 segment 的有界 attempt 内重试。401/403、非法 voice/参数和协议错误仍是永久失败。

豆包语音 Seed Audio HTTP：

- provider/protocol：`doubao` / `Doubao`；contract：`doubao-seed-audio-http-v1`。
- endpoint：`https://openspeech.bytedance.com/api/v3/tts/create`，使用新版控制台的 `X-Api-Key` 单头鉴权；每次请求另发随机 `X-Api-Request-Id`，不得把 key 或原始请求 ID 写入项目。
- 请求使用 `model=seed-audio-1.0`、`text_prompt`、`references=[{speaker: <voice>}]`，并固定请求 24 kHz WAV；规范化 rate/volume/pitch 分别映射为 `speech_rate`、`loudness_rate`、`pitch_rate`，合法范围为 `-50–100`、`-50–100`、`-12–12`。
- 只读取响应体的 Base64 `audio`，不消费有效期两小时的 `url`；`X-Tt-Logid` 仅保存不可逆摘要。原始 WAV 仍须经过共享 FFmpeg/ffprobe 规范化与 canonical 24 kHz mono 校验。
- 文档未给出完整业务错误码语义，因此只把 DNS/连接/timeout、HTTP 429/502/503/504 和明确限流消息作为有界可重试错误；鉴权、参数/音色、业务错误、协议或媒体错误均永久失败，不自动切换 provider。

豆包 adapter 与 MiniMax 一样只使用 Python 标准库，不增加 provider 包。它复用 `queueIntervalMs` 与 `maxRetries`，共享 adapter 实例在全部 `voiceGeneration` worker 间串行计算请求启动间隔。

Edge TTS 不需要 API Key 或 Base URL，但不是离线模型。它依赖外网和微软语音服务，可能受服务规则、可用性、限流、音色和返回格式变化影响。不得把“无 Key”写成“无需网络”，也不得在失败后自动改 voice/rate、切换 provider 或降级到其他 TTS。

Skill 不调用、不导入、不读取 Yingshu 的 API、数据库、模型配置或 `node_modules`，也不调用其他 Codex skill、PowerShell 包装器或外部 ASR 凭据。旁白对齐使用当前 skill 自带的 Python FunASR runner 和工作区本地模型缓存。当前不支持多角色、SSML、克隆音色、浏览器试听 UI 或自动 voice 推荐。BGM 只支持一个内置 CC0 固定预设：Yoiyami 的《First Light Particles》，`-15 dB`、1.2 秒淡入、1.8 秒淡出、短于成片时循环；用户只在阶段 0 选择加入或不加入，不增加独立试听或批准 Gate。

## 环境与配置

基础环境不会因为没有 `edge-tts` 而失败。只有 Edge 项目显式安装和检查 feature：

```powershell
python scripts/prepare_env.py --check
python scripts/prepare_env.py

<ENV_PY> scripts/prepare_env.py --feature edge-tts
<ENV_PY> scripts/prepare_env.py --check --feature edge-tts

# 首次整轨旁白对齐前准备一次本地 FunASR 模型；正式生成只做只读消费：
<ENV_PY> scripts/prepare_env.py --feature narration-asr
<ENV_PY> scripts/prepare_env.py --check --feature narration-asr
```

`narration-asr` 将 Paraformer 中文 ASR、FSMN-VAD 与 CT-Punc 准备到工作区 `runtime/cache/funasr-models/`，并写入模型 receipt。正式 runner 只读取 receipt 中的本地绝对模型路径，不在旁白生成阶段隐式下载模型，也不读取 Yingshu 或其他 skill 的环境。

`config/voice-providers.example.json` 是无秘密的首版 provider 合同示例，记录 package/contract、voice、language、规范化 rate/pitch/volume、输出格式及请求策略字段。它不是凭据文件，也不允许加入 key、Cookie、Token 或临时 URL。

MiniMax 请求策略字段为 `queueIntervalMs`、`requestsPerMinute`、`rateLimitBackoffMs` 与 `maxRetries`。`requestsPerMinute` 必须位于 1–600，`rateLimitBackoffMs` 必须位于 1000–300000；缺失时分别采用 20 RPM 与 35000ms。豆包使用 `queueIntervalMs` 与 `maxRetries`，并要求本地配置包含 `apiKey`、实际 `voice`、`model=seed-audio-1.0`。降低 `voiceGeneration` 只能减少在途请求和本地资源占用，不能替代 provider 自身限流。

当前样音 CLI 的 provider 永远读取 `config/voice-providers.local.json` 的 `activeProvider`，不提供 `--provider` 覆盖入口；activeProvider 必须与项目已经冻结的 `voiceoverMode` 一致。CLI 仍支持 `--voice` 与整数百分点 `--rate`，MiniMax/豆包未指定 voice/rate 时从 local provider 配置读取。`--rate 0` 规范化为 `+0%`，`10` 为 `+10%`，`-10` 为 `-10%`。持久化 identity 使用规范化字符串，不依赖调用点猜单位。全局示例配置不能自动覆盖项目中已经生成或批准的 `planning/voice-plan.json`。

## 项目文件

```text
planning/voice-plan.json
previews/voice-sample.wav
manifests/voice-manifest.json
audio/segments/unit-0001.wav
audio/narration.wav
audio/timeline.json
audio/narration.srt
planning/timing-plan.json
```

所有路径在 JSON 中保存为项目相对 POSIX 路径。manifest 不保存完整 provider 响应、秘密或本机绝对路径；可选 provider request ID 只能保存不可逆脱敏摘要。

## 整轨合成与身份分层

旁白复用共享的 `scripts/srt_timeline.py`，不维护第二套 SRT 解析口径。完整旁白 planner 使用 `full-track-v1`：

- 全部已确认旁白按 cue 原顺序组成一个 provider 请求，固定只有一个 synthesis task。
- 同一 scene 内连续朗读，scene 之间保留两个换行作为段落边界；provider 仍能看到全文上下文。
- 不再按 12/24/36 code point 拆分，也不因 provider 或 ASR 失败回退成逐句请求。
- 豆包、MiniMax 与 Edge 共用同一整轨 planner；各 provider 若拒绝当前全文长度或合同，按永久 provider 错误 fail-closed，不自动切换 provider 或重新拆句。
- cue/scene mapping 仍进入整轨 unit 的审计与 identity；真正的 cue/scene 毫秒边界在成品 WAV 生成后由 ASR 对齐派生。
- 样音仍从全文中确定性选择一条代表性自然句，不会拿全文生成样音。

身份分为三层，避免源 SRT 只改时间时无意义地重请求音频：

```text
sourceTextIdentityHash
sourceTimingIdentityHash
voiceSynthesisIdentityHash
```

`voiceSynthesisIdentityHash` 覆盖完整规范化朗读文本、稳定 ordinal/range、scene 段落、voice、rate、language、整轨合同和 provider 合同。source 原文件 SHA 仍用于审计；它不是判断整轨音频是否可复用的唯一依据。

voice plan 另有 `voicePlanAuditHash`，覆盖完整审计合同。audit hash 变化会使样音/完整批准、时长决定、timeline、narration SRT 和下游重新判定，但只有 synthesis identity 或正式 WAV 媒体合同变化的 segment 才必须重新请求。

## 样音关卡

从已确认文本中确定性选择代表性中文自然句，生成并规范化样音：

```powershell
<ENV_PY> scripts/generate_voiceover.py sample `
  --project <项目根目录> `
  --voice zh-CN-YunjianNeural `
  --rate 0
```

成功输出：

```text
SAMPLE_AUDIO=<项目根目录>\previews\voice-sample.wav
SAMPLE_REVIEW_AUDIO=<项目根目录>\previews\voice-sample-<voice>-<identity前12位>.wav
SAMPLE_REQUEST_AUDIT=<同名脱敏审计 JSON>
SAMPLE_VOICE_ID=<本次请求 voice id>
SAMPLE_AUDIO_SHA256=<current WAV SHA-256>
SAMPLE_IDENTITY=<64位 sha256>
```

`SAMPLE_AUDIO` 仍是供批准合同使用的 canonical current 文件；`SAMPLE_REVIEW_AUDIO`
是字节完全相同、按 voice 与 identity 唯一命名的不可变试听副本，交付用户时必须优先使用它，
避免桌面播放器或浏览器按固定路径缓存上一版样音。脱敏审计只记录 provider/model、规范化
voice/rate/volume/pitch、identity、媒体 SHA/bytes/duration，以及 provider 是否回显实际
voice id；不得保存 API Key、Authorization、正文或完整 provider 响应。若 provider 不回显
voice id，审计必须明确记录 `voiceIdEchoAvailable=false`，不得把“客户端已发送”表述为
“服务端已确认采用”。

技术校验只说明 WAV 可读、媒体合同正确，不代表 voice/rate 已获接受。必须播放并完整听取 current 样音后作出明确决定：`agentApprovalEnabled` 缺失/为 `false` 时等待用户确认，为 `true` 时由具备真实听音能力的 coordinator 审阅；审阅能力不足时报告 `BLOCKED`。明确接受后才执行：

```powershell
<ENV_PY> scripts/generate_voiceover.py approve-sample `
  --project <项目根目录> `
  --identity-hash <刚完整试听的 SAMPLE_IDENTITY>
```

identity 必须仍是 current sample。错误或 stale identity 返回 5，不修改旧批准。未批准 current 样音时，`full` 必须返回 5。

## 完整旁白与恢复

跨阶段状态、identity、stale、attempt 恢复、自动重试与
`unknown_external_outcome` 的权威规则见
[`recovery-and-identity.md`](recovery-and-identity.md)。本节只补充语音 segment、WAV、
timeline 和 narration SRT 的阶段绑定。

```powershell
<ENV_PY> scripts/generate_voiceover.py full --project <项目根目录>

# 中断或部分失败后：
<ENV_PY> scripts/generate_voiceover.py full `
  --project <项目根目录> --retry-failed

# 自动 ASR 未完成时，从同一 current narration.wav 恢复：
<ENV_PY> scripts/generate_voiceover.py publish-alignment `
  --project <项目根目录> --asr-srt <FunASR句级字幕.srt>
```

`execution.concurrency.voiceGeneration` 仍保留为工作区兼容配置，但 full-track 固定只有一个在途 task，因此 effective concurrency 恒为 0 或 1。provider worker 只产生 attempt candidate；voice manifest、正式 WAV、timeline、SRT、identity 与批准仍由 coordinator 单写：

```text
Edge 临时媒体
  → FFmpeg 规范化为 pcm_s16le/mono/24000Hz WAV
  → ffprobe + RIFF/WAVE/流合同检查
  → SHA-256/bytes/duration
  → 原子写入 .work/<run>/external/uXXXX-aXXXX.wav candidate
  → coordinator 写 candidate_ready / publishing checkpoint
  → coordinator 复制、fsync、原子发布唯一的 audio/segments/unit-0001.wav
  → 发布 canonical audio/narration.wav，状态进入 waiting_alignment
  → 本地 FunASR 生成句级声学 SRT
  → 以已确认 source 原稿校正文字并发布 timeline/narration SRT/FULL_IDENTITY
  → coordinator 核对正式 SHA/bytes 并写 validated
```

正式段必须为普通非空文件，恰好 1 路音频、0 视频，codec/sample rate/channels 正确，duration 大于 0，ffprobe size 与磁盘 bytes 一致。失败候选只留在本次 `.work/voice-generate-<runId>/`；不得覆盖旧正式文件。

新 attempt 的 segment 状态固定为：

```text
pending | prepared | requesting | candidate_ready | publishing |
validated | failed | cancelled | unknown_external_outcome
```

`normalizing` 只作为旧 manifest 的兼容识别状态；新运行不再写入。

恢复规则：

- 唯一整轨 task validated 后立即落盘并登记 hash；ASR/对齐失败不能删除或重请求该音频。
- 内部 ASR 的逐次证据写入当前项目唯一 `.work/voice-align-<run>/`；正式项目只发布沿用现有 identity 链的 narration SRT、timeline 与 manifest 绑定，不新增 ASR Gate 或 identity。
- `--retry-failed` 只处理 failed、cancelled 或未完成的整轨 task。
- synthesis identity、正式 WAV SHA 和媒体合同仍 current 时不重请求、不覆盖。
- `requesting` 且 candidate 存在时直接验证并提升为 `candidate_ready`；`candidate_ready/publishing` 恢复时 provider 调用数必须为 0。
- `requesting` 且 candidate 不存在、provider 又不能按同一幂等键查询时写 `unknown_external_outcome`，即使传 `--retry-failed` 也不得自动重复请求。
- manifest 与正式文件 hash 不一致时失败，不能假装恢复。
- voice plan audit hash 变化不等于所有 segment synthesis identity 都变化。
- 纯 source timing 变化、朗读文本和 scene/分段边界不变时，validated segment/WAV 可复用；时长偏差、批准、timeline 和下游仍要重算。
- canonical WAV 发布失败保留 validated segment；只清理或恢复本次 run，不扫描其他 `.work` 目录。

可重试的外部错误仅包括 DNS/连接、明确 timeout、429、502、503、504；重试次数有限。voice 不存在、配置/协议错误、身份变化、媒体无效、路径越界、FFmpeg 缺失和用户取消不自动重试。

## Canonical 完整音频与时间轴

唯一整轨 task 成功后发布 `audio/narration.wav`，随后直接调用当前 skill 内部 `transcribe_narration` Python API。runner 使用本地 Paraformer + FSMN-VAD + CT-Punc，启用真实句级时间戳，并把原始 SRT、JSON、receipt 和时间校验证据写入本次 `.work/voice-align-<run>/`。ASR 只提供真实声学时间，最终字幕文字必须来自已确认 source 原稿。对齐成功后再原子发布：

```text
audio/narration.wav
audio/timeline.json
audio/narration.srt
```

成功输出：

```text
FULL_AUDIO=<项目根目录>/audio/narration.wav
FULL_IDENTITY=<64位 sha256>
```

完整音频、timeline 与 narration SRT 均 current 后，`full` 才输出 `FULL_IDENTITY`，不再编码 1920×1080 的无画面预审视频。若内部 ASR 或参考原稿对齐失败，canonical `narration.wav` 继续保留，manifest 保持 `waiting_alignment`，已经成功的 TTS 不会重跑；此时只有 `FULL_AUDIO_IDENTITY` 可供技术恢复，不能用于 `approve-full`。环境修复后可再次执行 `full --retry-failed` 复用 current WAV，或用 `publish-alignment` 显式导入同一音频的句级 SRT 继续，不新增人工 Gate。

`audio/timeline.json` 必须满足：

- unit 0 从全局 0 开始，所有 unit 连续、无空洞、无重叠。
- 最后 unit 以整轨 `ffprobe` 实测 duration 收口。
- scene 保留已批准语义 cue 边界，连续覆盖全部 unit，最后一幕以整轨时长收口。
- 每幕记录全局 `startMs/endMs` 及累计计算的 `startFrame/endFrameExclusive/frameCount`。
- 包含 source SRT、voice plan audit、audio 与 narration SRT 的 current binding。

`audio/narration.srt` 的文字逐字来自已确认 source 原稿，时间来自最终整轨 WAV 的 FunASR 句级声学边界；从 0 连续覆盖真实音频，cue 不跨 scene。它不复制 source SRT 的 provisional 时间戳，也不能由字幕阶段手工改写。所有音频旁白模式唯一使用该文件。

只读技术验证：

```powershell
<ENV_PY> scripts/generate_voiceover.py status --project <项目根目录>
<ENV_PY> scripts/validate_voiceover.py --project <项目根目录>

# 验证器合同升级或需要重新取得完整解码证据时：
<ENV_PY> scripts/validate_voiceover.py --project <项目根目录> --force-deep
```

`status` 输出 voice plan、sample、各 segment 状态计数和 full approval 摘要；它不修改项目。`validate_voiceover.py` 验证 current WAV、timeline 与 narration SRT，成功输出 `VOICEOVER_VALIDATED=1` 和 current identity，也不会写人工批准。

## 完整旁白与真实时长批准

完整旁白生成后，指定审阅主体必须完整试听 `audio/narration.wav`，完成两项明确判断：人工模式为用户，AI 代理模式为具备真实听音能力的 coordinator；能力不足时必须 `BLOCKED`。

1. 配音内容、音色、语速与完整听感可接受，没有漏读、重复、断裂或奇怪停顿。
2. 查看 target/source provisional duration 与真实 audio duration 的差值和比例，决定是否接受真实时长。

`agentApprovalEnabled` 缺失或为 `false` 时，同一条完整旁白确认请求还必须展示本次运行的生成后审阅策略：`user_first` 直接把各阶段通过技术校验的 current artifact 交给用户，`agent_first` 在交付用户前为各阶段准备一次辅助 AI 语义预审。用户应能在一次回复中同时确认完整旁白、作出所需的真实时长决定并选择策略，例如“确认完整旁白，选择 user_first”；不得先完成 `approve-full`，再设置一次只用于选择策略的独立聊天关卡。用户未指定策略时必须继续停在本 Gate 询问，禁止静默默认。

`agentApprovalEnabled=true` 时，审阅策略确定性为 `agent_first`，不再询问 `user_first|agent_first`。coordinator 完整听取并接受 current 旁白后，使用同一 `approve-full --review-policy agent_first` 原子绑定配音、真实时长决定和后续审阅策略。

`audio/narration.srt` 继续作为 current 权威字幕源和技术证据，但此时尚无真实画面，因此不做字幕视觉批准。换行、对比度、遮挡与安全区统一在正式字幕 contact sheet 和最终成片中审查。

两项判断在一次 `approve-full` 原子事务中绑定 current `FULL_IDENTITY`，并校验 narration WAV、timeline 与 narration SRT：

```powershell
# 偏差在 10% 阈值内：
<ENV_PY> scripts/generate_voiceover.py approve-full `
  --project <项目根目录> `
  --identity-hash <刚完整试听旁白所对应的 FULL_IDENTITY> `
  --review-policy user_first

# 偏差超过 10%，且用户明确接受真实时长：
<ENV_PY> scripts/generate_voiceover.py approve-full `
  --project <项目根目录> `
  --identity-hash <FULL_IDENTITY> `
  --duration-decision accept_actual `
  --review-policy agent_first
```

`--identity-hash` 必须是 current full identity；不匹配时返回 5，且不得修改旧批准。`--review-policy` 必填并持久化到 `fullApproval.reviewPolicy`；后续视觉阶段省略参数时读取冻结值，显式冲突时返回 stale。旧批准缺少该字段时可以读取和重新试听，但不得进入视觉阶段，必须对 current identity 重新执行带策略的 `approve-full`。阈值内不得传 `accept_actual` 冒充超阈值人工决定；manifest 应记录 `within_threshold`。超阈值未显式接受时返回 5，另一条合法路径是修改 rate/文本后重新生成、完整试听和批准。

coordinator 在人工模式收到同时包含两项决定的回复后，或在 AI 代理模式完成两项真实审阅决定后，必须先核对 current `FULL_IDENTITY` 并成功执行 `approve-full`，再采用审阅策略并开始生图；旁白被拒绝、identity stale 或超阈值时长未获接受时，不得只凭策略启动任何视觉生成。AI 驳回时只返工受影响的配音/时间阶段并重新完整试听；需实质改写阶段 0 文本或已冻结策略时必须回到用户确认。审阅策略本身不是批准，不进入作品 identity，也不能替代后续线稿、annotation review bundle、scene review bundle 或最终成片批准。

批准成功输出 `FULL_APPROVED_IDENTITY` 和 `TIMING_PLAN`，并原子更新 `planning/timing-plan.json`，使 current audio timeline 成为正式时钟。此操作不能修改图片 `generation-plan.json` 或 generation manifest。

## 时间坐标与渲染绑定

```text
audio/timeline.json scenes[*].startMs/endMs   全局时间
timing plan scenes[*].startMs/endMs           全局时间
annotation.sceneDurationMs                    场景局部总时长
annotation elements.reveal.startMs            场景局部时间，从本幕 0 开始
audio/narration.srt cue start/end             全局时间
```

Edge annotation 必须绑定 current audio SHA、timeline SHA、timing plan SHA、render profile SHA、scene ID 和全局 scene 范围。第二幕及以后不能把全局 scene start 直接写成元素局部 start。任何 binding stale 时，正式渲染返回 5。

正式逐幕渲染、clean 合并、字幕烧录和最终 mux 都消费 timing plan 的累计帧边界；禁止逐幕独立向上取整后相加，也禁止使用 `-shortest` 截断音频或画面。

## 最终封装与批准

全部单幕按 `sceneRender` 有界并行生成和逐幕检查、由 coordinator 按 generation plan 顺序发布，并通过 current scene review bundle 联合批准后，连续生成并技术验证 clean master、烧录 narration SRT，再执行：

```powershell
<ENV_PY> scripts/mux_voiceover.py --project <项目根目录>
<ENV_PY> scripts/validate_final_media.py --project <项目根目录>
```

mux 允许 Edge、MiniMax 或豆包语音模式，要求 current full approval、captioned video、WAV、timeline、字幕和 delivery identity 全部有效。它以 `-c:v copy` 保持已经烧录字幕的视频，以 `AAC 192k / 24000Hz / mono` 编码旁白，原子发布 `output/final.mp4`，输出 `FINAL_IDENTITY`。

若 `project.json.backgroundMusic.enabled=true`，同一个 mux 命令额外读取 skill 内置 CC0 曲目，按固定 `-15 dB` 混入旁白，应用 1.2 秒淡入和 1.8 秒淡出，并在曲目短于成片时循环。最终仍只有一路 AAC；delivery manifest 的 `final.backgroundMusic` 记录曲名、作者、许可证、资产 SHA 与混音参数。若字段为 `false` 或旧项目缺少该字段，保持原有纯旁白封装。BGM 不新增人工 Gate，最终完整看片听音批准同时验收其听感。

完整旁白批准和技术验证都不等于最终 Gate 批准。指定审阅主体必须完整看片听音，确认最终真实画面上的字幕、画面、音频和尾部均无截断后，才允许执行：人工模式由用户审阅，AI 代理模式由具备完整视频与音频查看能力的 coordinator 审阅；能力不足时必须 `BLOCKED`，不得以关键帧、contact sheet 或媒体技术验证代替。

```powershell
<ENV_PY> scripts/approve_final_media.py `
  --project <项目根目录> `
  --identity-hash <刚完整看片听音的 FINAL_IDENTITY>
```

final identity 覆盖 clean video、audio、timeline、权威字幕、样式、字体、render profile、timing plan、burn/mux contract 和 final SHA。任一输入变化都会使最终批准 stale。

## stale 矩阵

完整 stale 传播矩阵的唯一来源是
[`recovery-and-identity.md`](recovery-and-identity.md)。下表保留语音阶段的具体产物视图，
不得独立改变 retry、current binding 或批准语义。

| 变化 | 必须 stale | 可保留 |
|---|---|---|
| topic/body/rewritePolicy/target/narration cue/scene mapping 改变 | content/source package、voice plan、full 批准、音频、timeline、narration SRT 和全部相关下游 | 仅按 current identity 重新判定可复用 segment |
| 仅 imagePrompt 改变，cue/scene boundary 不变 | generation plan、图片和视觉下游 | current 音频、timeline 与 narration SRT |
| voice/rate/朗读文本/分段边界/provider synthesis contract | sample/full 批准、受影响段、WAV、timeline、narration SRT、annotation、场景视频、captioned/final、最终批准 | synthesis identity 未变的其他段；图片需语义复核 |
| source 仅改时间，朗读文本与分段不变 | 时长决定、full 批准、timeline、narration SRT、annotation 和下游 | synthesis identity/current WAV 合法的 segments |
| narration WAV 改变 | full 批准、timeline、annotation 和全部下游 | 图片 |
| timeline/timing plan 改变 | narration SRT、annotation 时序、场景视频、captioned/final、最终批准 | 图片 generation plan/manifest；音频按 identity 调查 |
| render profile 或 mode 改变 | timing、annotation、所有视频与最终批准 | 图片和音频可保留但需重新绑定/复核 |
| captioned video、AAC 参数、BGM 开关/内置资产/固定混音参数或 final SHA 改变 | final、最终批准 | 上游 current 产物 |

stale 文件可作为历史证据保留，但不得作为 current 输入进入下一阶段。不因下游 stale 删除上游仍有效的语音段。

## 退出码

| 码 | 含义 |
|---:|---|
| 0 | 成功且对应技术检查通过 |
| 1 | 批处理有失败/取消 unit |
| 2 | 参数、项目、voice plan、manifest、SRT 或 timeline 无效；不可重试 provider 配置错误也归此类 |
| 3 | Edge 网络、服务或限流重试耗尽 |
| 4 | FFmpeg、ffprobe、WAV 或最终媒体验证失败 |
| 5 | stale、identity 不匹配或缺少人工批准 |

## 自动测试与真实外部验收

自动测试使用 fake provider、固定 WAV fixture 与注入的内部 ASR model factory/runner，不默认调用外网或加载真实 FunASR 模型。fixture 可验证 planner、异常分类、恢复、canonical WAV、ASR 失败后复用、timeline、字幕来源、AAC mux、stale 和 identity，但不能证明微软 Edge 服务当前可用、本地模型已经准备完成或声音已获用户接受。

真实 Edge 验收只能在 fixture 通过后单独进行：

1. 用 `zh-CN-YunjianNeural` 与默认 rate 生成短中文样音。
2. 用户完整试听并批准 sample identity。
3. 用短 SRT 生成完整旁白、timeline 和 narration SRT。
4. 用户完整试听 narration WAV、查看真实时长偏差并批准 current full identity；不生成无画面的预审视频。
5. 按 audio-authoritative 时钟渲染，烧录 narration SRT，封装 AAC。
6. 验证 H.264/AAC、1920×1080、60fps、yuv420p、24kHz mono、帧数/时长、字幕像素和完整解码。
7. 用户完整看片听音并批准 final identity。

如果外网、DNS、Edge 服务或音色不可用，必须报告：

```text
自动 fixture：PASS（若确实通过）
真实 Edge 外部验收：BLOCKED（具体网络/服务原因）
```

不得报告为 PASS，也不得用 SKIP 或 fixture PASS 冒充真实 Edge 已验收。真实图片 provider 与视觉/声音审阅也必须按项目模式单列为 SKIP/BLOCK/待用户确认/待 coordinator 代理审阅。本文只描述验收合同，不声明当前环境已经完成真实 Edge、真实图片 provider 或真实媒体审阅。

完整旁白批准只提前确认 current 配音与真实时长，不替代线稿、一次性 annotation review bundle 批准、一次性 scene review bundle 批准、最终字幕烧录/contact sheet 或最终成片完整看片听音批准。annotation 技术 current 后可先生成本地区域预览，再把标注内容、预览、`protectedRegions` 与 reveal 时序合并审阅；scene 按 `sceneRender` 有界并行生成 candidate、逐幕技术检查并由 coordinator 按 plan 顺序发布，但仍只对有序 current bundle 做一次 Gate 批准。人工/AI 审阅主体的分支均不合并这些 Gate；clean master 只是连续成片链路中的技术中间工件，不设独立确认。
