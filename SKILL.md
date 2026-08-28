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

用户明确说“新任务”“不要沿用旧任务”或同义表达时，该意图立即覆盖任何基于同名目录、相似输入或历史 artifact 的恢复推断：阶段 0 使用新的 `draft-root`，建项走新建路径且不得使用 `--resume`。只有用户明确要求继续某个既有项目时，才进入恢复路径。

`topic + preserve/polish`、`text + generate` 非法。非 SRT 必须有 15–600 秒 `targetDurationSeconds`；缺失时可建议 60 秒，但要与其他缺失配置一次性展示并等待确认。`voiceoverMode` 不属于用户选择项：除非用户明确要求静音，否则必须通过 `voice_provider_config.py status` 的脱敏接口读取 active provider，规范化为 `edge-tts`、`minimax` 或 `doubao` 后自动冻结；禁止用 shell、文件读取工具或临时代码直接读取、打印、转述任何 `config/*.local.json` 原文。不得询问用户在 Edge TTS、MiniMax 与豆包语音之间选择，也不得从项目目录、旧项目 manifest、命令行 provider 参数或对话回复读取旁白 provider。

topic/text 先冻结最小输入和 `contentDrafting` attempt；child 候选经只读校验后，coordinator 生成审阅 artifact，再确定性派生 source 并创建明确标记为 `pending_initial_approval` 的预项目。预项目只允许阶段 0 审阅、current 样音生成/技术验证、草案或样音修订与联合批准；完整旁白、正式生图、annotation、render、merge、burn、mux、final 必须在各自入口重验并硬拒绝 pending 项目。review 可以展示“当前已采用：豆包语音/MiniMax/Edge TTS”，但 active voice provider 不是用户选择项。

旁白项目在预项目内生成绑定 current 草案/voice plan 的真实样音。用户一次检查草案、制作方案并试听 current 样音，然后从 coordinator 按当前能力生成的完整自然语言句子中复制一句或回复编号。合法通过句原子完成 content 与 sample 批准、冻结 `backgroundMusic.enabled`、`agentApprovalEnabled`、`imageGenerationMode`，把 `project.json.initialApproval` 从 pending 提升为 approved；任一 current identity、pending 状态、选项或能力重验失败不得留下半批准状态。旧项目缺少 `initialApproval` 时按已完成初始批准读取，缺少 `agentApprovalEnabled` 时仍按 `false`。草案或 voice/rate 修改只使受影响 identity/样音及下游 stale，不静默复用旧样音批准。用户明确要求新任务时总是创建新预项目，不 resume 旧项目。

`imageGenerationMode = provider | gpt-login` 是正式项目字段。只有当前 Codex/ChatGPT 确实使用 GPT 账号登录且宿主内置 `image_gen` 可用时，才在上述联合确认中询问“当前登录的 GPT 账号/已配置图片供应商”；否则不询问并直接冻结为 `provider`。用户已经明确指定时不重复询问，只展示当前已采用；若明确指定 `gpt-login` 但宿主能力不可用，则 `BLOCKED`，不得静默切换 provider 或需要 API Key 的 CLI。传统 SRT 在首次分镜确认时一并冻结 BGM、代理批准和生图方式，不新增 Gate。旧项目缺失字段按 `provider` 读取；仅对尚未生图的旧项目，若当前满足 GPT 登录态与 `image_gen` 能力条件，coordinator 可在首次生图前补问一次并持久化选择。详见 [references/phase-0-content.md](references/phase-0-content.md)、[references/content-input.md](references/content-input.md) 与 [references/image-generation.md](references/image-generation.md)。

`agentApprovalEnabled=false` 保持逐阶段人工 Gate。为 `true` 表示用户已经通过 current 样音授权声音主观方向，后续完整旁白与最终成片不再要求 AI 冒充完整听音：严格技术链通过后，coordinator 调用既有批准动作并把 `approvalBasis`/`reviewBasis` 记录为“用户样音授权后的技术推进”。完整旁白仍必须满足整轨单次 provider、canonical WAV、完整解码、本地 FunASR、原稿对齐、cue/scene/timeline/narration SRT、current identity 和时长偏差检查；超过 10% 时按该授权采用真实音频时钟，不再询问。最终交付仍必须满足 current full audio、字幕、AAC、流结构、完整解码、时长/帧数/尾部、BGM 固定混音和 `FINAL_IDENTITY` 技术证据。不得声称 AI 完整听过旁白或最终成片。视觉 Gate 在宿主能看图/视频时仍对 current artifact 实际检查；人工模式的完整旁白和最终看片听音 Gate 不变。

