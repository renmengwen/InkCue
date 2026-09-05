---
name: srt-whiteboard-animation
description: 将主题、正文或 SRT 制作成按叙事顺序流式落墨的手绘视频；当前 warm-paper-stream-v1 的六个视觉模板共享暖米黄纸张画布。支持传统 SRT 的无旁白/Edge TTS/MiniMax/豆包语音路径，以及经一次内容与制作方案联合确认后派生严格 SRT 的 topic/text 路径。用户要求“把主题/正文/SRT 做成白板手绘视频”“按文案分镜画手绘”“生成带字幕或在线旁白的白板动画”时触发。
---

# SRT 白板动画：入口路由

本文件只定义入口路由、不可变边界和按阶段读取的 reference。阶段字段、完整命令、批准句、provider 请求、恢复和验收细节只以对应 reference 为准，不在入口重复。输出为 1920×1080、60fps；当前六个 preset 均兼容 `warm-paper-stream-v1`，因此共享 `#F5EBD7` 暖米黄纸张 surface，但这不是所有未来 renderer/preset 的永久产品定义。具体视觉语言以 generation plan 冻结的 preset `promptRecipe` 为准。面向用户的说明、分镜、配置和界面文字使用中文。

## 1. 输入与新建/恢复路由

| `inputMode` | `rewritePolicy` | `voiceoverMode` 来源 | 业务 reference 的读取时机 |
|---|---|---|---|
| `srt` | 不适用 | 默认读取 active provider；用户明确静音时为 `disabled` | [content-input.md](references/content-input.md) |
| `topic` | 仅 `generate` | 自动读取 active provider | child 派发成功后按需读取 [phase-0-content.md](references/phase-0-content.md)、[content-input.md](references/content-input.md) |
| `text` | `preserve | polish` | 自动读取 active provider | child 派发成功后按需读取 [phase-0-content.md](references/phase-0-content.md)、[content-input.md](references/content-input.md) |

- `topic + preserve/polish`、`text + generate` 非法。非 SRT 必须冻结 15–600 秒的 `targetDurationSeconds`；缺失时可把建议值 60 秒与其他缺失配置一次性展示。
- 用户明确说“新任务”“不要沿用旧任务”时必须新建 `draft-root`，不得根据同名目录或相似内容恢复，也不得使用 `--resume`。只有用户明确指定继续既有项目时才进入恢复路径。
- 除明确静音外，`voiceoverMode` 只能通过 `voice_provider_config.py status` 的脱敏结果冻结为 `edge-tts | minimax | doubao`。不得让用户在三者间选择，不得直接读取或转述 `config/*.local.json`，也不得从旧项目、命令行参数或对话猜 provider。
- 视觉模板采用“AI 推荐默认值 + 用户明确覆盖”。在创建 `contentDrafting` / `storyboardPlanning` attempt 前冻结具体 preset ID；不得把 `auto` 写入任何 artifact。

## 2. topic/text 单次 bootstrap 与短上下文 child

coordinator 先把本文件目录解析为绝对 `SKILL_ROOT`。下列独立环境预检只适用于传统 SRT、兼容或诊断路径；正常 `topic`/`text` 新任务不得在 bootstrap 前运行它们：

```powershell
python <SKILL_ROOT>\scripts\prepare_env.py --check-workspace-access
python <SKILL_ROOT>\scripts\prepare_env.py --check
```

若第二条仅因专用环境或依赖未准备而失败，才运行不带 `--check` 的同一脚本。捕获末行绝对 `ENV_PY` 后，传统 SRT、兼容或诊断路径的后续 Python 命令直接使用 `<ENV_PY> <SKILL_ROOT>\scripts\...`；不得先试裸 `python`、`py` 或依赖临时 shell 变量。只有宿主权限、工作区或解释器环境实际改变时才重新预检。

