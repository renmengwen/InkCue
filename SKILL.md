---
name: srt-whiteboard-animation
description: 将主题、正文或 SRT 制作成暖米黄纸张底、按叙事顺序流式落墨的白板手绘视频；支持传统 SRT 的无旁白/Edge TTS/MiniMax/豆包语音路径，以及经一次内容与制作方案联合确认后派生严格 SRT 的 topic/text 路径。用户要求“把主题/正文/SRT 做成白板手绘视频”“按文案分镜画手绘”“生成带字幕或在线旁白的白板动画”时触发。
---

# SRT 白板动画：路由与不变量

本文件只负责入口路由、质量 Gate、不可变合同和命令索引；阶段细则以 references 为唯一来源。输出为 1920×1080、60fps 的暖米黄白板动画，所有面向用户的说明、分镜、配置和界面文字使用中文。不得把目录防覆盖、路径校验、candidate/正式文件分开、role 写入边界或普通 fallback 统称为“隔离/安全保护”，应报告真实原因。

## 输入路由

外部输入固定为 `inputMode = srt | topic | text`：

| inputMode | rewritePolicy | voiceoverMode 来源 |
|---|---|---|
| `srt` | 不适用 | 默认读取 `activeProvider`；明确静音时为 `disabled` |
| `topic` | 仅 `generate` | 自动读取 `activeProvider` |
| `text` | `preserve | polish` | 自动读取 `activeProvider` |

`topic + preserve/polish`、`text + generate` 非法。非 SRT 必须有 15–600 秒 `targetDurationSeconds`；缺失时可建议 60 秒，但要与其他缺失配置一次性展示并等待确认。`voiceoverMode` 不属于用户选择项：除非用户明确要求静音，否则必须读取 skill 根目录 `config/voice-providers.local.json` 的 `activeProvider`，规范化为 `edge-tts`、`minimax` 或 `doubao` 后自动冻结。不得询问用户在 Edge TTS、MiniMax 与豆包语音之间选择，也不得从项目目录、旧项目 manifest、命令行 provider 参数或对话回复读取旁白 provider。

topic/text 先冻结最小输入和 `contentDrafting` attempt；child 候选经只读校验后，coordinator 生成审阅 artifact。内容、target、rewritePolicy、由 activeProvider 派生的 voiceoverMode、cue→scene、分镜、图片提示词、是否加入 BGM、阶段 0 后是否委托 AI 代理批准，以及 `imageGenerationMode`，必须通过一次“内容与制作方案联合确认”同时冻结；确认前不得运行 `prepare_source.py`、创建正式项目或写批准。review 中可以展示“当前已采用：豆包语音/MiniMax/Edge TTS”供用户知情查看，但不把旁白 provider 变成待选择问题。`backgroundMusic.enabled` 与 `agentApprovalEnabled` 是两个独立布尔选择：BGM 只询问“加入/不加入”，加入时固定使用内置 CC0 轻音乐和 `-15 dB` 预设；代理批准只询问“逐阶段由我确认/委托 AI 自动判断并推进”，二者都不增加独立 Gate。

`imageGenerationMode = provider | gpt-login` 是正式项目字段。只有当前 Codex/ChatGPT 确实使用 GPT 账号登录且宿主内置 `image_gen` 可用时，才在上述联合确认中询问“当前登录的 GPT 账号/已配置图片供应商”；否则不询问并直接冻结为 `provider`。用户已经明确指定时不重复询问，只展示当前已采用；若明确指定 `gpt-login` 但宿主能力不可用，则 `BLOCKED`，不得静默切换 provider 或需要 API Key 的 CLI。传统 SRT 在首次分镜确认时一并冻结 BGM、代理批准和生图方式，不新增 Gate。旧项目缺失字段按 `provider` 读取；仅对尚未生图的旧项目，若当前满足 GPT 登录态与 `image_gen` 能力条件，coordinator 可在首次生图前补问一次并持久化选择。详见 [references/phase-0-content.md](references/phase-0-content.md)、[references/content-input.md](references/content-input.md) 与 [references/image-generation.md](references/image-generation.md)。

`agentApprovalEnabled` 写入正式项目；旧项目或字段缺失时等价于 `false`。`false` 保持现有人工 Gate；`true` 表示用户只把初次确认之后的常规质量 Gate 委托给 coordinator AI，后者必须实际审阅 current artifact，决定通过或返工，并在通过时调用现有批准动作。该字段不删除 Gate，不新增 identity、manifest、状态机或专用恢复协议，也不授权 child、CLI 或 runner 自行批准。

