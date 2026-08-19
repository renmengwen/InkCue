# Edge TTS 旁白合同

本文说明 `voiceoverMode=edge-tts` 首版的 provider、配置、样音、完整旁白、canonical WAV、真实时间轴、恢复/stale 和外部验收边界。Disabled 模式不安装、不调用也不要求 Edge TTS；其正式交付仍需按 [字幕合同](subtitles.md) 烧录 source SRT。topic/text 首版只允许 Edge；其内容确认和 source package 合同见 [内容入口合同](content-input.md)。

## 目录

- [能力边界](#能力边界)
- [环境与配置](#环境与配置)
- [项目文件](#项目文件)
- [Speech unit 与身份分层](#speech-unit-与身份分层)
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

首版只支持 Python `edge-tts` provider：

- provider/protocol：`edge-tts`。
- provider contract：`edge-tts-python-7.2.8-v1`。
- 默认 voice：`zh-CN-YunjianNeural`。
- 默认 language：`zh-CN`。
- 默认 rate/pitch/volume：`+0% / +0Hz / +0%`。
- provider 临时格式：`audio-24khz-48kbitrate-mono-mp3`。
- 正式 canonical 格式：WAV `pcm_s16le / mono / 24000Hz`。

Edge TTS 不需要 API Key 或 Base URL，但不是离线模型。它依赖外网和微软语音服务，可能受服务规则、可用性、限流、音色和返回格式变化影响。不得把“无 Key”写成“无需网络”，也不得在失败后自动改 voice/rate、切换 provider 或降级到其他 TTS。

Skill 不调用、不导入、不读取 Yingshu 的 API、数据库、模型配置或 `node_modules`，也不接触任何外部凭据。首版不支持第二 provider、多角色、情绪/SSML、克隆音色、背景音乐、浏览器试听 UI 或自动 voice 推荐。

## 环境与配置

基础环境不会因为没有 `edge-tts` 而失败。只有 Edge 项目显式安装和检查 feature：

```powershell
python scripts/prepare_env.py --check
python scripts/prepare_env.py

<ENV_PY> scripts/prepare_env.py --feature edge-tts
<ENV_PY> scripts/prepare_env.py --check --feature edge-tts
```

`config/voice-providers.example.json` 是无秘密的首版 provider 合同示例，记录 package/contract、voice、language、规范化 rate/pitch/volume、输出格式及请求策略字段。它不是凭据文件，也不允许加入 key、Cookie、Token 或临时 URL。

当前样音 CLI 只暴露 `--voice` 与整数百分点 `--rate`；其余首版字段由实现合同冻结。`--rate 0` 规范化为 `+0%`，`10` 为 `+10%`，`-10` 为 `-10%`。持久化 identity 使用规范化字符串，不依赖调用点猜单位。全局示例配置不能自动覆盖项目中已经生成或批准的 `planning/voice-plan.json`。

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

## Speech unit 与身份分层

旁白复用共享的 `scripts/srt_timeline.py`，不维护第二套 SRT 解析口径。speech unit planner：

- 保留稳定 `sourceOrdinal`；合法的原始编号另存 `originalIndex`。
- 句末 `。！？!?；;……` 优先断句，`，,、：:` 为次级断点。
- 合并过短相邻 cue、拆分超长句、把纯标点并入相邻 unit。
- 字符长度按 Unicode code point 计算。
- cue 范围连续、无遗漏、无重复；相同输入生成相同 unit/hash。
- unit 必须在已确认 scene 边界断开，不得跨 scene。

身份分为三层，避免源 SRT 只改时间时无意义地重请求音频：

```text
sourceTextIdentityHash
sourceTimingIdentityHash
voiceSynthesisIdentityHash
```

`voiceSynthesisIdentityHash` 覆盖规范化朗读文本、稳定 ordinal/range、voice、rate、language、分段合同和 provider 合同。source 原文件 SHA 仍用于审计；它不是判断每段是否必须重新合成的唯一依据。

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
SAMPLE_IDENTITY=<64位 sha256>
```

技术校验只说明 WAV 可读、媒体合同正确，不代表用户已接受 voice/rate。必须播放完整样音并等待明确确认；收到确认后才执行：

```powershell
<ENV_PY> scripts/generate_voiceover.py approve-sample `
  --project <项目根目录> `
  --identity-hash <刚完整试听的 SAMPLE_IDENTITY>
```

identity 必须仍是 current sample。错误或 stale identity 返回 5，不修改旧批准。未批准 current 样音时，`full` 必须返回 5。

## 完整旁白与恢复

```powershell
<ENV_PY> scripts/generate_voiceover.py full --project <项目根目录>

# 中断或部分失败后：
<ENV_PY> scripts/generate_voiceover.py full `
  --project <项目根目录> --retry-failed
```

`execution.concurrency.voiceGeneration` 控制最多在途 unit 数，缺失时继承 worker `default`，整个 pool 缺失时为 `1`。摘要记录 configured/effective/peak worker 与 task count；该 pool 与 `execution.agents` 独立、不共享也不相乘。provider worker 只产生 attempt candidate；voice manifest、正式 WAV、timeline、SRT、identity 与批准仍由 coordinator 单写并按 unit index 串行提交：

```text
Edge 临时媒体
  → FFmpeg 规范化为 pcm_s16le/mono/24000Hz WAV
  → ffprobe + RIFF/WAVE/流合同检查
  → SHA-256/bytes/duration
  → 原子写入 .work/<run>/external/uXXXX-aXXXX.wav candidate
  → coordinator 写 candidate_ready / publishing checkpoint
  → coordinator 复制、fsync、原子发布 audio/segments/unit-xxxx.wav
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

- 每个 validated 段立即落盘并登记 hash，其他段失败不能删除它。
- 首个 provider、取消、规范化失败或 unknown outcome 后停止派发新 unit；已经在途且形成合法 candidate 的 unit 仍安全提交。
- `--retry-failed` 只处理 failed、cancelled 或未完成 unit。
- synthesis identity、正式 WAV SHA 和媒体合同仍 current 的 validated 段不重请求、不覆盖。
- `requesting` 且 candidate 存在时直接验证并提升为 `candidate_ready`；`candidate_ready/publishing` 恢复时 provider 调用数必须为 0。
- `requesting` 且 candidate 不存在、provider 又不能按同一幂等键查询时写 `unknown_external_outcome`，即使传 `--retry-failed` 也不得自动重复请求。
- manifest 与正式文件 hash 不一致时失败，不能假装恢复。
- voice plan audit hash 变化不等于所有 segment synthesis identity 都变化。
- 纯 source timing 变化、朗读文本和 scene/分段边界不变时，validated segment/WAV 可复用；时长偏差、批准、timeline 和下游仍要重算。
- 合并失败保留所有 validated segments；只清理或恢复本次 run，不扫描其他 `.work` 目录。

可重试的外部错误仅包括 DNS/连接、明确 timeout、429、502、503、504；重试次数有限。voice 不存在、配置/协议错误、身份变化、媒体无效、路径越界、FFmpeg 缺失和用户取消不自动重试。

## Canonical 完整音频与时间轴

所有段成功后合并并发布：

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

完整音频、timeline 与 narration SRT 均 current 后，`full` 立即结束，不再编码 1920×1080 的无画面预审视频。`FULL_IDENTITY` 已绑定三者的 current identity，供完整试听与真实时长批准使用。

`audio/timeline.json` 必须满足：

- unit 0 从全局 0 开始，所有 unit 连续、无空洞、无重叠。
- 最后 unit 以整轨 `ffprobe` 实测 duration 收口。
- scene 保留已批准语义 cue 边界，连续覆盖全部 unit，最后一幕以整轨时长收口。
- 每幕记录全局 `startMs/endMs` 及累计计算的 `startFrame/endFrameExclusive/frameCount`。
- 包含 source SRT、voice plan audit、audio 与 narration SRT 的 current binding。

`audio/narration.srt` 的文本和时间从 canonical units/timeline 派生，从 0 连续覆盖真实音频；它不复制 source SRT 的旧时间戳，也不能由字幕阶段手工改写。Edge 正式字幕唯一使用该文件。

只读技术验证：

```powershell
<ENV_PY> scripts/generate_voiceover.py status --project <项目根目录>
<ENV_PY> scripts/validate_voiceover.py --project <项目根目录>

# 验证器合同升级或需要重新取得完整解码证据时：
<ENV_PY> scripts/validate_voiceover.py --project <项目根目录> --force-deep
```

`status` 输出 voice plan、sample、各 segment 状态计数和 full approval 摘要；它不修改项目。`validate_voiceover.py` 验证 current WAV、timeline 与 narration SRT，成功输出 `VOICEOVER_VALIDATED=1` 和 current identity，也不会写人工批准。

## 完整旁白与真实时长批准

完整旁白生成后，用户必须完整试听 `audio/narration.wav`，完成两项明确人工判断：

1. 配音内容、音色、语速与完整听感可接受，没有漏读、重复、断裂或奇怪停顿。
2. 查看 target/source provisional duration 与真实 audio duration 的差值和比例，决定是否接受真实时长。

同一条完整旁白确认请求还必须展示本次运行的生成后审阅策略：`user_first` 直接把各阶段通过技术校验的 current artifact 交给用户，`agent_first` 在交付用户前为各阶段准备一次辅助 AI 语义预审。用户应能在一次回复中同时确认完整旁白、作出所需的真实时长决定并选择策略，例如“确认完整旁白，选择 user_first”；不得先完成 `approve-full`，再设置一次只用于选择策略的独立聊天关卡。若用户确认旁白但未指定策略，默认使用 `user_first`。

`audio/narration.srt` 继续作为 current 权威字幕源和技术证据，但此时尚无真实画面，因此不做字幕视觉批准。换行、对比度、遮挡与安全区统一在正式字幕 contact sheet 和最终成片中审查。

两项判断在一次 `approve-full` 原子事务中绑定 current `FULL_IDENTITY`，并校验 narration WAV、timeline 与 narration SRT：

```powershell
# 偏差在 10% 阈值内：
<ENV_PY> scripts/generate_voiceover.py approve-full `
  --project <项目根目录> `
  --identity-hash <刚完整试听旁白所对应的 FULL_IDENTITY>

# 偏差超过 10%，且用户明确接受真实时长：
<ENV_PY> scripts/generate_voiceover.py approve-full `
  --project <项目根目录> `
  --identity-hash <FULL_IDENTITY> `
  --duration-decision accept_actual
```

`--identity-hash` 必须是 current full identity；不匹配时返回 5，且不得修改旧批准。阈值内不得传 `accept_actual` 冒充超阈值人工决定；manifest 应记录 `within_threshold`。超阈值未显式接受时返回 5，另一条合法路径是修改 rate/文本后重新生成、完整试听和批准。

coordinator 收到同时包含两项决定的回复后，必须先核对 current `FULL_IDENTITY` 并成功执行 `approve-full`，再采用审阅策略并开始生图；旁白被拒绝、identity stale 或超阈值时长未获接受时，不得只凭同一回复中的策略选择启动任何视觉生成。审阅策略不是人工批准，不进入作品 identity，也不能替代后续线稿、annotation review bundle、scene review bundle 或最终成片批准。

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

全部单幕完成串行渲染/检查并通过 current scene review bundle 联合批准后，连续生成并技术验证 clean master、烧录 narration SRT，再执行：

```powershell
<ENV_PY> scripts/mux_voiceover.py --project <项目根目录>
<ENV_PY> scripts/validate_final_media.py --project <项目根目录>
```

mux 只允许 Edge 模式，要求 current full approval、captioned video、WAV、timeline、字幕和 delivery identity 全部有效。它以 `-c:v copy` 保持已经烧录字幕的视频，以 `AAC 192k / 24000Hz / mono` 编码旁白，原子发布 `output/final.mp4`，输出 `FINAL_IDENTITY`。

完整旁白批准和技术验证都不等于最终人工批准。用户必须完整看片听音，确认最终真实画面上的字幕、画面、音频和尾部均无截断后，才允许执行：

```powershell
<ENV_PY> scripts/approve_final_media.py `
  --project <项目根目录> `
  --identity-hash <刚完整看片听音的 FINAL_IDENTITY>
```

final identity 覆盖 clean video、audio、timeline、权威字幕、样式、字体、render profile、timing plan、burn/mux contract 和 final SHA。任一输入变化都会使最终批准 stale。

## stale 矩阵

| 变化 | 必须 stale | 可保留 |
|---|---|---|
| topic/body/rewritePolicy/target/narration cue/scene mapping 改变 | content/source package、voice plan、full 批准、音频、timeline、narration SRT 和全部相关下游 | 仅按 current identity 重新判定可复用 segment |
| 仅 imagePrompt 改变，cue/scene boundary 不变 | generation plan、图片和视觉下游 | current 音频、timeline 与 narration SRT |
| voice/rate/朗读文本/分段边界/provider synthesis contract | sample/full 批准、受影响段、WAV、timeline、narration SRT、annotation、场景视频、captioned/final、最终批准 | synthesis identity 未变的其他段；图片需语义复核 |
| source 仅改时间，朗读文本与分段不变 | 时长决定、full 批准、timeline、narration SRT、annotation 和下游 | synthesis identity/current WAV 合法的 segments |
| narration WAV 改变 | full 批准、timeline、annotation 和全部下游 | 图片 |
| timeline/timing plan 改变 | narration SRT、annotation 时序、场景视频、captioned/final、最终批准 | 图片 generation plan/manifest；音频按 identity 调查 |
| render profile 或 mode 改变 | timing、annotation、所有视频与最终批准 | 图片和音频可保留但需重新绑定/复核 |
| captioned video、AAC 参数或 final SHA 改变 | final、最终批准 | 上游 current 产物 |

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

自动测试使用 fake provider 或固定 WAV fixture，不默认调用外网。fixture 可验证 planner、异常分类、恢复、canonical WAV、timeline、字幕来源、AAC mux、stale 和 identity，但不能证明微软 Edge 服务当前可用或声音已获用户接受。

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

不得报告为 PASS，也不得用 SKIP 或 fixture PASS 冒充真实 Edge 已验收。真实图片 provider 与人工视觉/声音判断也必须单列为 SKIP/BLOCK/待用户确认。本文只描述验收合同，不声明当前环境已经完成真实 Edge、真实图片 provider 或人工验收。

完整旁白批准只提前确认 current 配音与真实时长，不替代线稿、一次性 annotation review bundle 批准、一次性 scene review bundle 批准、最终字幕烧录/contact sheet 或最终成片完整看片听音批准。annotation 技术 current 后可先生成本地区域预览，再把标注内容、预览、`protectedRegions` 与 reveal 时序合并确认；scene 仍串行渲染和逐幕技术检查，但只对有序 current bundle 做一次人工批准。clean master 只是连续成片链路中的技术中间工件，不设独立人工确认。