`contentDrafting`、`storyboardPlanning`、`visualReview`、`annotationDrafting` child 不重复运行 `prepare_env`。child 只读取本短入口、冻结的 `role-contract.md`、`task.json` 及 `task.inputs`；不得主动搜索源码、tests、examples、CLI `--help`、provider 配置、长日志或跨阶段 reference。descriptor 给出的纯本地 candidate lint/validation 命令可以直接执行，coordinator 仍须独立重验。

对正常 `topic`/`text` 新任务，coordinator 在完整读取本文件后，真实派发前只允许一条确定性 bootstrap 链路：解析 `SKILL_ROOT` → 一次 `prepare_env.py --bootstrap-content-draft` → 读取 descriptor 的派发字段 → 立即调用顶层 direct `spawn_agent`。除这条链路外，派发前禁止 memory lookup、分段重读任何 reference、搜索源码/tests/examples、试跑 CLI `--help` 或以 `Test-Path` 探路；不得预先读取 `phase-0-content.md`、`content-input.md`、`prompt-writing.md`、`subagent-orchestration.md`。这些业务 reference 只能在 child 已成功派发后，由 child 或 coordinator 处理 candidate、进入后续阶段时按需读取。bootstrap 不得被 provider status、视觉推荐、手写输入或其他探路步骤拆开。

正常 topic/text 新任务只有一条阶段 0 快路径：上述一次 `prepare_env.py --bootstrap-content-draft` → descriptor `nextAction=spawn_now` 后立即派发。该单次 bootstrap 已完成 workspace-access、环境 check、经脱敏接口冻结 active provider、采用用户明确指定的 preset 或自动推荐并冻结具体 preset ID、生成合法 managed content input、分配不覆盖既有目录的唯一新 `draft-root`，并准备 attempt descriptor。coordinator 不得在此前后再单独运行 provider status、视觉推荐、手写 `content-input.json`、先落 `body-file`、用 `Test-Path` 试探名称，或搜索源码/tests/examples/CLI `--help`；只有用户主动要求浏览模板时才运行 visual style catalog。用户明确要求新任务时 bootstrap 不得恢复或覆盖旧任务。

prepared task 必须直接提供 `agentPrompt`、`candidateValidationArgv`、`resultMaterializeArgv` 和下一步定位信息；task 还必须携带该 role 的 canonical `candidateSchema` 与 `candidateSkeleton`。coordinator 收到后应立即派发，不再自行拼 prompt 或搜索 result/candidate schema；child 只能按冻结 schema/skeleton 生成 candidate/findings，不得猜字段。所有 role 的 `result.json` 均由 coordinator 根据冻结 task 与输出 SHA 确定性生成。

`spawn_agent` 是开发者工具定义中的顶层 collaboration direct tool，必须直接调用；它被有意排除在 `functions.exec` 的 `tools.*` / `ALL_TOOLS` 中。不得因为嵌套工具列表中找不到它就宣称 child unavailable，也不得在真实派发前选择 coordinator fallback。descriptor 为 `nextAction=spawn_now` 后，下一步必须先发起真实的 direct `spawn_agent` 调用；只有该 direct call 实际返回 tool error，才能报告派发失败，并再按当前用户约束决定 `BLOCKED` 或是否允许 fallback。当前用户明确要求“主代理只编排”时，本 Skill 全链路禁止 coordinator 编写或修改 `contentDrafting`、`storyboardPlanning`、`visualReview`、`annotationDrafting` 的生成式 candidate/findings；派发失败只能准确报告，不能伪装为即将派发或由主代理代做。

candidate validator 的一次运行必须返回完整结构错误清单，不得只报第一个字段。首次结构失败时，同一 attempt 只 followup 一次原 child，要求按完整错误清单和冻结 `candidateSchema`/`candidateSkeleton` 做一次全量 schema 归一；仍结构失败就直接换用更强的短上下文 child，不得逐字段“洋葱式”反复补丁。该补正策略不创建新 Gate、状态机或批准语义，也不削弱 current、SHA、stale 与批准边界。