权威时钟：`disabled` 使用 `source/source.srt` 原始全局时间轴；Edge/MiniMax/豆包语音使用获批 provider 生成的真实 audio timeline 与 `audio/narration.srt`。`targetDurationSeconds` 只作内容预算和 provisional SRT，不是成片时钟。

## 七个工作阶段与交付链

1. **阶段 0：内容与制作方案联合确认**。冻结输入、旁白稿、cue、scene、target、rewritePolicy、voiceoverMode、generation plan、`backgroundMusic.enabled`、`agentApprovalEnabled` 和 `imageGenerationMode`；topic/text 由 `contentDrafting` 候选开始，确认前 fail-closed。
2. **阶段 1：严格 SRT 与分镜确认**。传统 SRT 严格解析、时长约束和 `storyboardPlanning` candidate/result 交接；用户首次确认分镜并同时冻结 BGM、代理批准与生图方式后才可建项。
3. **阶段 3：样音与 voice/rate 确认**。音频旁白 provider 生成 sample，由当前批准主体真实试听并绑定 current `SAMPLE_IDENTITY`；未批准时完整旁白以退出码 5 拒绝。
4. **阶段 4：完整旁白、真实时长与 review policy 确认**。整篇旁白以单次 provider 请求生成 current WAV，再通过当前 skill 自带的本地 FunASR runner 与已确认原稿对齐后发布 timeline、narration SRT；当前批准主体完整试听并处理时长偏差，超过 10% 还须 `accept_actual`。人工模式由用户选择 review policy；代理批准模式确定性派生 `agent_first`，不再询问。
5. **阶段 5：统一线稿确认**。图片候选独立有界生成、技术校验和 global visual review；线稿保留独立质量 Gate，主窗口只交付 review 文件链接、identity、计数和异常摘要。
6. **阶段 6：annotation、区域预览与 reveal 联合确认**。技术 current 后生成预览和项目 URL；当前批准主体一次检查 annotation、区域、`protectedRegions`、reveal 时序并绑定 current review identity。
7. **阶段 7：正式 scene bundle 确认**。按 `sceneRender` 有界并行生成候选，coordinator 按 generation plan 顺序单写发布；当前批准主体一次检查有序 scene bundle 后才可合并。

阶段 2（创建/升级项目）是阶段 0/1 Gate 之后的确定性建项，不单独增加 Gate。阶段 8–10 为连续交付：静音画面母版合并 → 字幕烧录 →（旁白模式）音频封装、技术验证和最终成片质量确认。clean master 只做技术验证，不能另设确认；`output/final.mp4` 必须由当前批准主体完整看片（旁白还须完整听音）后，才由 coordinator 调用 `approve_final_media.py` 绑定 `FINAL_IDENTITY`。

## 质量 Gate（全部 fail-closed）

- topic/text 的内容与制作方案联合确认，以及传统 SRT 的首次分镜确认，始终由用户亲自完成；这次确认同时冻结 `backgroundMusic.enabled`、`agentApprovalEnabled` 与 `imageGenerationMode`。生图方式只在 GPT 登录态及宿主 `image_gen` 确实可用时作为选择题，否则直接展示并冻结 `provider`。
- `agentApprovalEnabled=false` 或缺失时，后续 Gate 仍由用户亲自批准；`true` 时，coordinator AI 代理用户审阅并批准阶段 0/首次分镜之后的常规质量 Gate。`agent_first` 本身只表示审阅顺序，不能在没有该布尔授权时跳过用户。
- 未回复、笼统授权、技术 `validated`、fixture PASS、child candidate、child findings 或“用户没有反对”都不是批准。代理批准模式下也只有 coordinator 在实际检查 current artifact 并作出通过判断后，才能调用原批准脚本；child 始终 `approvalWritesAllowed:false`，CLI/runner 始终不能自行批准。
- 内容/制作方案联合确认、传统 SRT 分镜确认、样音批准、完整旁白与真实时长批准、线稿确认、annotation/区域/reveal 联合批准、scene bundle 批准、最终成片批准彼此独立。修改上一步只重做受影响步骤并重过对应 Gate；自动模式只是更换后续批准主体。
- 五类持久化 identity 必须绑定 current 字节和证据：`SAMPLE_IDENTITY`、`FULL_IDENTITY`、`annotationReviewIdentitySha256`、`sceneReviewIdentityHash`、`FINAL_IDENTITY`。批准脚本仅批准刚检查的 identity。
- AI 代理批准必须真实查看图片、完整试听音频、完整观看视频；宿主缺少完成当前 Gate 所需的媒体审阅能力时报告 `BLOCKED`，不得用技术 PASS、波形、元数据、抽帧或 child 摘要冒充已看/已听。
- `unknown_external_outcome`（provider 请求后 candidate/receipt 不完整且不能按同一幂等键查询）不得普通重跑或 `--retry-failed` 自动重发；必须单独取得用户承担新外部调用的授权。新的费用、凭据或服务授权、版权授权，以及必须实质改变阶段 0/首次分镜已冻结用户意图的修改，也必须单独询问用户。冻结计划内的正常有界 provider 调用和常规返工不打断用户。