权威时钟：`disabled` 使用 `source/source.srt` 原始全局时间轴；Edge/MiniMax/豆包语音使用获批 provider 生成的真实 audio timeline 与 `audio/narration.srt`。`targetDurationSeconds` 只作内容预算和 provisional SRT，不是成片时钟。

## 七个工作阶段与交付链

1. **阶段 0：预项目、样音与一次联合确认**。先完成旁白稿、cue、scene、分镜和 generation plan 候选，创建 pending 预项目并生成 current 样音；用户以一条完整句或编号原子批准 current content/sample identity 并冻结 BGM、后续模式和生图方式。传统静音 SRT 不生成或要求样音，使用“字幕与分镜方案通过……”语义完成初始批准。
2. **阶段 1：严格 SRT 与分镜确认**。传统 SRT 严格解析、时长约束和 `storyboardPlanning` candidate/result 交接；用户首次确认分镜并同时冻结 BGM、代理批准与生图方式后才可建项。
3. **阶段 3：样音后语音执行**。current 样音已在初始联合批准中绑定；完整旁白仍以单次 provider 请求生成 current WAV，并完成本地 FunASR token 级时间戳、权威原稿对齐与语义安全字幕切句。人工模式完整试听并处理时长偏差；自主模式以用户样音授权为主观依据，仅在全部严格技术证据 current 后写技术推进批准，超过 10% 自动采用真实音频时钟。
4. **阶段 4：真实时间轴与 review policy**。发布 timeline、narration SRT 和 `FULL_IDENTITY`；人工模式由用户选择 review policy，自主模式确定性派生 `agent_first`，但该值不表示 AI 完整试听。
5. **阶段 5：统一线稿确认**。图片候选独立有界生成、技术校验和 global visual review；线稿保留独立质量 Gate，主窗口只交付 review 文件链接、identity、计数和异常摘要。
6. **阶段 6：annotation、区域预览与 reveal 联合确认**。技术 current 后生成预览和项目 URL；当前批准主体一次检查 annotation、区域、`protectedRegions`、reveal 时序并绑定 current review identity。
7. **阶段 7：正式 scene bundle 确认**。按 `sceneRender` 有界并行生成候选，coordinator 按 generation plan 顺序单写发布；当前批准主体一次检查有序 scene bundle 后才可合并。

阶段 2 是把已联合批准的 pending 预项目原子提升为正式可执行项目，不单独增加 Gate。阶段 8–10 为连续交付：静音画面母版合并 → 字幕烧录 →（旁白模式）音频封装和技术验证。clean master 不设确认；人工模式仍须用户完整看片听音后批准 `FINAL_IDENTITY`，自主模式则在严格 final 技术证据 current 后以 `reviewBasis=user_sample_authorization_technical_validation`（或项目实现的等价固定审计值）批准，不声称发生完整视听审阅。

## 质量 Gate（全部 fail-closed）