宿主派发 child 时优先选用满足文本/图像/视频能力的最快可用模型和 `medium` effort；按上述一次完整 schema 归一仍失败、复杂实质修订或非结构业务校验失败时才升级更强模型/effort。模型和 effort 是执行策略，不进入 artifact identity。

## 3. 阶段路由

只读取当前正在执行阶段的 reference。跨阶段只消费短摘要、current/stale/approval 状态和 identity；不得提前加载后续 reference，或把完整正文、SRT、prompt、原图、JSON、媒体和长日志回灌主窗口/child prompt。

| 阶段/主题 | 当前阶段唯一规范来源 |
|---|---|
| topic/text 内容、自然口播、pending 预项目、联合批准 | [phase-0-content.md](references/phase-0-content.md)、[content-input.md](references/content-input.md) |
| 传统 SRT、scene 划分、prompt 与视觉拓扑 | [content-input.md](references/content-input.md)、[prompt-writing.md](references/prompt-writing.md) |
| child task/result、模型策略、并发、fallback | [subagent-orchestration.md](references/subagent-orchestration.md) |
| 正式生图、PNG 校验、线稿 review、scene bundle | [image-generation.md](references/image-generation.md) |
| 整轨语音、provider-native 字幕、真实时钟 | [voiceover.md](references/voiceover.md)、[subtitles.md](references/subtitles.md) |
| annotation drafting、preview 与联合批准 | [annotation-drafting-role.md](references/annotation-drafting-role.md)、[subagent-orchestration.md](references/subagent-orchestration.md) |
| annotation-preview / final-delivery runner | [phase-4-runner.md](references/phase-4-runner.md) |
| current、stale、identity、retry、恢复 | [recovery-and-identity.md](references/recovery-and-identity.md) |

执行顺序概括为：阶段 0 联合批准 → 严格 source/正式项目 → current 整轨音频和真实时间轴 → 生图与线稿 Gate → annotation/区域/reveal Gate → scene bundle Gate → merge/burn/mux/final 技术验证 → final Gate。`targetDurationSeconds` 只是内容预算和 provisional SRT；静音以 source SRT 为权威时钟，旁白以获批 current audio timeline 和 narration SRT 为权威时钟。

## 4. 不可变写入、并发与派发边界

1. coordinator 是唯一用户接口和正式 writer；只有它能发布正式 scene/audio/annotation、manifest、timeline、SRT、identity、stale、checkpoint 和批准。child 始终 `formalWritesAllowed:false`、`approvalWritesAllowed:false`。
2. attempt 是 artifact 版本边界，不是执行者边界。首次独立任务使用短上下文 child；同一 role 的修订/执行性补正优先 followup 仍兼容且 idle 的原 child，否则再 spawn。磁盘 task、input 和 SHA 始终高于代理记忆。
3. 只有 coordinator 根据实际宿主 slots/capability 直接调用顶层 spawn/followup/wait。Python 不推断 child slots，不生成宿主调用，不替 coordinator 决定 dispatch；coordinator 也不得从 `functions.exec` 的嵌套工具清单预判 direct collaboration tool 不可用。只有真实 direct call 返回 tool error 后，才按当前用户约束决定失败收口；用户要求主代理只编排时不得 fallback 生成 candidate。effective agent concurrency 取 configured、ready task/unit、可用 child slots 和 coordinator budget 的最小值，并保留 coordinator 槽位。
4. agent pool 与图片/校验/sceneRender worker pool 分离且不相乘；多个独立 ready task/unit 必须先填满安全 effective concurrency。正式发布仍按 generation plan 顺序。
5. annotation 保留“一幕一 task/attempt/candidate/result”，最多 3 个连续 scene 组成一个 unit。同一 child 在 unit 内按序处理：每幕写 candidate 后执行 descriptor 给定的纯本地 lint；PASS 才继续下一幕，FAIL 只补正当前幕。整个 unit 只返回一次，coordinator 随后 batch observe/materialize 并执行完整 current validator。30 秒 tail grace 只用于异常恢复，不进入正常关键路径。
6. `user_first` 不创建额外 visualReview。`agent_first` 的 image/annotation-preview visualReview child 只写 findings，由 coordinator 确定性生成 result 并作出独立 current 决定。scene bundle 若具备视频能力的 coordinator 本来就必须完整观看 current 视频，child 关键帧预审不是强制关键路径；只有需要独立第二意见或 coordinator 需要定位辅助时才派发。