本地 coordinator runner 支持 `annotation-preview` 与 `final-delivery`。前者串联 annotation 确定性校验、receipt、preview/contact sheet；后者只在 current scene bundle 已获批准后连续执行 merge/burn/可选 mux/final validation，并输出逐步耗时。两者到达质量 Gate 都必须停止并保持 `approvalWritten=false`；人工模式由 coordinator 等待用户，代理批准模式由 coordinator 在 runner 外真实审阅、决定返工或调用原批准脚本。runner 本身不读取 `agentApprovalEnabled` 来批准；逐步 CLI 始终保留为调试和恢复路径。字段、Gate 停止与恢复合同见 [references/phase-4-runner.md](references/phase-4-runner.md)。
- runner 技术链完成并停在 Gate 时进程退出码为 0，结构化状态仍使用现有 `WAITING_HUMAN_GATE` 且 `approvalWritten=false`。退出码 0 只避免 PowerShell/桌面包装层误报技术失败，不表示批准；任何自动化都必须读取 JSON 状态，不能据退出码越过 Gate。
- preview 服务必须由 `serve_preview.py --ensure --project <root>` 启动/复用，并验证 `PREVIEW_READY=PASS`、项目 API、全部 ready/current scene 后交付完整 `PREVIEW_URL`；失败报告 `BLOCKED/FAIL` 真实原因。

| Gate | 必须检查者与内容 | 通过后允许 |
|---|---|---|
| 内容与制作方案 | 用户：完整旁白稿、cue/scene、分镜、图片提示词、target、配音策略、BGM、代理批准与生图方式 | 确定性派生 source、建项 |
| 传统 SRT 分镜 | 用户：严格 SRT 解析结果、分镜 candidate、BGM、代理批准与生图方式 | 建项 |
| 样音 | 当前批准主体：current sample 的 voice/rate | 生成完整旁白 |
| 完整旁白 | 当前批准主体：current WAV 全程、真实时长差值、narration SRT | 使用 canonical audio timeline |
| 线稿 | 当前批准主体：current 有序全量线稿 review artifact | annotation batch |
| annotation 联合审阅 | 当前批准主体：annotation、区域预览、保护区和 reveal 时序 | 正式逐幕 render |
| scene bundle | 当前批准主体：current 有序 scene review bundle | merge、字幕、mux、技术验证 |
| 最终成片 | 当前批准主体：current `output/final.mp4` 全程画面与声音 | 写 final approval |

## 核心不变量（实现和批准边界不得弱化）