- 初始联合确认始终由用户亲自完成并绑定 current content identity；旁白项目还必须绑定 current `SAMPLE_IDENTITY`。生图方式只有在 GPT 登录态 `image_gen` 与已配置图片供应商同时真实可用时才展开为 8 个通过句，否则冻结唯一合法方式并显示 4 个完整通过句。不可用组合不得展示。
- 固定生图方式的四个旁白通过句逐字为：“草案和样音通过，使用 BGM，后续由 AI 自主推进至成片。”“草案和样音通过，不使用 BGM，后续由 AI 自主推进至成片。”“草案和样音通过，使用 BGM，后续由我逐阶段确认。”“草案和样音通过，不使用 BGM，后续由我逐阶段确认。”两种生图能力同时可用时，每句增加“使用当前登录的 GPT 账号生成图片”或“使用已配置图片供应商生成图片”，例如“草案和样音通过，使用 BGM，使用当前登录的 GPT 账号生成图片，后续由 AI 自主推进至成片。”
- 三个返工句逐字为：“草案需要修改，当前样音暂不批准。修改意见：……”“草案通过，样音需要调整，其他方案保持不变。调整意见：……”“草案和样音都需要修改。修改意见：……”。不提供斜杠填空句，也不把 active voice provider 列成选项。
- `agentApprovalEnabled=false` 或缺失时，后续声音与最终 Gate 仍由用户真实审阅；`true` 时只有视觉 Gate 继续要求实际媒体检查，声音/final 采用用户样音授权后的严格技术推进审计。技术推进不是听音 PASS。
- 未回复、笼统授权、技术 `validated`、fixture PASS、child candidate、child findings 或“用户没有反对”都不是批准。代理批准模式下也只有 coordinator 在实际检查 current artifact 并作出通过判断后，才能调用原批准脚本；child 始终 `approvalWritesAllowed:false`，CLI/runner 始终不能自行批准。
- 初始 content/sample 联合批准是一个原子动作；线稿、annotation/区域/reveal、scene bundle 等视觉 Gate 仍独立。人工模式保留完整旁白与 final Gate；自主模式只用最小审计字段区分技术推进，不复制批准系统。修改上一步只重做受影响步骤。
- 五类持久化 identity 必须绑定 current 字节和证据：`SAMPLE_IDENTITY`、`FULL_IDENTITY`、`annotationReviewIdentitySha256`、`sceneReviewIdentityHash`、`FINAL_IDENTITY`。批准脚本仅批准刚检查的 identity。
- AI 视觉批准必须真实查看 current 图片/视频；能力不足时 `BLOCKED`。自主声音/final 路径不得把技术 PASS、波形、元数据、抽帧或 child 摘要描述成已听，只能准确报告“用户样音授权后的技术推进”。
- `unknown_external_outcome`（provider 请求后 candidate/receipt 不完整且不能按同一幂等键查询）不得普通重跑或 `--retry-failed` 自动重发；必须单独取得用户承担新外部调用的授权。新的费用、凭据或服务授权、版权授权，以及必须实质改变阶段 0/首次分镜已冻结用户意图的修改，也必须单独询问用户。冻结计划内的正常有界 provider 调用和常规返工不打断用户。
- 图片不设全局禁字：新 generation plan 默认 `constraints.forbidText=false`，允许语义需要的画内文字。视觉核对不得因“出现文字”本身判失败，只检查文字是否清晰、正确、符合语义且没有乱码、意外内容或供应商水印；旧项目或用户明确要求的 `forbidText=true` 仍按该计划执行。

本地 coordinator runner 支持 `annotation-preview` 与 `final-delivery`。前者串联 annotation 确定性校验、receipt、preview/contact sheet；后者只在 current scene bundle 已获批准后连续执行 merge/burn/可选 mux/final validation，并输出逐步耗时。两者到达质量 Gate 都必须停止并保持 `approvalWritten=false`；人工模式由 coordinator 等待用户，代理批准模式由 coordinator 在 runner 外真实审阅、决定返工或调用原批准脚本。runner 本身不读取 `agentApprovalEnabled` 来批准；逐步 CLI 始终保留为调试和恢复路径。字段、Gate 停止与恢复合同见 [references/phase-4-runner.md](references/phase-4-runner.md)。
- runner 技术链完成并停在 Gate 时进程退出码为 0，结构化状态仍使用现有 `WAITING_HUMAN_GATE` 且 `approvalWritten=false`。退出码 0 只避免 PowerShell/桌面包装层误报技术失败，不表示批准；任何自动化都必须读取 JSON 状态，不能据退出码越过 Gate。
- preview 服务必须由 `serve_preview.py --ensure --project <root>` 启动/复用，并验证 `PREVIEW_READY=PASS`、项目 API、全部 ready/current scene 后交付完整 `PREVIEW_URL`；失败报告 `BLOCKED/FAIL` 真实原因。