## 5. 全部 fail-closed 的 Gate

| Gate | 必须检查者与 current 证据 | 通过后允许 |
|---|---|---|
| 初始联合批准 | 用户检查 current 草案/字幕分镜和制作方案；旁白项目还须试听 current `SAMPLE_IDENTITY` | 原子冻结 BGM、后续模式、生图方式并提升 pending 项目 |
| 完整旁白 | 人工模式真实试听；自主模式重验严格技术证据并记录用户样音授权后的技术推进 | 使用 canonical audio timeline |
| 线稿 | 当前批准主体实际检查 current 有序全量线稿 | annotation batch |
| annotation 联合审阅 | 当前批准主体检查 annotation、区域、保护区和 reveal | 正式 scene render |
| scene bundle | 当前批准主体实际检查 current 有序视频 | merge、burn、mux、final 技术验证 |
| 最终成片 | 人工模式完整看片听音；自主模式重验 current final 全套技术证据 | 批准 `FINAL_IDENTITY` |

- 阶段 0 的 topic/text candidate 经确定性校验后，coordinator 可先派生 source、创建 `pending_initial_approval` 预项目和 current 样音，再等待用户一次联合批准；pending 项目不得进入完整旁白、生图、annotation、render 或 final。
- `agentApprovalEnabled=false`/缺失保留逐阶段人工 Gate；为 `true` 只允许 full/final 使用“用户样音授权后的技术推进”审计，不能声称 AI 完整听音。视觉 Gate 始终要求有能力的批准主体实际查看 current artifact。
- candidate、`completed`、技术 `validated`、fixture PASS、child findings、无异常摘要、未回复或“没有反对”均不是批准。runner/CLI/child 不自行批准；只有 coordinator 能调用既有批准动作绑定刚检查的 current identity。
- 必须保留 `SAMPLE_IDENTITY`、`FULL_IDENTITY`、`annotationReviewIdentitySha256`、`sceneReviewIdentityHash`、`FINAL_IDENTITY` 及其 current 字节/证据绑定。输入或相关字节变化必须按 recovery reference 传播 stale，旧批准不能复用。
- provider 请求后 candidate/receipt 不完整且无法按同一幂等键查询时为 `unknown_external_outcome`：禁止普通重跑或 `--retry-failed` 自动重发，必须取得用户承担新外部调用的明确授权。新增费用/凭据/服务/版权授权或实质改变已冻结用户意图，也必须单独询问。

## 6. Provider 与媒体底线