1. coordinator 是唯一用户接口和正式 writer；只有它能写正式 `scenes/*.png`、`audio/segments/*.wav`、manifest、timeline、SRT、identity、stale、checkpoint 与批准。
2. child 只能写其冻结 attempt 内 candidate/log/result（`result.json` 通常由 coordinator 确定性生成），`formalWritesAllowed:false`、`approvalWritesAllowed:false`。
3. 只有 coordinator 能根据当前真实宿主状态调用 `spawn_agent`、向已存在 child 发送 `followup`、等待或决定 fallback；任何 Python 脚本都不得接收/推断 child slots、宿主 role capability 或 coordinator budget，不得生成 `spawnAgentCall`/`spawnRequest`，也不得替宿主决定 dispatch/fallback。
4. coordinator 从实际可用 child slots 计算 effective agent concurrency：取 configured、ready task/unit 和当前可用 child slots 的最小值，始终保留 coordinator 槽位；`execution.agents` 与 worker concurrency 分离，不相乘。具备所需工具时优先真实派发；只有真实派发不可用时才允许 coordinator fallback，并报告宿主真实原因；双方缺能力时 `BLOCKED`。
5. attempt 是持久化版本边界，不是执行者边界。首次 `contentDrafting`、`storyboardPlanning` 与独立 `visualReview` 使用短上下文 child；用户对 content 草案提出修订时仍创建新 attempt，但优先 `followup` 上一 attempt 的同 role 原 child（它仍存在、idle、上一结果 completed 且 role contract 兼容时），让它读取新的 task/base/revision SHA。原 child 不可用、失败、role 改变、修改升级为全面独立重写或用户明确要求换执行者时才 spawn 新 child。同一 attempt 的执行性补正也 followup 原 child。`annotationDrafting` 一幕一 attempt，最多 3 个连续 scene 组成 unit；child prompt 只含冻结 task/role 定位与 SHA。
6. `imagePrompt`（content draft）到 formal generation plan 的 `prompt` 只允许 coordinator 确定性映射；child 不接收完整主对话、完整 SRT、provider 凭据、长日志或批准信息。详见 [references/prompt-writing.md](references/prompt-writing.md) 与 [references/subagent-orchestration.md](references/subagent-orchestration.md)。
7. 每幕只表达一个核心视觉命题；可独立揭示的 2–3 个视觉簇之间保持真实纸面留白，不以道路/河流/山脉/箭头等贯穿结构连接，除非该结构本身不可分割。annotation 按连续墨迹簇划分，最多 3 个且不为凑数强拆。
8. reveal 时间严格串行、不可重叠；空间 region 仅在真实遮挡/交界处适度重叠；`protectedRegions` 只能保护正确分区中不可避免的局部，不能掩盖错误分区。
9. 图片采用 `continue_independent`：单幕失败不阻止其他幕候选，但任一必需 scene 缺失/失败/stale 时 batch 总状态为 FAIL，不得启动全量预览或写批准。完整 TTS 固定为一个 full-track task；不得因 provider/ASR 失败回退成逐句多请求。
10. provider worker 或宿主图片结果导入只能写已登记 attempt 的 candidate/去敏 receipt；coordinator 按 `prepared → requesting → candidate_ready → publishing → validated` 串行 checkpoint、重验、原子发布和清理。`gpt-login` 只替换图片字节来源，不另建 Gate、manifest 或发布链。
11. `sceneRender` 是当前正式单幕候选的有界并行能力；worker 数只读 workspace `execution.concurrency.sceneRender`（缺失继承 pool default，最终默认 1，范围 1–16），候选完成顺序不得改变 generation plan 顺序或正式 manifest。
12. image validation 每张 PNG 同一打开周期只完整解码一次；voice deep validation、timeline、SRT、累计帧、identity、binding 和 approval 仍按合同串行/有界，证据缺失或 bytes 变化不得降级为 binding PASS。
13. 正式成片永远烧录字幕：disabled 为 H.264、0 音频且使用 source SRT；Edge/MiniMax/豆包语音为 H.264 + AAC 旁白且使用 current narration SRT。旁白项目 `backgroundMusic.enabled=true` 时，最终封装在同一路 AAC 中按固定 `-15 dB` 混入内置 CC0 BGM；关闭或旧项目缺字段时保持原旁白封装。旁白模式缺少 current narration SRT/timeline/full approval/identity 必须失败，不能回退 source SRT。
14. 任一输入、旁白文本/分段、scene mapping、imagePrompt、音频/timing/render binding、annotation/reveal、scene 集合/顺序、手部素材 `handSha256`、字幕 preset/字体/SRT 或 clean/final SHA 变化，按 [references/recovery-and-identity.md](references/recovery-and-identity.md) 使受影响 identity 和批准 stale；历史 stale 证据不得作为 current 输入。
15. 每次进入工作区前先用当前可启动的 Python 运行 `prepare_env.py --check-workspace-access`，完成真实 create/write/flush/read/delete 预检；随后在任何业务脚本、导入探测或渲染启动前运行 `python scripts/prepare_env.py --check`，读取末行 `ENV_PY=<绝对解释器路径>`。若仅因专用环境或依赖尚未就绪而失败，运行不带 `--check` 的同一脚本完成准备并重新读取 `ENV_PY`。本次任务后续每条 Python 命令都必须直接调用该绝对路径；不得使用裸 `python`、`py`、shebang 或不会跨工具调用持久化的临时 shell 变量先试跑再回退，也不得把系统 Python 缺少 `cv2` 误报成 OpenCV 未安装。宿主 `CreateProcess rejected by policy` 与 Windows 文件写入拒绝必须分开报告；UI 刚切换权限时在新回合重跑预检，不用复杂 shell 写删命令试探。
16. 旁白项目的 `approve-full` 必须显式带 `--review-policy user_first|agent_first` 并写入 `fullApproval.reviewPolicy`；后续线稿、annotation preview 和 scene review 自动继承且拒绝冲突值。`agentApprovalEnabled=true` 时由项目授权确定性派生 `agent_first`，不得再次询问或接受冲突值；为 `false`/缺失时仍由用户选择，不得静默采用默认策略。