| Gate | 必须检查者与内容 | 通过后允许 |
|---|---|---|
| 初始联合批准 | 用户：current 草案/字幕分镜、制作方案；旁白项目还须试听 current 样音 | 原子冻结 BGM/后续模式/生图方式并提升 pending 预项目 |
| 传统 SRT 分镜 | 用户：严格 SRT 解析结果、分镜 candidate、BGM、代理批准与生图方式 | 建项 |
| 完整旁白 | 人工模式真实试听；自主模式重验严格技术证据并记录样音授权后的技术推进 | 使用 canonical audio timeline |
| 线稿 | 当前批准主体：current 有序全量线稿 review artifact | annotation batch |
| annotation 联合审阅 | 当前批准主体：annotation、区域预览、保护区和 reveal 时序 | 正式逐幕 render |
| scene bundle | 当前批准主体：current 有序 scene review bundle | merge、字幕、mux、技术验证 |
| 最终成片 | 人工模式完整看片听音；自主模式重验 current final 全套技术证据 | 写入区分真实审阅/技术推进的 final approval |

## 核心不变量（实现和批准边界不得弱化）

1. coordinator 是唯一用户接口和正式 writer；只有它能写正式 `scenes/*.png`、`audio/segments/*.wav`、manifest、timeline、SRT、identity、stale、checkpoint 与批准。
2. child 只能写其冻结 attempt 内 candidate/log/result（`result.json` 通常由 coordinator 确定性生成），`formalWritesAllowed:false`、`approvalWritesAllowed:false`。
3. 只有 coordinator 能根据当前真实宿主状态调用 `spawn_agent`、向已存在 child 发送 `followup`、等待或决定 fallback；任何 Python 脚本都不得接收/推断 child slots、宿主 role capability 或 coordinator budget，不得生成 `spawnAgentCall`/`spawnRequest`，也不得替宿主决定 dispatch/fallback。
4. coordinator 从实际可用 child slots 计算 effective agent concurrency：取 configured、ready task/unit 和当前可用 child slots 的最小值，始终保留 coordinator 槽位；`execution.agents` 与 worker concurrency 分离，不相乘。具备所需工具时优先真实派发；只有真实派发不可用时才允许 coordinator fallback，并报告宿主真实原因；双方缺能力时 `BLOCKED`。
5. attempt 是持久化版本边界，不是执行者边界。首次 `contentDrafting`、`storyboardPlanning` 与独立 `visualReview` 使用短上下文 child；用户对 content 草案提出修订时仍创建新 attempt，但优先 `followup` 上一 attempt 的同 role 原 child（它仍存在、idle、上一结果 completed 且 role contract 兼容时），让它读取新的 task/base/revision SHA。原 child 不可用、失败、role 改变、修改升级为全面独立重写或用户明确要求换执行者时才 spawn 新 child。同一 attempt 的执行性补正也 followup 原 child。`annotationDrafting` 一幕一 attempt，最多 3 个连续 scene 组成 unit；child prompt 只含冻结 task/role 定位与 SHA。
6. `imagePrompt`（content draft）到 formal generation plan 的 `prompt` 只允许 coordinator 确定性映射；child 不接收完整主对话、完整 SRT、provider 凭据、长日志或批准信息。详见 [references/prompt-writing.md](references/prompt-writing.md) 与 [references/subagent-orchestration.md](references/subagent-orchestration.md)。
7. 每幕只表达一个核心视觉命题；可独立揭示的 2–3 个视觉簇之间保持真实纸面留白，不以道路/河流/山脉/箭头等贯穿结构连接，除非该结构本身不可分割。annotation 按连续墨迹簇划分，最多 3 个且不为凑数强拆。
8. reveal 时间严格串行、不可重叠；空间 region 仅在真实遮挡/交界处适度重叠；`protectedRegions` 只能保护正确分区中不可避免的局部，不能掩盖错误分区。
9. 旁白字幕必须使用可一一对应的 FunASR token 时间戳；句级边界不得用于估算词内时间。正式 runner 固定使用 Paraformer 支持的 30 秒 VAD 上限，保存每个 VAD 子结果的 token/timestamp，并按分段原序重建后与 FunASR 顶层数组逐项核对；只比较全局 cardinality 不算可信证据。字幕只在已确认原稿的标点或原始 cue 边界安全切分，禁止把金额、数字词组、英文词、固定词语或连续汉字从中间截断；真实前导、句间和尾部停顿允许保持无字幕空档。token 证据不足、分段重建不一致、边界位移过大、滑动窗口局部语速异常或断词 QA 失败时必须 fail-closed。scene 边界不得早于本幕最后一个声学 token 结束；真实停顿内只能采用记录了 basis 的边界，禁止统一大延迟。
10. 图片采用 `continue_independent`：单幕失败不阻止其他幕候选，但任一必需 scene 缺失/失败/stale 时 batch 总状态为 FAIL，不得启动全量预览或写批准。完整 TTS 固定为一个 full-track task；不得因 provider/ASR 失败回退成逐句多请求。
11. provider worker 或宿主图片结果导入只能写已登记 attempt 的 candidate/去敏 receipt；coordinator 按 `prepared → requesting → candidate_ready → publishing → validated` 串行 checkpoint、重验、原子发布和清理。`gpt-login` 只替换图片字节来源，不另建 Gate、manifest 或发布链。
12. `sceneRender` 是当前正式单幕候选的有界并行能力；worker 数只读 workspace `execution.concurrency.sceneRender`（缺失继承 pool default，最终默认 1，范围 1–16），候选完成顺序不得改变 generation plan 顺序或正式 manifest。
13. image validation 每张 PNG 同一打开周期只完整解码一次；voice deep validation、timeline、SRT、累计帧、identity、binding 和 approval 仍按合同串行/有界，证据缺失或 bytes 变化不得降级为 binding PASS。
14. 正式成片永远烧录字幕：disabled 为 H.264、0 音频且使用 source SRT；Edge/MiniMax/豆包语音为 H.264 + AAC 旁白且使用 current narration SRT。旁白项目 `backgroundMusic.enabled=true` 时，最终封装在同一路 AAC 中按固定 `-15 dB` 混入内置 CC0 BGM；关闭或旧项目缺字段时保持原旁白封装。旁白模式缺少 current narration SRT/timeline/full approval/identity 必须失败，不能回退 source SRT。
15. 任一输入、旁白文本/分段、scene mapping、imagePrompt、音频/timing/render binding、annotation/reveal、scene 集合/顺序、手部素材 `handSha256`、字幕 preset/字体/SRT 或 clean/final SHA 变化，按 [references/recovery-and-identity.md](references/recovery-and-identity.md) 使受影响 identity 和批准 stale；历史 stale 证据不得作为 current 输入。
16. 先把本 `SKILL.md` 所在目录解析为绝对 `SKILL_ROOT`；脚本路径一律使用 `<SKILL_ROOT>\scripts\...`，不得依赖调用者 cwd。每次进入工作区前先用当前可启动的 Python 运行 `<SKILL_ROOT>\scripts\prepare_env.py --check-workspace-access`，完成真实 create/write/flush/read/delete 预检；随后在任何业务脚本、导入探测或渲染启动前运行同一绝对脚本的 `--check`，读取末行 `ENV_PY=<绝对解释器路径>`。若仅因专用环境或依赖尚未就绪而失败，运行不带 `--check` 的同一绝对脚本完成准备并重新读取 `ENV_PY`。本次任务后续每条 Python 命令都必须直接调用该绝对解释器和绝对脚本路径；不得使用裸 `python`、`py`、shebang 或不会跨工具调用持久化的临时 shell 变量先试跑再回退，也不得把系统 Python 缺少 `cv2` 误报成 OpenCV 未安装。宿主 `CreateProcess rejected by policy` 与 Windows 文件写入拒绝必须分开报告；UI 刚切换权限时在新回合重跑预检，不用复杂 shell 写删命令试探。
17. 旁白项目的 `approve-full` 必须显式带 `--review-policy user_first|agent_first` 并写入 `fullApproval.reviewPolicy`；后续线稿、annotation preview 和 scene review 自动继承且拒绝冲突值。`agentApprovalEnabled=true` 时由项目授权确定性派生 `agent_first`，不得再次询问或接受冲突值；为 `false`/缺失时仍由用户选择，不得静默采用默认策略。
18. 正式 CLI 在进程入口把可重配置的 stdout/stderr 固定为 UTF-8；不得依赖 PowerShell 当前代码页或要求调用者临时设置 `PYTHONUTF8`。测试捕获流等不可重配置对象保持原样。

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