- 正式图片请求只消费 current formal generation plan；topic/text 的 `imagePrompt` 由 coordinator 确定性映射为 formal `prompt`。每幕请求彼此独立，失败不阻断其他独立幕，但任一必需幕缺失/失败/stale 时 batch 不得越过 Gate。
- Edge/MiniMax/豆包完整旁白都固定为一个整轨 synthesis task，不因 provider/ASR 失败回退为逐句多请求。Edge 使用本地 FunASR token 证据；MiniMax 与豆包只使用各自同一次合成响应绑定的 provider-native word 字幕，不重复跑 FunASR。
- 豆包固定使用外部模型 `seed-audio-1.0` 的 prompt-only 能力。请求省略整个 `references` 字段，不传 `speaker`、`audio_data` 或 `audio_url`；音色、年龄和表演只由 authored `text_prompt` 定义。新旁白版本在样音前由 coordinator 重新读取 `C:\Users\MOVER\Desktop\seed-audio-1.0 text_prompt 参考.txt`，只把它作为风格/能力示例，结合 current 全文、scene 与 BGM 方向冻结 `schemaVersion: 1, kind: performanceBrief` 的 brief；程序逐字装配正文，并把 provisional scene 时间窗口写成 `[startSeconds:endSeconds]`，明确整轨目标时长。brief、参考 SHA 和最终 prompt SHA 纳入既有 voice identity，恢复或重试复用。`backgroundMusic.enabled=true` 时豆包音乐已嵌入 canonical narration，且只在叙事确有变化处改变，不为每个 scene 强制补一句；final mux 不得再混内置曲。Edge/MiniMax 才按既有固定混音 recipe 混入 CC0 BGM。
- 正式 final 永远烧录字幕：静音为 H.264/0 音频并用 source SRT；旁白为 H.264 + 24kHz mono AAC 并用 current narration SRT。完整解码、流、尺寸、fps、帧数/时长/尾部、字体/字幕、BGM 模式和 identity 必须 current。
- 自动测试、fixture、技术检查或 child 结果不得冒充真实 provider、真实媒体或主观质量 PASS；外部服务或宿主媒体能力不足时准确报告 `BLOCKED`/`FAIL`。

## 7. 最短命令入口

完整逐步命令只查当前阶段 reference。正常路径优先使用以下入口，不用 `--help` 探路：

```powershell
# 诊断/传统 SRT 的脱敏状态与恢复定位；正常 topic/text 快路径不单独调用 status
<ENV_PY> <SKILL_ROOT>\scripts\voice_provider_config.py status
<ENV_PY> <SKILL_ROOT>\scripts\coordinator_cli.py project-status --project <项目根目录>

# topic/text 新任务的唯一派发前命令；stdout 紧凑 descriptor 为 spawn_now 时立即派发
python <SKILL_ROOT>\scripts\prepare_env.py --bootstrap-content-draft --workspace <workspace-root> --new-draft-label <label> (--topic <text> | --body <正文>) --rewrite-policy <generate|preserve|polish> --target-sec <15..600> [--visual-style-preset <具体ID>]

# 传统 SRT 分镜 task；stdout 的 preparedTask 可直接派发
<ENV_PY> <SKILL_ROOT>\scripts\prepare_draft_agent_task.py storyboardPlanning --draft-root <draft-root> --source-srt <字幕.srt> --target-sec <秒> --min-sec <秒> --max-sec <秒>

# 已覆盖的连续确定性阶段
<ENV_PY> <SKILL_ROOT>\scripts\run_phase.py --project <项目根目录> --phase annotation-preview
<ENV_PY> <SKILL_ROOT>\scripts\run_phase.py --project <项目根目录> --phase final-delivery
```

每次阶段结束只返回短摘要：`PASS | FAIL | BLOCKED | SKIP | 待确认`、真实原因、artifact 路径、current/stale/approval、identity、实际并发/派发模式、失败 scene/unit 和下一条安全动作。不得重新嵌入完整正文、prompt、图片、JSON、媒体或 validator 长输出。

## 8. 质量底线

- 首帧为所选模板与 renderer 冻结的干净画布（当前六模板均为 `warm-paper-stream-v1` 的 `#F5EBD7` 暖米黄纸张 surface），未开始区域完全隐藏；reveal 使用本幕局部时钟、严格串行、不越过 scene，末尾至少保留 0.5 秒。
- generation plan、manifest、图片 SHA/尺寸、annotation/timing/render binding、手部素材、scene bundle 批准、字幕和 final evidence 均须 current。
- 图片不全局禁字：新计划默认 `constraints.forbidText=false`；只检查文字是否清晰、正确、符合语义且无乱码、意外内容或水印。显式 `true` 才禁字。
- `protectedRegions` 只能保护正确分区中的局部，不能掩盖错误分区；连续不可分割墨迹合并，可独立揭示簇保持纸面留白，首版最多 3 个。
- `@moveR` 版权层未经明确授权不得修改或移除。
