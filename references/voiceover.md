# 语音旁白合同

本文说明 `voiceoverMode=edge-tts | minimax | doubao` 的 provider、配置、完整旁白、canonical WAV、真实时间轴、恢复/stale 和外部验收边界。Disabled 模式不安装、不调用语音 provider；其正式交付仍需按 [字幕合同](subtitles.md) 烧录 source SRT。topic/text 的 provider 不由用户选择，而是唯一读取 `config/voice-providers.local.json` 的 `activeProvider`；其内容确认和 source package 合同见 [内容入口合同](content-input.md)。

## 目录

- [能力边界](#能力边界)
- [环境与配置](#环境与配置)
- [项目文件](#项目文件)
- [整轨合成与身份分层](#整轨合成与身份分层)
- [完整音频准备](#完整音频准备)
- [完整旁白与恢复](#完整旁白与恢复)
- [Canonical 完整音频与时间轴](#canonical-完整音频与时间轴)
- [完整旁白与真实时长批准](#完整旁白与真实时长批准)
- [时间坐标与渲染绑定](#时间坐标与渲染绑定)
- [最终封装与批准](#最终封装与批准)
- [stale 矩阵](#stale-矩阵)
- [退出码](#退出码)
- [自动测试与真实外部验收](#自动测试与真实外部验收)

## 能力边界

当前支持三个语音 provider：

- provider/protocol：`edge-tts`。
- 外部包版本：`edge-tts 7.2.8`，写入 plan 的 `provider.options.packageVersion`。
- 默认 voice：`zh-CN-YunjianNeural`。
- 默认 language：`zh-CN`。
- 默认 rate/pitch/volume：`+0% / +0Hz / +0%`。
- provider 临时格式：`audio-24khz-48kbitrate-mono-mp3`。
- 正式 canonical 格式：WAV `pcm_s16le / mono / 24000Hz`。

MiniMax T2A V2：

- provider/protocol：`minimax` / `MiniMax`；外部 model 为 `speech-2.8-hd`，endpoint 为 `/v1/t2a_v2`。
- endpoint：`https://api.minimaxi.com/v1/t2a_v2`，使用 `Authorization: Bearer <apiKey>`。
- 配置中的 `+10%` rate、`+10%` volume、`+0Hz` pitch 分别映射为 MiniMax `speed=1.1`、`vol=1.1`、`pitch=0`；speed 限制为 `0.5–2`，pitch 限制为 `-12–12`。
- 整轨请求使用 `speech-2.8-hd`、`output_format=hex`、`subtitle_enable=true`、`subtitle_type=word`、32 kHz mono MP3；返回的 hex 必须先解码，再经过 FFmpeg/ffprobe 规范化为 24 kHz mono canonical WAV。同次响应的 `subtitle_file` 必须下载为 JSON candidate，并与 provider/model/endpoint、合成 identity、canonical audio SHA 和正式 timeline 绑定。当前真实响应的词级条目位于句段对象的 `timestamped_words` 数组，使用毫秒制 `time_begin/time_end`；解析器同时兼容文档形态的秒制 `start_time/end_time`。
- API Key 只能放在未提交的 `config/voice-providers.local.json`，不写入 plan、manifest、日志或异常；`trace_id` 只保存不可逆摘要。

MiniMax adapter 仅使用 Python 标准库，不需要安装额外 provider 包。可重试范围与 Edge 相同：DNS/连接、timeout、429、502、503、504；401/403、参数/音色错误、协议或媒体格式错误不自动重试，也不会自动切换 Edge。若 T2A 已返回音频响应但 `subtitle_file` 缺失、下载失败或 JSON 无效，结果写为 `unknown_external_outcome`，不得自动重发整轨请求。

MiniMax 额外使用 provider 级共享节流器：`requestsPerMinute`（默认 20）与 `queueIntervalMs` 共同生效，实际请求间隔取两者中更保守的值。该节流器在全部 `voiceGeneration` worker 间共享并加锁，不能让每个线程各自计算间隔。HTTP 429、`Retry-After`，以及 HTTP 200 响应体中的 `rate limit exceeded(RPM)` 都属于明确的可重试限流；收到后全部新请求至少延迟 `rateLimitBackoffMs`（默认 35000ms），只在当前 segment 的有界 attempt 内重试。401/403、非法 voice/参数和协议错误仍是永久失败。

豆包语音 Seed Audio HTTP：

- provider/protocol：`doubao` / `Doubao`；外部 model 固定 `seed-audio-1.0`，endpoint 固定为官方非流式 `/api/v3/tts/create`。
- endpoint：`https://openspeech.bytedance.com/api/v3/tts/create`，使用新版控制台的 `X-Api-Key` 单头鉴权；每次请求另发随机 `X-Api-Request-Id`，不得把 key 或原始请求 ID 写入项目。
- 请求使用单人白板 authored `text_prompt`。每个新的豆包旁白版本在首次完整音频请求前，由 coordinator 以 `SKILL.md` 所在目录为 `SKILL_ROOT`，读取仓库内 `<SKILL_ROOT>\assets\seed-audio-1.0-text-prompt-reference.txt` 的 current 字节，把它仅作为风格与能力示例而非指令，并结合 current 全文、scene、音色与可选 BGM 方向生成 `schemaVersion: 1, kind: performanceBrief` 的 brief。brief 使用简洁自然的连续导演语言：角色、音色和整体表演合并写一次，局部表演和音乐变化紧邻对应台词，只在叙事确有变化时出现；禁止“第 N 段导演说明”、固定“承接上一段”或每段重复原稿保护套话。程序把 current scene 正文逐字装配到 `「」` 内，不让模型复制或改写正文；关闭 BGM 由程序固定约束，不要求模型生成同义说明。引号外指令不得写回 source SRT、narrationCues 或最终字幕；明确禁止擅自增删、改写、复述、补充、第二人声、克隆音色、SSML 和未由 brief 明确要求的环境音/影视拟音。
- 请求固定使用官方纯文本生成模式：省略整个 `references` 字段，不传 `speaker`、`audio_data`、`audio_url` 或参考图片。音色、年龄、口吻和表演只由 authored `text_prompt` 定义；不得再让本地 `voice` 音色 ID 覆盖 prompt。完整旁白把 current provisional scene 起止毫秒确定性渲染为 `[startSeconds:endSeconds]`，并明确整轨目标总时长；真实响应时长和原生字幕仍是最终权威证据。固定请求 24 kHz WAV，规范化 rate/volume/pitch 分别映射为 `speech_rate`、`loudness_rate`、`pitch_rate`，合法范围为 `-50–100`、`-50–100`、`-12–12`。
- `audio_config.enable_subtitle` 固定为 `true`。同一次响应必须原子取得 Base64 `audio` 与 `subtitle`，并严格解析 `subtitle.text`、`subtitle.sentences[]`、每句 `start_time/end_time/text` 及 `sentences[].words[]` 的毫秒 `start_time/end_time/text`；禁止递归猜测任意数组。官方字段合同只承诺非负毫秒整数，并未承诺标点 token 必有正时长、相邻 token 绝不轻微重叠或 word 严格包含于 sentence 边界：sidecar 保留原始 token 以验证文本完整性；生成语义时间轴时忽略纯标点零时长 token，只允许不超过 100ms 且仍能保留正时长的相邻语义 token 交叠裁切，其他倒序、越界、语义 token 零时长或大幅重叠继续 fail-closed。
- `duration` 是后处理音频时长，`original_duration` 是计费原始时长；两者都必须为正且不超过 120 秒。只读取 Base64 `audio`，不消费或持久化有效期两小时的 `url`；`X-Tt-Logid` 仅保存不可逆摘要。原始 WAV 仍须经过共享 FFmpeg/ffprobe 规范化与 canonical 24 kHz mono 校验。
- 正式 sidecar 独立发布为 `audio/doubao-subtitles.json`，使用数字 `schemaVersion`、非版本化 `kind=providerNativeWordSubtitles`，并通过 receipt/timeline 绑定 sidecar SHA/bytes、provider/model、voice synthesis identity、完整 `text_prompt` SHA、canonical `narration.wav` SHA 和正式 timeline。provider 字幕文字只用于时间匹配与覆盖证据，正式字幕文字仍逐字来自 current 已确认 source 原稿。
- 完整 `text_prompt` 在任何外部请求前 fail-closed 检查 3000 字符上限；完整旁白 provisional 时长在请求前检查 120 秒上限。不得截断 prompt、拆成逐句请求、退回裸文本或自动换 provider。
- 文档未给出完整业务错误码语义，因此只把 DNS/连接/timeout、HTTP 429/502/503/504 和明确限流消息作为有界可重试错误；鉴权、参数/音色、业务错误、协议或媒体错误均永久失败，不自动切换 provider。

豆包 adapter 与 MiniMax 一样只使用 Python 标准库，不增加 provider 包。它复用 `queueIntervalMs` 与 `maxRetries`，共享 adapter 实例在全部 `voiceGeneration` worker 间串行计算请求启动间隔。若响应已经包含可能计费的 audio，但 subtitle 缺失、无效或不能形成完整同请求证据，attempt 必须进入 `unknown_external_outcome`，禁止普通重发或用 FunASR 补齐。manifest 只记录稳定的去敏原因码（如 `invalid_duration`、`missing_subtitle`、`invalid_word_timing`、`sentence_words_text_mismatch`），不记录响应体、Base64 音频、正文、完整 prompt、临时 URL、原始请求 ID 或 provider 自由文本。

Edge TTS 不需要 API Key 或 Base URL，但不是离线模型。它依赖外网和微软语音服务，可能受服务规则、可用性、限流、音色和返回格式变化影响。不得把“无 Key”写成“无需网络”，也不得在失败后自动改 voice/rate、切换 provider 或降级到其他 TTS。

Skill 不调用、不导入、不读取 Yingshu 的 API、数据库、模型配置或 `node_modules`，也不调用其他 Codex skill、PowerShell 包装器或外部 ASR 凭据。Edge TTS 旁白对齐使用当前 skill 自带的 Python FunASR runner 和工作区本地模型缓存；MiniMax 与豆包分别直接使用各自同次合成响应返回的 provider-native word 字幕，不启动 FunASR 二次识别。当前不支持多角色、SSML、克隆音色、浏览器试听 UI 或自动 voice 推荐。用户仍只在阶段 0 选择加入或不加入 BGM，不增加独立试听或批准 Gate：Edge/MiniMax 启用时沿用内置 CC0《First Light Particles》的固定混音；豆包启用时由同一导演式 prompt 生成克制、低于人声、无歌词、自然淡入淡出的器乐层，canonical `narration.wav` 已包含人声+BGM，最终 mux 禁止再次混入内置曲目；关闭时豆包 prompt 明确禁止背景音乐、环境音和拟音。

## 环境与配置

基础环境不会因为没有 `edge-tts` 而失败。只有 Edge 项目显式安装和检查 feature：

```powershell
python scripts/prepare_env.py --check
python scripts/prepare_env.py

<ENV_PY> scripts/prepare_env.py --feature edge-tts
<ENV_PY> scripts/prepare_env.py --check --feature edge-tts

# 仅 Edge TTS 首次整轨旁白对齐前准备一次本地 FunASR 模型；MiniMax/豆包跳过：
<ENV_PY> scripts/prepare_env.py --feature narration-asr
<ENV_PY> scripts/prepare_env.py --check --feature narration-asr
```

`narration-asr` 仅供 Edge TTS 使用：它将 Paraformer 中文 ASR、FSMN-VAD 与 CT-Punc 准备到工作区 `runtime/cache/funasr-models/`，并写入模型 receipt。正式 runner 只读取 receipt 中的本地绝对模型路径，不在旁白生成阶段隐式下载模型，也不读取 Yingshu 或其他 skill 的环境。MiniMax 与豆包整轨不准备、不加载、不运行该 feature。

`config/voice-providers.example.json` 是无秘密的 provider 配置示例，记录外部 package/model/endpoint、language、规范化 rate/pitch/volume、输出格式及请求策略字段；Edge/MiniMax 另记录 voice，豆包只记录 `voiceControlMode=text_prompt`。用户配置不再携带内部 `contractVersion`；旧 local 文件中残留的该字段会在 loader 边界忽略，不迁移也不写入 artifact。它不是凭据文件，也不允许加入 key、Cookie、Token 或临时 URL。

排查 active provider 或凭据是否已配置时，只能调用脱敏状态接口，不得用 shell、文件读取工具或临时代码直接读取、打印或转述任何 `config/*.local.json` 原文：

```powershell
<ENV_PY> scripts/voice_provider_config.py status
```

该命令只输出 `provider`、`model`、`voice`、`voiceControlMode`、`rate` 与 `credentialsConfigured`；豆包的 `voice` 只显示内部 `text-prompt-authored` 标记，不是 speaker ID。provider 自由文本响应只可在进程内用于分类；异常、CLI、request audit、receipt 与 manifest 均不得保存 API Key、Authorization、Token、原始请求 ID、原始响应或可能回显这些内容的 provider 消息。

MiniMax 请求策略字段为 `queueIntervalMs`、`requestsPerMinute`、`rateLimitBackoffMs` 与 `maxRetries`。`requestsPerMinute` 必须位于 1–600，`rateLimitBackoffMs` 必须位于 1000–300000；缺失时分别采用 20 RPM 与 35000ms。豆包使用 `queueIntervalMs` 与 `maxRetries`，并要求本地配置包含 `apiKey`、`model=seed-audio-1.0`；本地旧 `voice` 和 `contractVersion` 即使仍存在也会在 loader 边界丢弃，不能进入 plan、identity 或请求。降低 `voiceGeneration` 只能减少在途请求和本地资源占用，不能替代 provider 自身限流。

完整音频 CLI 的 provider 永远读取 `config/voice-providers.local.json` 的 `activeProvider`，不提供 `--provider` 覆盖入口；activeProvider 必须与项目已经冻结的 `voiceoverMode` 一致。首次 `full` 支持整数百分点 `--rate`；`--voice` 仅允许 Edge/MiniMax，豆包 prompt-only 模式传入该参数会在请求前拒绝。`--rate 0` 规范化为 `+0%`，`10` 为 `+10%`，`-10` 为 `-10%`。持久化 identity 使用规范化字符串，不依赖调用点猜单位。已有 current `planning/voice-plan.json` 时，`full` 不允许覆盖 voice/rate/brief。

## 项目文件

```text
planning/voice-plan.json
manifests/voice-manifest.json
audio/segments/unit-0001.wav
audio/narration.wav
audio/doubao-subtitles.json  # 仅豆包
audio/minimax-subtitles.json # 仅 MiniMax
audio/timeline.json
audio/narration.srt
planning/timing-plan.json
```

所有路径在 JSON 中保存为项目相对 POSIX 路径。manifest 不保存完整 provider 响应、秘密或本机绝对路径；可选 provider request ID 只能保存不可逆脱敏摘要。

## 整轨合成与身份分层

旁白复用共享的 `scripts/srt_timeline.py`，不维护第二套 SRT 解析口径。完整旁白 planner 使用非版本化 `segmentation.mode=full_track`：

- 全部已确认旁白按 cue 原顺序组成一个 provider 请求，固定只有一个 synthesis task。
- 同一 scene 内连续朗读，scene 之间保留两个换行作为段落边界；provider 仍能看到全文上下文。
- 不再按 12/24/36 code point 拆分，也不因 provider 或 ASR 失败回退成逐句请求。
- 豆包、MiniMax 与 Edge 共用同一整轨 planner；各 provider 若拒绝当前全文长度或请求，按永久 provider 错误 fail-closed，不自动切换 provider 或重新拆句。豆包另在请求前同时检查完整导演式 prompt 不超过 3000 字符、provisional 整轨不超过 120 秒。
- cue/scene mapping 仍进入整轨 unit 的审计与 identity；真正的 cue/scene 毫秒边界在成品 WAV 生成后由 ASR 对齐派生。

身份分为三层，避免源 SRT 只改时间时无意义地重请求音频：

```text
sourceTextIdentityHash
sourceTimingIdentityHash
voiceSynthesisIdentityHash
```

`voiceSynthesisIdentityHash` 覆盖完整规范化朗读文本、稳定 ordinal/range、scene 段落、voice、rate、language、`segmentation.mode`、provider id/protocol/options。options 中外部真实 package/model/endpoint 与 prompt capability 参数进入 identity，不再复制一个内部 provider 合同名。豆包的 `voice` 固定为内部审计标记 `text-prompt-authored`，不得是 speaker ID；豆包还显式覆盖 `voiceControlMode=text_prompt`、`timeControlMode=scene_windows` 和确定性渲染后的完整 `text_prompt` SHA，因此 scene 时间、情绪/停顿方向、BGM 开关或纯文本音色方向变化都不能复用旧音频。source 原文件 SHA 仍用于审计；它不是判断整轨音频是否可复用的唯一依据。

voice plan 另有 `voicePlanAuditHash`，覆盖完整 plan。豆包的 `provider.options.promptSpec` 使用 `schemaVersion: 1, kind: textPromptPlan`，在这里冻结 coordinator authored 的单段角色/整体表演方向、可选 BGM 的开头与结尾、与 current scene 一一对应的局部导演方向、参考文件 SHA 及硬限制，但不复制原稿。关闭 BGM 由渲染器确定性执行。首次 `full` 通过 `--doubao-performance-brief` 导入该 brief；状态恢复和 provider 重试只消费已冻结 plan，不重新调用模型或重读参考文件。audit hash 变化会使完整批准、时长决定、timeline、narration SRT 和下游重新判定，但只有 synthesis identity 或正式 WAV recipe/字节变化的 segment 才必须重新请求。

## 完整音频准备

阶段 0 只确认内容与制作方案，不生成、试听或批准任何短样音。初始联合批准完成后，首次 `full` 在发出任何外部请求前确定性冻结 voice plan 与空的 voice manifest。Edge/MiniMax 可从 active provider 配置读取 voice/rate，也可在首次调用显式传入；豆包必须由 coordinator 先生成项目 `.work/` 下的 current performance brief：

```powershell
<ENV_PY> scripts/generate_voiceover.py full `
  --project <项目根目录> `
  --doubao-performance-brief <项目\.work\doubao-performance-brief.json>
```

豆包 performance brief 顶层只允许以下字段；`narratorDirection` 在同一段内合并角色、音色、整体口吻和表演弧线，避免另写同义的全局说明。`passages` 必须按 current scene 顺序一一对应，`enabledMusicBefore` 可为空字符串，其余导演字段必须是非空单段自然语言且不得包含正文边界符号 `「」`：

```json
{
  "schemaVersion": 1,
  "kind": "performanceBrief",
  "referenceSha256": "<current 参考文件 SHA-256>",
  "narratorDirection": "旁白 是……，整体像……",
  "music": {
    "enabledOpeningDirection": "开头……音乐始终低于人声……",
    "enabledEndingDirection": "人声结束后……自然淡出"
  },
  "passages": [
    {
      "sceneId": "scene-001",
      "voiceDirection": "旁白以……口吻自然切入……",
      "enabledMusicBefore": ""
    }
  ]
}
```

coordinator 生成 brief 时必须完整读取仓库内 current 参考文件，但按当前任务选择适用能力，不照搬无关角色、语言、拟音或剧情。官方润色范式优先：

- `narratorDirection` 用一个自然段同时说明年龄/音色/普通话、交流姿态和贯穿全文的表层与内在情绪，不再补第二段同义的“整体表演方向”。
- `enabledOpeningDirection` 只交代开头动机、核心配器、情绪颜色、相对人声音量与人声进入后的退让；没有可听价值的乐器清单和抽象解释应删除。
- `voiceDirection` 紧贴本段真正会听见的语速、停顿、重音、音调和情绪转折，并可点名少量关键原词；不复述正文含义，不把每一句都解释一遍。
- `enabledMusicBefore` 默认为空；只有配器、张力、音量或留白确实发生变化时才写一句，不为凑 scene 数重复近义过渡。
- `enabledEndingDirection` 只写人声结束后的一个简洁收束动作。禁止“第 N 段导演说明”、固定“承接上一段”、机械位置标签和每段重复的原稿保护套话。

正文不进入 brief，由程序从 current source 逐字插入。若附件缺失/不可读、brief 与 current scene 不一致或渲染后超过 3000 字符，`full` 在外部请求前失败；超限时只精简导演说明，禁止截断正文、退回旧硬编码模板或更换 provider。

```powershell
<ENV_PY> scripts/approve_initial_project.py `
  --project <项目根目录> `
  --choice <结构化联合选择>
```

联合动作必须重验 current content identity、pending 状态、完整句所对应组合与当前能力，然后原子冻结 BGM、后续模式、生图方式和 `initialApproval`。任一失败不留下半批准状态。未完成初始批准时，`full` 必须返回 5。

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

# 仅 Edge TTS 自动 FunASR 未完成时，从同一 current narration.wav 恢复；MiniMax/豆包不使用此入口：
<ENV_PY> scripts/generate_voiceover.py publish-alignment `
  --project <项目根目录> --asr-srt <FunASR一-token-一-cue声学字幕.srt>
```

`execution.concurrency.voiceGeneration` 仍保留为工作区兼容配置，但 full-track 固定只有一个在途 task，因此 effective concurrency 恒为 0 或 1。provider worker 只产生 attempt candidate；voice manifest、正式 WAV、timeline、SRT、identity 与批准仍由 coordinator 单写：

```text
provider 临时媒体
  →（MiniMax/豆包）原子取得并严格验证同请求 provider-native word 字幕 candidate
  → FFmpeg 规范化为 pcm_s16le/mono/24000Hz WAV
  → ffprobe + RIFF/WAVE/流合同检查
  → SHA-256/bytes/duration
  → 原子写入 .work/<run>/external/uXXXX-aXXXX.wav candidate
  → coordinator 写 candidate_ready / publishing checkpoint
  → coordinator 复制、fsync、原子发布唯一的 audio/segments/unit-0001.wav
  → 发布 canonical audio/narration.wav，状态进入 waiting_alignment
  → Edge TTS 由本地 FunASR 生成 token 级声学 SRT
  → MiniMax 原子发布同次响应的 audio/minimax-subtitles.json，并直接消费 provider-native word 时间戳
  → 豆包原子发布同次响应的 audio/doubao-subtitles.json，并直接消费 subtitle.sentences[].words[] 毫秒时间戳
  → 以已确认 source 原稿校正文字、按原稿标点语义安全切句并发布 timeline/narration SRT/FULL_IDENTITY
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

- 唯一整轨 task validated 后立即落盘并登记 hash；Edge TTS 的 ASR/对齐失败或 MiniMax/豆包的原生字幕对齐失败都不能删除或普通重请求该音频。
- Edge TTS 内部 ASR 的逐次证据写入当前项目唯一 `.work/voice-align-<run>/`；MiniMax 与豆包原始 word 字幕分别只写入当前 attempt candidate 和正式 `audio/minimax-subtitles.json` / `audio/doubao-subtitles.json`。正式项目沿用同一 FULL identity 链，不新增独立批准 Gate。
- `--retry-failed` 只处理 failed、cancelled 或未完成的整轨 task。
- synthesis identity、正式 WAV SHA 和媒体合同仍 current 时不重请求、不覆盖。
- `requesting` 且 candidate 存在时直接验证并提升为 `candidate_ready`；`candidate_ready/publishing` 恢复时 provider 调用数必须为 0。
- `requesting` 且 candidate 不存在、provider 又不能按同一幂等键查询时写 `unknown_external_outcome`，即使传 `--retry-failed` 也不得自动重复请求。
- manifest 与正式文件 hash 不一致时失败，不能假装恢复。
- voice plan audit hash 变化不等于所有 segment synthesis identity 都变化；但豆包的 scene 时间窗口进入完整 prompt 与 synthesis identity，窗口变化时不得复用旧音频。
- 纯 source timing 变化、朗读文本和 scene/分段边界不变时，Edge/MiniMax 的 validated segment/WAV 可按原规则复用；豆包 prompt-only v3 必须因时间窗口 prompt 变化重建整轨。所有 provider 的时长偏差、批准、timeline 和下游仍要重算。
- canonical WAV 发布失败保留 validated segment；只清理或恢复本次 run，不扫描其他 `.work` 目录。

可重试的外部错误仅包括 DNS/连接、明确 timeout、429、502、503、504；重试次数有限。voice 不存在、配置/协议错误、身份变化、媒体无效、路径越界、FFmpeg 缺失和用户取消不自动重试。

## Canonical 完整音频与时间轴

唯一整轨 task 成功后发布 `audio/narration.wav`。Edge TTS 随后调用当前 skill 内部 `transcribe_narration` Python API：runner 使用本地 Paraformer + FSMN-VAD + CT-Punc，固定 `max_single_segment_time=15000`，捕获并重建每个 VAD 子结果的 token/timestamp，再与顶层数组逐项核对。MiniMax 不调用该 runner；整轨 T2A 请求固定开启 `subtitle_enable=true` 与 `subtitle_type=word`，把同次响应的 `subtitle_file` 下载、校验并原子发布为 `audio/minimax-subtitles.json`。豆包也不调用该 runner；整轨请求固定 `enable_subtitle=true`，严格解析同次响应的 `subtitle.text/sentences[].words[]` 并原子发布 `audio/doubao-subtitles.json`。三条路径最终都只把时间边界作为证据，正式字幕文字必须逐字来自已确认 source 原稿。对齐成功后再原子发布：

```text
audio/narration.wav
audio/minimax-subtitles.json  # 仅 MiniMax
audio/doubao-subtitles.json   # 仅豆包
audio/timeline.json
audio/narration.srt
```

成功输出：

```text
FULL_AUDIO=<项目根目录>/audio/narration.wav
FULL_IDENTITY=<64位 sha256>
```

完整音频、timeline 与 narration SRT 均 current 后，`full` 才输出 `FULL_IDENTITY`，不再编码 1920×1080 的无画面预审视频。若对应 provider 的时间证据或参考原稿对齐失败，canonical `narration.wav` 继续保留，manifest 保持 `waiting_alignment`，已经成功且证据完整的 TTS 不会重跑；此时只有 `FULL_AUDIO_IDENTITY` 可供技术恢复，不能用于 `approve-full`。只有 Edge 可在环境修复后再次执行 `full --retry-failed` 复用 current WAV，或用 `publish-alignment` 显式导入 token SRT。MiniMax/豆包的音频响应若没有取得完整同请求原生字幕则属于 `unknown_external_outcome`，不得用本地 FunASR 替代，也不得自动重发。

`audio/timeline.json` 必须使用数字 `schemaVersion: 3`，并满足：

- narration unit 使用字幕真实显示区间：全部正时长、递增且不重叠，但允许保留真实前导、句间与尾部无字幕空档。
- scene 仍从全局 0 连续覆盖整轨并以 `ffprobe` 实测 duration 收口；字幕空档不改变 scene 全局时钟。
- scene 保留已批准语义 cue 边界，连续覆盖全部 unit，最后一幕以整轨时长收口。
- 每幕记录全局 `startMs/endMs` 及累计计算的 `startFrame/endFrameExclusive/frameCount`。
- 包含 source SRT、voice plan audit、audio 与 narration SRT 的 current binding，以及 `tokenTimingUsed=true`、`qualityGatePassed=true`、结构化 `captionSegmentationRecipe` 和真实 gap 摘要。Edge TTS 另绑定 15 秒 VAD 分段重建 recipe、顶层逐项一致性与局部语速 QA；MiniMax 与豆包分别绑定各自 provider-native word 字幕 SHA/bytes、provider/model、合成 identity 与 audio SHA，豆包另绑定完整导演式 prompt SHA、响应 `duration/original_duration` 证据。

`audio/narration.srt` 的文字逐字来自已确认 source 原稿，时间来自对应 provider 的 current 词级边界；cue 不跨 scene，且只允许在原稿标点或原始 cue 边界切分。不得把金额、数字词组、ASCII 单词、固定词语或连续汉字从中间截断。全部路径检查全局文本匹配、真实语义边界、caption 阅读上限、断词和时间重叠；只有 Edge TTS 的二次 ASR 证据额外执行 16/32/48-token 局部语速下限、上限和最快/最慢波动比，MiniMax/豆包同请求原生时间戳不使用这套经验阈值。scene N 的边界不得早于本幕最后一个真实词结束；若与下一 cue 之间存在真实停顿，只在该停顿内取可审计中点并记录 `lastNarratedTokenEndMs/nextNarratedTokenStartMs/availablePauseMs/boundaryBasis`，不使用统一固定延迟。它不复制 source SRT 的 provisional 时间戳，也不能由字幕阶段手工改写。所有音频旁白模式唯一使用该文件。

真实静音可以产生 SRT gap：首字开口前不提前显示字幕，句间停顿超过真实词间距时可以短暂无字幕，末字结束后不得为“收口到整轨”而把末句强行延长至音频末尾。scene/timing plan 仍覆盖完整音频，因此画面和 AAC 时长不受影响。Edge 的旧时间证据缺少 current 分段重建 recipe 或局部语速 QA 时必须重新执行本地 ASR/对齐；MiniMax 或豆包缺少 current 同请求原生字幕/prompt/audio binding 时按普通 current schema/identity 不匹配处理，不能从旧 WAV 反推或用 FunASR 补齐。

只读技术验证：

```powershell
<ENV_PY> scripts/generate_voiceover.py status --project <项目根目录>
<ENV_PY> scripts/validate_voiceover.py --project <项目根目录>

# 验证器合同升级或需要重新取得完整解码证据时：
<ENV_PY> scripts/validate_voiceover.py --project <项目根目录> --force-deep
```

`status` 输出 voice plan、各 segment 状态计数和 full approval 摘要；它不修改项目。`validate_voiceover.py` 验证 current WAV、timeline 与 narration SRT，成功输出 `VOICEOVER_VALIDATED=1` 和 current identity，也不会写人工批准。

## 完整旁白与真实时长批准

完整旁白生成后分两种批准 basis。人工模式仍由用户完整试听 `audio/narration.wav` 并完成两项明确判断；自主模式不要求 coordinator 冒充完整听音，而是重验阶段 0 current 内容与制作方案授权及以下严格技术证据后推进。

1. 配音内容、音色、语速与完整听感可接受，没有漏读、重复、断裂或奇怪停顿；同时抽查字幕与旁白同步、语义切句和停顿留白，不得出现词中断开。
2. 查看 target/source provisional duration 与真实 audio duration 的差值和比例，决定是否接受真实时长。

`agentApprovalEnabled` 缺失或为 `false` 时，同一条完整旁白确认请求还必须展示本次运行的生成后审阅策略：`user_first` 直接把各阶段通过技术校验的 current artifact 交给用户，`agent_first` 在交付用户前为各阶段准备一次辅助 AI 语义预审。用户应能在一次回复中同时确认完整旁白、作出所需的真实时长决定并选择策略，例如“确认完整旁白，选择 user_first”；不得先完成 `approve-full`，再设置一次只用于选择策略的独立聊天关卡。用户未指定策略时必须继续停在本 Gate 询问，禁止静默默认。

`agentApprovalEnabled=true` 时，审阅策略确定性为 `agent_first`，不再询问 `user_first|agent_first`。coordinator 必须确认：整轨单次 provider 请求、canonical WAV recipe 与完整解码、对应 provider 的 current 词级时间证据（Edge 为 FunASR，MiniMax 与豆包为各自同次响应原生 word 字幕）、原稿对齐、cue/scene/timeline/narration SRT binding、current `FULL_IDENTITY`、时长与偏差证据全部通过；豆包还必须确认完整 prompt SHA 和实际 BGM 模式。然后使用同一原子动作并记录 `approvalBasis=technical_after_initial_approval`。不得记录或表述为 AI 已完整试听。

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

`--identity-hash` 必须是 current full identity；不匹配时返回 5，且不得修改旧批准。`--review-policy` 必填并持久化到 `fullApproval.reviewPolicy`。人工模式超 10% 仍要求用户 `accept_actual`；自主模式按阶段 0 授权记录 `accept_actual` 并采用真实音频时钟，不再询问用户。alignment 失败、stale、媒体失败或 `unknown_external_outcome` 仍 fail-closed。

coordinator 在人工模式收到完整听审与时长决定后，或在自主模式完成阶段 0 授权/current 技术证据复核后，必须先核对 `FULL_IDENTITY` 并成功执行 `approve-full`，再开始视觉阶段。identity stale、alignment/媒体失败或超阈值策略不满足时不得只凭 `agent_first` 启动生图。自主返工只重做受影响技术阶段；需实质改写阶段 0 文本或已冻结策略时仍回到用户确认。review policy 不进入作品 identity，也不能替代视觉 Gate。

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

若 `project.json.backgroundMusic.enabled=true`：Edge/MiniMax 的同一个 mux 命令继续读取 skill 内置 CC0 曲目，按结构化 `background_music_mix` recipe 固定 `-15 dB` 混入旁白，应用 1.2 秒淡入和 1.8 秒淡出，并在曲目短于成片时循环；豆包 prompt-only 的 canonical `narration.wav` 已由导演式 prompt 生成“人声+克制器乐”，mux 必须识别 `renderMode=provider_embedded`，禁止读取或混入《First Light Particles》，只做目标 AAC 封装。最终仍只有一路 AAC；delivery manifest 的 `final.backgroundMusic` 对固定混音记录资产、参数和 recipe，对豆包记录 `provider=doubao`、`model=seed-audio-1.0`、完整 prompt SHA、voice synthesis/full audio identity 和 canonical audio SHA，并拒绝重复固定 BGM receipt。若字段为 `false` 或旧项目缺少该字段，保持纯旁白封装。BGM 不新增人工 Gate，最终完整看片听音批准同时验收其听感。

完整旁白批准和技术验证都不自动等于 final approval。人工模式仍由用户完整看片听音。自主模式不能因宿主缺少听音能力再次阻塞，也不能声称 AI 完整听过；它必须重验 current full audio、权威字幕、AAC、流结构、完整解码、时长/帧数/尾部、实际 BGM 模式（固定混音或豆包 provider-embedded）与 `FINAL_IDENTITY`，然后以 `approvalBasis=technical_after_initial_approval`、`reviewBasis=current_final_technical_validation` 写 final approval。视觉阶段若宿主具备查看能力仍实际检查 current 视觉 artifact。

```powershell
<ENV_PY> scripts/approve_final_media.py `
  --project <项目根目录> `
  --identity-hash <刚完整看片听音的 FINAL_IDENTITY>
```

final identity 覆盖 clean video、audio、timeline、权威字幕、样式 recipe、字体、render profile、timing plan、burn/mux recipe 和 final SHA。任一输入变化都会使最终批准 stale；`approvalBasis/reviewBasis` 是审计字段，不进入作品 identity。

## stale 矩阵

完整 stale 传播矩阵的唯一来源是
[`recovery-and-identity.md`](recovery-and-identity.md)。下表保留语音阶段的具体产物视图，
不得独立改变 retry、current binding 或批准语义。

| 变化 | 必须 stale | 可保留 |
|---|---|---|
| topic/body/rewritePolicy/target/narration cue/scene mapping 改变 | content/source package、voice plan、full 批准、音频、timeline、narration SRT 和全部相关下游 | 仅按 current identity 重新判定可复用 segment |
| 仅 imagePrompt 改变，cue/scene boundary 不变 | generation plan、图片和视觉下游 | current 音频、timeline 与 narration SRT |
| voice/rate/朗读文本/分段边界/provider model、endpoint 或请求参数 | full 批准、受影响段、WAV、timeline、narration SRT、annotation、场景视频、captioned/final、最终批准 | synthesis identity 未变的其他段；图片需语义复核 |
| source 仅改时间，朗读文本与分段不变 | 时长决定、full 批准、timeline、narration SRT、annotation 和下游；豆包另 stale scene 时间窗口 prompt 与整轨音频 | Edge/MiniMax synthesis identity/current WAV 合法的 segments；豆包不复用旧音频 |
| narration WAV 改变 | full 批准、timeline、annotation 和全部下游 | 图片 |
| ASR/对齐 recipe 或 narration SRT 改变，scene 边界不变 | full approval、字幕烧录、captioned/final、最终批准 | current canonical WAV、图片、annotation、scene bundle 与 clean master；按 binding 重验 |
| timeline 的 scene 边界/timing plan 改变 | narration SRT、annotation 时序、场景视频、captioned/final、最终批准 | 图片 generation plan/manifest；音频按 identity 调查 |
| render profile 或 mode 改变 | timing、annotation、所有视频与最终批准 | 图片和音频可保留但需重新绑定/复核 |
| captioned video、AAC 参数、BGM 开关、豆包完整 prompt/provider-embedded 模式、内置资产/固定混音参数或 final SHA 改变 | final、最终批准；豆包 prompt 变化还使 audio/timeline 下游 stale | 未绑定变化的上游 current 产物 |

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

自动测试只允许 fake provider、固定 WAV/JSON fixture 与注入的内部 ASR model factory/runner，不默认调用外网或加载真实 FunASR 模型。fixture 可验证 planner、异常分类、恢复、canonical WAV、原生字幕/ASR 失败语义、timeline、字幕来源、AAC mux、stale 和 identity，但不能证明微软 Edge、MiniMax 或豆包服务当前可用、本地模型已经准备完成或声音已获用户接受。

真实 Edge 验收只能在 fixture 通过后单独进行：

1. 用短 SRT、`zh-CN-YunjianNeural` 与默认 rate 生成完整旁白、timeline 和 narration SRT。
2. 用户完整试听 narration WAV、查看真实时长偏差并批准 current full identity；不生成无画面的预审视频。
3. 按 audio-authoritative 时钟渲染，烧录 narration SRT，封装 AAC。
4. 验证 H.264/AAC、1920×1080、60fps、yuv420p、24kHz mono、帧数/时长、字幕像素和完整解码。
5. 用户完整看片听音并批准 final identity。

如果外网、DNS、Edge 服务或音色不可用，必须报告：

```text
自动 fixture：PASS（若确实通过）
真实 Edge 外部验收：BLOCKED（具体网络/服务原因）
```

不得报告为 PASS，也不得用 SKIP 或 fixture PASS 冒充真实 Edge 已验收。真实图片 provider 与视觉/声音审阅也必须按项目模式单列为 SKIP/BLOCK/待用户确认/待 coordinator 代理审阅。本文只描述验收合同，不声明当前环境已经完成真实 Edge、真实图片 provider 或真实媒体审阅。

完整旁白批准不替代线稿、annotation review bundle 或 scene review bundle。人工模式 final 仍完整看片听音；自主模式 final 使用上述严格技术 evidence/basis，不重复设置听音 Gate。annotation 与 scene 视觉审阅边界保持；clean master 只是技术中间工件。