以下 `<SKILL_ROOT>` 必须解析为本文件所在目录的绝对路径；即使当前目录是 `D:\SRTWhiteboard` 也不能省略。其余 references 中的 `scripts/...` 只是排版简写，实际执行同样使用绝对脚本路径。

```powershell
# 环境与输入准备
python <SKILL_ROOT>\scripts\prepare_env.py --check-workspace-access
python <SKILL_ROOT>\scripts\prepare_env.py --check
# 上一步仅因专用环境或依赖缺失而失败时：
python <SKILL_ROOT>\scripts\prepare_env.py
# 捕获末行 ENV_PY；后续每次工具调用都直接使用该绝对路径，不用裸 python/py
# 首次生成整轨旁白前准备/检查当前 skill 自带的本地 ASR：
<ENV_PY> <SKILL_ROOT>\scripts\prepare_env.py --feature narration-asr
<ENV_PY> <SKILL_ROOT>\scripts\prepare_env.py --check --feature narration-asr
<ENV_PY> <SKILL_ROOT>\scripts\prepare_draft_agent_task.py contentDrafting --content-input <input.json> --draft-root <draft-root>
<ENV_PY> <SKILL_ROOT>\scripts\validate_content_draft.py --stdin
<ENV_PY> <SKILL_ROOT>\scripts\prepare_draft_agent_task.py storyboardPlanning --draft-root <draft-root> --source-srt <字幕.srt> --target-sec 30 --min-sec 25 --max-sec 35
<ENV_PY> <SKILL_ROOT>\scripts\parse_srt.py <字幕.srt> --target-sec 30 --min-sec 25 --max-sec 35
<ENV_PY> <SKILL_ROOT>\scripts\create_project.py --name <项目名> --srt <source.srt> --plan <generation-plan.json> --source-input <input.json> --source-manifest <manifest.json> --pending-initial-approval
# coordinator 将用户完整句/编号解析为绑定 current identities 的 selection.json 后：
<ENV_PY> <SKILL_ROOT>\scripts\approve_initial_project.py --project <项目根目录> --selection <selection.json> --configured-image-provider-available [--gpt-login-capable]
<ENV_PY> <SKILL_ROOT>\scripts\create_project.py --resume <项目根目录> --srt <原始字幕.srt>
<ENV_PY> <SKILL_ROOT>\scripts\upgrade_project.py --project <项目根目录> --to-schema 2

# 图片、线稿、语音
<ENV_PY> <SKILL_ROOT>\scripts\voice_provider_config.py status
<ENV_PY> <SKILL_ROOT>\scripts\generate_images.py --project <项目根目录>
<ENV_PY> <SKILL_ROOT>\scripts\generate_images.py --project <项目根目录> --host-results <绝对JSON路径>
<ENV_PY> <SKILL_ROOT>\scripts\validate_generated_images.py --project <项目根目录> --review-policy user_first
<ENV_PY> <SKILL_ROOT>\scripts\validate_generated_images.py --project <项目根目录> --review-policy agent_first
<ENV_PY> <SKILL_ROOT>\scripts\generate_voiceover.py sample --project <项目根目录> --voice <voice> --rate <rate>
<ENV_PY> <SKILL_ROOT>\scripts\generate_voiceover.py approve-sample --project <项目根目录> --identity-hash <SAMPLE_IDENTITY>
<ENV_PY> <SKILL_ROOT>\scripts\generate_voiceover.py full --project <项目根目录>
# CLI 正常执行时会自动调用当前 skill 内部 FunASR runner 并发布对齐结果；调试/恢复也可显式导入 ASR SRT：
<ENV_PY> <SKILL_ROOT>\scripts\generate_voiceover.py publish-alignment --project <项目根目录> --asr-srt <FunASR一-token-一-cue声学字幕.srt>
<ENV_PY> <SKILL_ROOT>\scripts\generate_voiceover.py status --project <项目根目录>
<ENV_PY> <SKILL_ROOT>\scripts\validate_voiceover.py --project <项目根目录> [--force-deep]
<ENV_PY> <SKILL_ROOT>\scripts\generate_voiceover.py approve-full --project <项目根目录> --identity-hash <FULL_IDENTITY> --review-policy <user_first|agent_first> [--duration-decision accept_actual]

# annotation、preview、scene bundle
<ENV_PY> <SKILL_ROOT>\scripts\validate_annotations.py prepare --project <项目根目录> --images-confirmed
<ENV_PY> <SKILL_ROOT>\scripts\validate_annotations.py materialize --project <项目根目录> --candidate-root <candidate-root> --task-id <taskId>
<ENV_PY> <SKILL_ROOT>\scripts\validate_annotations.py validate --project <项目根目录> --candidate-root <candidate-root>
<ENV_PY> <SKILL_ROOT>\scripts\serve_preview.py --ensure --project <项目根目录>
<ENV_PY> <SKILL_ROOT>\scripts\generate_annotation_previews.py --project <项目根目录> --all --review-policy user_first
<ENV_PY> <SKILL_ROOT>\scripts\approve_annotation_review.py --project <项目根目录> --identity-hash <annotationReviewIdentitySha256>
<ENV_PY> <SKILL_ROOT>\scripts\render_stream_whiteboard.py --project <项目根目录> --all --ink-path grid --color-fill contour-wipe
<ENV_PY> <SKILL_ROOT>\scripts\scene_review.py --project <项目根目录> --review-policy user_first
<ENV_PY> <SKILL_ROOT>\scripts\approve_scene_review.py --project <项目根目录> --identity-hash <sceneReviewIdentityHash>

# 连续交付与最终 Gate
<ENV_PY> <SKILL_ROOT>\scripts\merge_scenes.py --project <项目根目录> --inputs <幕1.mp4> <幕2.mp4>
<ENV_PY> <SKILL_ROOT>\scripts\burn_subtitles.py --project <项目根目录>
<ENV_PY> <SKILL_ROOT>\scripts\mux_voiceover.py --project <项目根目录>
<ENV_PY> <SKILL_ROOT>\scripts\validate_final_media.py --project <项目根目录>
<ENV_PY> <SKILL_ROOT>\scripts\run_phase.py --project <项目根目录> --phase final-delivery
<ENV_PY> <SKILL_ROOT>\scripts\coordinator_cli.py project-status --project <项目根目录>
<ENV_PY> <SKILL_ROOT>\scripts\coordinator_cli.py validate-draft-result --task <绝对task.json>
<ENV_PY> <SKILL_ROOT>\scripts\coordinator_cli.py parse-initial-approval --project <项目根目录> --reply <用户完整回复> --output <项目\.work\selection.json> [能力参数]
<ENV_PY> <SKILL_ROOT>\scripts\annotation_dispatch.py observe --manifest <dispatch-manifest.json> --task-id <taskId> --child-running
<ENV_PY> <SKILL_ROOT>\scripts\approve_final_media.py --project <项目根目录> --identity-hash <FINAL_IDENTITY>
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
- `subtitles/final.ass`、字体 hash、样式 hash、权威 SRT 与 contact sheet 写入 delivery evidence；旁白字幕必须通过 VAD 分段 token 重建、顶层逐项一致性、语义切句、断词、滑动窗口局部阅读速度、scene 尾音边界与真实 gap QA。
- disabled final 必须 1 路 H.264、0 音频；旁白 final 必须 1 路 H.264 + 1 路 24kHz mono AAC；两者都有可见烧录字幕。
- 自动测试不调用真实 provider；外网或服务不可用写 `BLOCKED`，不以 fixture PASS、SKIP 或技术 `validated` 冒充外部或质量批准 PASS。