## current、stale 与恢复摘要

- candidate、`completed`、validator PASS、review findings 和发布成功均不自动成为 approved；用户亲自批准或 AI 代理批准都必须绑定刚刚实际检查的 current artifact。
- source 内容/策略/scene mapping 变化时，从 content/source/timing 到 final 的受影响链全部重新判定。
- 仅 `imagePrompt` 变化且 cue/scene boundary 不变时可保留有效音频，但 generation plan、图片和视觉下游 stale。
- voice/rate/provider synthesis contract、朗读文本或分段变化时，样音、full audio/timeline、视觉时序与 final 的受影响链 stale。
- 只有 source timing 变化且 synthesis identity 不变时，可按合同复用 validated audio 字节，但完整旁白时长决定、timeline 与下游重新绑定。
- annotation/preview/保护区/reveal 变化使 annotation review approval stale；只重建受影响 preview。
- scene 视频、render identity、hand SHA、scene 集合或 plan 顺序变化使 scene review approval stale；合并前必须重建并批准 bundle。
- 字幕 preset、字体、权威 SRT 或 encoding contract 变化使字幕/final stale，但不应重建仍 current 的 clean video 或 audio。
- 失败/取消/stale candidate 不得覆盖已验证正式文件；恢复只从最后一个 current checkpoint 继续。
- 任何重试先区分确定失败、可查询请求和 `unknown_external_outcome`，详细决策只读 recovery reference。

## 当前能力与失败边界

支持真实 Edge TTS、MiniMax、豆包语音、图片 provider（按配置）、当前 GPT 登录态可用的宿主内置 `image_gen`，以及 fixture/fake provider 自动测试；不以 fixture、技术验证或 child/AI review findings 冒充真实外部或质量批准。`gpt-login` 不是 provider 配置或浏览器 cookie 路径，不读取 `image-providers.local.json`、不需要 API Key，也不得静默改用 provider/API CLI。正式 render 使用 BGR24 stdin → libx264 单次编码；禁止用 `--fps`、`--total-ms`、`--cap-long-edge` 覆盖持久化合同。需要 AI 代理批准时，coordinator 必须使用当前宿主实际可用的图片、音频或视频消费能力完整检查对应媒体；能力不足即 `BLOCKED`。

### 项目预览链接交付合同

coordinator 必须运行 `serve_preview.py --ensure --project <项目根目录>`，核验 `PREVIEW_READY=PASS`、项目 API 和全部 ready/current scenes 后，才可交付命令返回的完整 `PREVIEW_URL`；不得仅拼接未经验证的地址。服务启动、端口、API 或 scene 完整性任一失败时，必须报告 `BLOCKED`/`FAIL` 及真实原因。
交付消息必须包含命令返回的完整、可点击 `PREVIEW_URL`，并说明打开后自动载入当前项目、无需手动导入；只报告代码已修改、服务已实现、端口号或项目目录均不算预览交付完成。

外部服务、宿主 child capability、FFmpeg/字体/字幕/WAV、端口/API 或文件完整性不足时报告 `BLOCKED`/`FAIL` 及真实原因，不自动扩大范围、不伪造 PASS。详细阶段合同路由：

| 阶段/主题 | 唯一 reference |
|---|---|
| 输入、旁白写作与 source evidence | [phase-0-content.md](references/phase-0-content.md)、[content-input.md](references/content-input.md) |
| prompt、视觉拓扑和字段映射 | [prompt-writing.md](references/prompt-writing.md)、[image-generation.md](references/image-generation.md) |
| child task/result、runtime、fallback、coordinator | [subagent-orchestration.md](references/subagent-orchestration.md) |
| annotation、preview、联合批准 | [annotation-drafting-role.md](references/annotation-drafting-role.md)、[subagent-orchestration.md](references/subagent-orchestration.md) |
| sceneRender、bundle、发布顺序 | [image-generation.md](references/image-generation.md)、[subagent-orchestration.md](references/subagent-orchestration.md) |
| merge、字幕、mux、final | [subtitles.md](references/subtitles.md)、[voiceover.md](references/voiceover.md) |
| stale、identity、retry、恢复 | [recovery-and-identity.md](references/recovery-and-identity.md) |
| voice provider、真实时长 | [voiceover.md](references/voiceover.md) |

当前阶段只读取与正在执行阶段对应的 reference；跨阶段只消费摘要、identity、current/stale/approval 状态，不把完整 prompt、原图、JSON 或长日志回灌主窗口。任何 role contract 需带 contract version 与 SHA 并冻结在 attempt。

### 阶段摘要合同

每次阶段结束只向 coordinator/用户返回可核验摘要：

- `status = PASS | FAIL | BLOCKED | SKIP | 待确认`，并说明真实原因；
- current/stale/approval 状态、对应 identity 和 artifact 路径；
- configured/effective/peak concurrency、task count 与实际 dispatch/fallback 模式（适用时）；
- 失败 scene/unit、是否 partial success、下一条安全恢复命令；
- 质量 Gate 明确列出“批准主体”“需要检查的 artifact”和“通过后允许的下一步”；代理模式的摘要使用“AI 代理批准”，不得声称用户已经看片或听音。

不得用阶段摘要重新嵌入完整正文、完整 prompt、全部图片、完整 JSON、长日志或重复 validator 输出。

## 命令索引（逐步 CLI；命令不会自动批准）

```powershell
# 环境与输入准备
python scripts/prepare_env.py --check-workspace-access
python scripts/prepare_env.py --check
# 上一步仅因专用环境或依赖缺失而失败时：
python scripts/prepare_env.py
# 捕获末行 ENV_PY；后续每次工具调用都直接使用该绝对路径，不用裸 python/py
# 首次生成整轨旁白前准备/检查当前 skill 自带的本地 ASR：
<ENV_PY> scripts/prepare_env.py --feature narration-asr
<ENV_PY> scripts/prepare_env.py --check --feature narration-asr
<ENV_PY> scripts/prepare_draft_agent_task.py contentDrafting --content-input <input.json> --draft-root <draft-root>
<ENV_PY> scripts/validate_content_draft.py --stdin
<ENV_PY> scripts/prepare_draft_agent_task.py storyboardPlanning --draft-root <draft-root> --source-srt <字幕.srt> --target-sec 30 --min-sec 25 --max-sec 35
<ENV_PY> scripts/parse_srt.py <字幕.srt> --target-sec 30 --min-sec 25 --max-sec 35
<ENV_PY> scripts/create_project.py --name <项目名> --srt <source.srt> --plan <generation-plan.json> --source-input <input.json> --source-manifest <manifest.json> --background-music <enabled|disabled> --agent-approval <enabled|disabled> --image-generation-mode <provider|gpt-login>
<ENV_PY> scripts/create_project.py --resume <项目根目录> --srt <原始字幕.srt>
<ENV_PY> scripts/upgrade_project.py --project <项目根目录> --to-schema 2

# 图片、线稿、语音
<ENV_PY> scripts/generate_images.py --project <项目根目录>
<ENV_PY> scripts/generate_images.py --project <项目根目录> --host-results <绝对JSON路径>
<ENV_PY> scripts/validate_generated_images.py --project <项目根目录> --review-policy user_first
<ENV_PY> scripts/validate_generated_images.py --project <项目根目录> --review-policy agent_first
<ENV_PY> scripts/generate_voiceover.py sample --project <项目根目录> --voice <voice> --rate <rate>
<ENV_PY> scripts/generate_voiceover.py approve-sample --project <项目根目录> --identity-hash <SAMPLE_IDENTITY>
<ENV_PY> scripts/generate_voiceover.py full --project <项目根目录>
# CLI 正常执行时会自动调用当前 skill 内部 FunASR runner 并发布对齐结果；调试/恢复也可显式导入 ASR SRT：
<ENV_PY> scripts/generate_voiceover.py publish-alignment --project <项目根目录> --asr-srt <ASR句级字幕.srt>
<ENV_PY> scripts/generate_voiceover.py status --project <项目根目录>
<ENV_PY> scripts/validate_voiceover.py --project <项目根目录> [--force-deep]
<ENV_PY> scripts/generate_voiceover.py approve-full --project <项目根目录> --identity-hash <FULL_IDENTITY> --review-policy <user_first|agent_first> [--duration-decision accept_actual]

# annotation、preview、scene bundle
<ENV_PY> scripts/validate_annotations.py prepare --project <项目根目录> --images-confirmed
<ENV_PY> scripts/validate_annotations.py validate --project <项目根目录> --candidate-root <candidate-root>
<ENV_PY> scripts/serve_preview.py --ensure --project <项目根目录>
<ENV_PY> scripts/generate_annotation_previews.py --project <项目根目录> --all --review-policy user_first
<ENV_PY> scripts/approve_annotation_review.py --project <项目根目录> --identity-hash <annotationReviewIdentitySha256>
<ENV_PY> scripts/render_stream_whiteboard.py --project <项目根目录> --all --ink-path grid --color-fill contour-wipe
<ENV_PY> scripts/scene_review.py --project <项目根目录> --review-policy user_first
<ENV_PY> scripts/approve_scene_review.py --project <项目根目录> --identity-hash <sceneReviewIdentityHash>

# 连续交付与最终 Gate
<ENV_PY> scripts/merge_scenes.py --project <项目根目录> --inputs <幕1.mp4> <幕2.mp4>
<ENV_PY> scripts/burn_subtitles.py --project <项目根目录>
<ENV_PY> scripts/mux_voiceover.py --project <项目根目录>
<ENV_PY> scripts/validate_final_media.py --project <项目根目录>
<ENV_PY> scripts/run_phase.py --project <项目根目录> --phase final-delivery
<ENV_PY> scripts/approve_final_media.py --project <项目根目录> --identity-hash <FINAL_IDENTITY>
```

`prepare_*`/`agent_first` 只冻结 attempt、task descriptor 或有序 unit；这些 artifact 不包含宿主调用参数，也不等于真实派发、candidate 完成或任何批准。coordinator 必须直接使用宿主协作工具并记录真实 agent/task 映射。正式 scene 由 `sceneRender` 有界并行生成、按 plan 顺序发布；`merge_scenes.py` 在任何写入前硬校验 current approved scene bundle。

## 退出码

| 码 | 权威含义 |
|---:|---|
| 0 | 操作成功且对应技术验证通过 |
| 1 | 批处理失败/取消 unit；环境准备和 standalone helper 本地操作失败 |
| 2 | content draft、参数、项目、配置、plan、manifest、SRT 或 timeline 无效 |
| 3 | Edge 外部请求失败或限流重试耗尽 |
| 4 | FFmpeg、ffprobe、字体、字幕、WAV 或媒体验证失败 |
| 5 | stale、identity 不匹配或缺少所需批准 |

脚本只返回与职责有关的子集，但正式项目必须遵循上表；`unknown_external_outcome` 另按恢复 reference 等待用户决定。

## 质量底线

- 首帧是干净暖米黄纸底，未开始区域完全隐藏；末尾至少保留 0.5 秒且不突破权威总时长。
- reveal 不重叠，annotation 使用局部时钟且不越过 scene 边界；允许掩码和 `protectedRegions` 合同 current。
- generation plan、manifest、图片 SHA、1920×1080 实际尺寸和线稿确认均 current。
- 正式 scene 使用 current `assets/drawing-hand.png`，manifest `handSha256` 匹配；`@moveR` 版权层未经明确授权不得修改或移除。
- 单幕和 clean master 帧数符合累计全局帧边界并全部完整解码；merge 已校验获批 bundle。
- `subtitles/final.ass`、字体 hash、样式 hash、权威 SRT 与 contact sheet 写入 delivery evidence。
- disabled final 必须 1 路 H.264、0 音频；旁白 final 必须 1 路 H.264 + 1 路 24kHz mono AAC；两者都有可见烧录字幕。
- 自动测试不调用真实 provider；外网或服务不可用写 `BLOCKED`，不以 fixture PASS、SKIP 或技术 `validated` 冒充外部或质量批准 PASS。
