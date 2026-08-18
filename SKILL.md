---
name: srt-whiteboard-animation
description: 将主题、正文或 SRT 制作成暖米黄纸张底、按叙事顺序流式落墨的白板手绘视频；支持传统 SRT 的无旁白/Edge TTS 路径，以及经一次内容与制作方案联合确认后派生严格 SRT 的 topic/text Edge TTS 路径。流程包含严格 SRT、分镜、完整旁白与真实时长确认、统一线稿、标注/区域/时序联合审阅、有界并行逐幕候选渲染与场景 bundle 联合审阅、正式字幕烧录、媒体验证和 identity 绑定的人工关卡。当用户要求“把主题/正文/SRT 做成白板手绘视频”“按文案分镜画手绘”“生成带字幕或 Edge TTS 旁白的白板动画”时触发。
---

# SRT 白板动画（mask 编排 + stream 画法）

把主题、正文或 SRT 转成 1920×1080、60fps 的白板手绘成片。外部输入模式固定为 `inputMode = srt | topic | text`；topic/text 先冻结最小输入，在允许真实派发时由 `contentDrafting` child 生成完整旁白稿、cue、scene 与画面建议候选，coordinator 只负责校验、生成审阅 artifact、接收修改和正式单写；宿主条件不足时由 coordinator fallback，并遵守同一 attempt 合同。topic/text 必须以一次“内容与制作方案联合确认”同时冻结内容、target、rewritePolicy、voiceoverMode、cue→scene、分镜与图片提示词；确认前不得运行 `prepare_source.py`，也不得创建正式项目，确认后确定性派生与建项不再重复询问相同策略。编排层按字幕叙事顺序逐区域揭示，未开始区域完全隐藏；`protectedRegions` 只保护正确分区中不可避免的局部重叠，不能掩盖错误分区。绘制层让笔尖在每个区域的允许掩码内连续落墨，先 `ink` 铺线，再 `color` 添彩。所有面向用户的说明、分镜、配置和界面文字必须使用中文。

面向用户描述失败、拒绝或 fallback 时必须使用真实原因，例如“输出目录不是完整 source 准备包，拒绝覆盖”“宿主缺少 child slot”或“coordinator 缺少 viewImage 能力”。不得把目录防覆盖、路径校验、candidate/正式文件分开、role 写入边界或普通 fallback 统称为“隔离”“隔离保护”或“安全保护”。

首版输入与配音组合固定为：

| inputMode | rewritePolicy | voiceoverMode |
|---|---|---|
| `srt` | 不适用 | `disabled | edge-tts` |
| `topic` | 仅 `generate` | 仅 `edge-tts` |
| `text` | `preserve | polish` | 仅 `edge-tts` |

`topic + preserve/polish`、`text + generate` 均为非法组合。非 SRT 输入必须有 15–600 秒的 `targetDurationSeconds`；未提供时 Codex 可以建议 60 秒，但必须在生成草案前与其他缺失配置一次性展示并等待用户确认。用户已经明确给出的 target、rewritePolicy 或 voiceoverMode 直接沿用，不得为相同具体值重复提问。该 target 只用于内容预算与 provisional source SRT，Edge 获批后的真实 audio timeline 才是权威时钟。

## 视觉分幕、构图与标注首版合同

- 分幕以视觉状态是否发生实质变化为依据；出现新的状态、因果阶段、构图中心或需要独立呈现的结果时可以增加 scene，不设固定 scene 数量，也不按名词数量机械切幕。每幕只表达一个核心视觉命题。
- 每幕默认只有一个主要视觉簇；叙事确有需要时可以增加一个空间独立的辅助视觉簇。视觉簇之间必须有真实、连续的干净纸面留白，不得互相嵌套、遮挡，也不得由跨簇的连续背景、共同底面、长线或其他贯穿性结构连接。多个概念若在视觉上必须形成连续构图，必须合并为同一个视觉簇并整体揭示。
- 上述约束描述的是通用空间关系，不得把某类场景对象写成固定提示词或固定分类。`imagePrompt` 仍须按每幕实际语义具体描述画面，但不能依赖硬编码对象清单来满足分区要求。
- annotation 按视觉上连续、应当一起落墨的墨迹簇划分，不按字幕中的叙事名词逐项拆框。一幕允许只有 1 个元素，默认 1–2 个，最多 3 个；第 2 或第 3 个元素只在各墨迹簇空间独立时成立。
- region 之间不得嵌套、交叉，不得横穿其他 region 的有效墨迹；同一连续主体、共享背景或连接结构必须归入同一 region。`protectedRegions` 只处理正确分区后仍不可避免的局部保护，禁止用它遮盖本应合并的连续墨迹或错误 region。

这些是现有 scene、`imagePrompt` 与 annotation `elements` 的编写约束，不新增 schema 字段，不改变人工关卡、时序合同、允许掩码公式或正式渲染算法。

正式项目必须显式选择：

```text
voiceoverMode = disabled | edge-tts
```

无论采用哪种模式，正式 `output/final.mp4` 都必须有可见的烧录字幕：

| 模式 | 权威时钟 | 权威字幕 | 正式成片 |
|---|---|---|---|
| `disabled` | 原始 SRT 全局时间轴 | `source/source.srt` | H.264 + 烧录字幕，0 音频流 |
| `edge-tts` | 已批准的 canonical audio timeline | `audio/narration.srt` | H.264 + 烧录字幕 + AAC 旁白 |

Edge 模式缺少 current narration SRT、timeline、完整旁白批准或 identity 绑定时必须失败，不能回退到 source SRT。Disabled 模式即使项目里残留 narration SRT，也只能使用 source SRT。

## 强制人工确认原则

工作流中的人工关卡必须逐项获得用户明确确认。不得把未回复、此前的笼统授权、技术校验通过或“用户没有反对”当作批准；不得由 CLI 或代理自行批准。用户要求修改上一步时，只重做受影响步骤，并重新经过对应关卡。

其中五类批准会持久化为 identity 绑定状态：

- 样音批准：绑定 current sample identity，确认 voice/rate；未批准时完整旁白以退出码 5 拒绝。
- 完整旁白与真实时长批准：Edge `full` 只生成并技术校验 current WAV、timeline 与 narration SRT，不编码无画面的预审视频。用户必须完整试听 `audio/narration.wav`，同时查看真实时长差值；`approve-full` 绑定 current `FULL_IDENTITY`，该 identity 已覆盖 WAV/timeline/narration SRT。偏差超过 10% 时还必须显式 `accept_actual`。
- 标注联合审阅批准：全量 annotation 技术 current 后可直接生成本地区域预览；`generate_annotation_previews.py` 调用 `annotation_review.py` 写入/验证 current review manifest，并在摘要输出 `annotationReviewIdentitySha256`。用户一次确认标注内容、区域预览、`protectedRegions` 与 reveal 时序后，`approve_annotation_review.py` 只批准该 current identity，并绑定有序 annotation/preview bundle、timing plan、render profile 与 Edge 按需音频证据。
- 场景联合审阅批准：正式 batch 按 `sceneRender` 有界并行生成并技术检查彼此独立的单幕候选，coordinator 仍按 generation plan 顺序单写发布；全部 current scene 组成有序 review bundle 后，`scene_review.py` 输出 `sceneReviewIdentityHash`，用户一次确认全部场景或指出拒绝的 scene。`approve_scene_review.py` 只批准该 current identity，并把 `sceneReviewApproval` 写入 `manifests/render-manifest.json` 顶层；合并前必须硬校验该批准。
- 最终成片批准：技术验证之后，用户完整看片；Edge 模式还要完整听音。`approve_final_media.py` 只批准 current final identity。

线稿仍是独立聊天关卡；标注内容、区域预览、重叠保护与 reveal 时序合并为一次 identity 绑定的联合关卡；全部正式单幕合并为一次有序 scene review bundle 关卡。它们不能被 manifest 的技术 `validated` 状态替代。尤其是：**current scene review approval 通过后才能进入合并、字幕烧录和封装链路。** `final-video-only.mp4` 只是内部技术工件，不设独立人工确认；正常链路不得在生成 clean master 后停下询问用户。样音、完整旁白与真实时长、线稿和最终成片仍为彼此独立的关卡；`unknown_external_outcome` 后是否重新调用 provider 仍须在异常发生时取得单独授权，不能并入任何批量批准。

默认不得为了查看图片、试听、读写 JSON、字幕烧录或最终验证而启动浏览器、文件选择框或电脑控制。图片用本地图片查看能力检查；音频直接播放本地文件；JSON 用文件工具；字幕和媒体用命令行。`assets/preview.html` 只用于 current annotation 的用户预览或用户明确要求的手工拖拽；当全量 AI annotation 已发布且技术 current 时，必须按下述“项目预览链接交付合同”启动/复用本地预览服务并把具体项目 URL 交给用户，不能只在代码层声明已支持。

## Coordinator/Subagent 编排入口

Phase 1A 的配置、task/result schema、路径逃逸、attempt 冻结 role contract、runtime decision、fake scheduler 与 coordinator fallback Gate 必须先通过；fake scheduler 只验证协议，不算真实 dispatch。完整合同见 [references/subagent-orchestration.md](references/subagent-orchestration.md)。

- 主 agent 是唯一 coordinator、用户接口和正式写者；只有 coordinator 可以发布正式候选并写 manifest、timeline、SRT、identity、stale、checkpoint 与批准。child 只能写当前 attempt 的 candidate/result/findings/log，task 必须冻结 `formalWritesAllowed:false` 与 `approvalWritesAllowed:false`。
- coordinator 按 ready task、宿主 child slots、资源预算与 role capability 决定真实派发；条件不足时由具备对应能力的 coordinator fallback。审计只记录 configured/effective/peak/task count、`dispatchAllowed`、adapter、mode 与 reason。
- 真实派发必须由 coordinator 调用宿主 `spawn_agent`、`followup` 与等待机制。`contentDrafting`、`storyboardPlanning` 与 global `visualReview` 的全局单 task 使用新鲜短上下文 child；`annotationDrafting` 保持一幕一个独立 task/attempt/candidate/result，并把按 plan 连续的最多 3 个 task 冻结成一个 dispatch unit，由同一 child 顺序处理。prompt 只传冻结 task/role 的绝对路径、SHA、允许的 attempt 目录和固定返回格式。
- role allowlist 为 `contentDrafting | storyboardPlanning | visualReview | annotationDrafting`。topic/text 从阶段 0 起由 `contentDrafting` 读取冻结输入并写 candidate；传统 SRT 分镜、线稿 review、逐幕 annotation 同样按 task/result 文件交接。
- effective agent concurrency 取 configured、ready task 数、宿主已换算 child slots 与 coordinator 资源预算的最小值；保留 coordinator 槽位且总槽到 child 槽只换算一次。多个 ready task 必须先并行 spawn，再统一等待，不能 spawn 一个就 wait 一个。
- `execution.agents` 与 worker concurrency 分离且不做乘法；agent task 不得再启动 provider、FFmpeg、深验或其他受 worker 并发控制的批处理。visual role 的实际执行者必须具备图片查看能力，否则按能力 fallback 或报告 `BLOCKED`。
- allowedOutputs、SHA 和 pre/post inventory 只用于协议校验与事后侦测。candidate、`completed` 和 validator PASS 都不等于 current 或人工批准；人工关卡、identity、stale、恢复和 coordinator 单写顺序保持不变。

### Phase 2/3 Worker 并发与提交合同

图片、语音与正式单幕渲染 worker pool 只从共享 workspace JSON loader 读取 `execution.concurrency`：各 stage 缺失时继承该 pool 的 `default`，整个 pool 或 `default` 缺失时为 `1`；所有 worker 配置都必须是 `1–16` 的整数。并发配置只改变本机执行策略，不进入作品 identity；`sceneRender` 是正式单幕候选的有界 worker 数。每次运行在 manifest/摘要记录 configured/effective/peak concurrency 与 task count，正式 manifest、timeline、SRT 和场景发布始终按 generation plan 或 unit index 的冻结顺序提交，不按完成时间提交。

- 图片和 TTS provider worker 都只能在 coordinator 预登记的 attempt 中产生 candidate 与去敏 receipt，不能写正式 `scenes/*.png`、`audio/segments/*.wav`、共享 manifest、identity 或批准。coordinator 是唯一正式 writer，按 `prepared → requesting → candidate_ready → publishing → validated` 串行 checkpoint、重验 candidate、原子发布并在 validated 落盘后清理。
- 图片场景彼此独立，采用 `continue_independent`：单幕明确失败不阻止其他幕完成；TTS unit 采用滚动有界的 `stop_dispatch`：首个 provider、取消、规范化失败或不确定外部结果后停止派发新 unit，已在途且形成合法 candidate 的 unit 仍由 coordinator 收尾发布。
- `requesting` 后若 candidate/receipt 不完整且 provider 不能按同一幂等键查询，状态必须为 `unknown_external_outcome`；图片和 TTS 都不得通过普通重跑或 `--retry-failed` 自动重复外部请求，必须等待用户决定是否承担新的外部调用。
- 图片消费验证按 `imageValidation` 并发，但每张 PNG 只在同一个打开周期进行一次完整解码，再检查格式、模式、尺寸、截断/CRC 与 SHA。语音显式深验可按 `voiceValidation` 并发验证相互独立的 segment WAV；timeline、SRT、累计帧、full identity、binding 和 approval 始终串行。
- 新 WAV、缺少 current 技术证据、验证器合同变化或显式 `--force-deep` 时走 deep（含 ffprobe/full decode）；普通 validate、批准和下游在 current SHA/bytes 与 validator receipt 完全匹配时只走 binding，不重复深验相同字节。binding 不得在证据缺失、字节变化、旧 validator receipt 或 stale approval 时降级通过。

上述自动化只证明 fixture、candidate、binding 或技术媒体合同；真实图片 provider、真实 Edge 服务和人工视觉/声音判断仍须单列 `PASS | FAIL | BLOCKED | SKIP | 待确认`，不得用 fake provider、技术 `validated`、visualReview findings 或 fixture PASS 冒充外部验收或人工批准。

### Phase 4 Annotation 批量合同

线稿获得用户明确确认后，coordinator 先调用 `build_formal_validation_context(project)`，把 timing plan、render profile、active timeline、按需 audio/full approval 和 voice manifest 的 current evidence 冻结为只读 `FormalValidationContext`；一次 batch 只深验这套全局 evidence 一次。随后必须使用 `resolve_formal_scenes(project, scene_ids, context=...)` 复用同一 context；`resolve_formal_scene()` 只保留为兼容单幕包装，不能在逐幕循环中反复深验相同 audio evidence。batch 结束和正式发布前还要只做 current SHA/binding 复核，任一全局 binding 变化都使尚未发布候选 stale。

- 每个 ready scene 对应唯一 `annotationDrafting` attempt。task 只引用该幕图片、最小 `scene-brief.json`、冻结 role contract 和全局 binding 摘要；child 的 `candidate.annotation.json` 只负责 `elements` 视觉判断，兼容 legacy 完整 candidate 时也只采纳其 `elements`。sceneId、canvas、sceneDurationMs、timing/render/timeline SHA、frame range 与 timingSource 全部由 coordinator 从 current evidence 确定性 materialize 到独立候选，再通过 validator；child 手抄的 envelope 字段不得进入正式 annotation。输出仍只允许 attempt 内 authored candidate、`result.json` 和可选 `agent.log`。
- annotation 按 `dispatchUnits` 有界并行真实 spawn，每个 unit 最多包含 3 个按 plan 连续的 ready scene；宿主能力或预算不足时由具备图片查看能力的 coordinator fallback。两条路径都必须保留逐幕 task/result、coordinator 单写、validator 与 current binding 复核。
- coordinator 是正式 annotation 和批准的唯一写者。候选可按 `annotationValidation` 有界并发校验，但正式 `.annotation.json` 必须按 generation plan 顺序逐幕、单文件原子发布；失败、取消或 stale scene 不覆盖旧 current annotation，完成乱序不得改变发布或摘要顺序。
- 单幕发布只表示 `published_current_technical`。任一必需 scene 失败、缺失或 stale 时，batch 总状态仍为 `FAIL`；已有发布时记录 `partialSuccess:true`，不得启动全量区域预览，也不得写任何批准。只有全部必需 scene 都是 current 且 validator PASS，coordinator 才执行 `serve_preview.py --ensure --project <项目根目录>`、核验 `PREVIEW_READY=PASS` 与 ready scene 数，并直接进入本地区域预览生成；技术 current 不需要先取得一次聊天确认。

### 项目预览链接交付合同

全量 AI annotation 完成后的用户交付必须同时满足以下条件：

- coordinator 必须运行 `<ENV_PY> scripts/serve_preview.py --ensure --project <项目根目录>`，由命令后台启动或复用只监听 `127.0.0.1:8765` 的本地服务。不得只拼一个未经验证的 URL，也不得要求用户再选择 `scenes` 文件夹。
- 命令必须深验全量 formal annotation 的 current timing/render/audio binding，并返回 `PREVIEW_READY=PASS`、正确的 `PROJECT_ID`、`READY_SCENES=<全部>/<全部>`、`CURRENT_SCENES=<全部>/<全部>` 与 `PREVIEW_URL`；coordinator 还必须确认项目 API 可访问后，才能向用户报告“预览已就绪”。
- 面向用户的回复必须包含命令返回的完整、可点击 `PREVIEW_URL`，并明确说明“打开后自动载入当前项目、无需手动导入”；只报告代码已修改、服务已实现、端口号、项目目录或泛化地址均不算完成交付。
- URL 只允许携带 current `projectId`、可选 `sceneId`、模式和 fragment 编辑令牌；不得携带盘符绝对路径、任意 `folder` 参数或目录穿越片段。服务端必须从已校验的 `project.json`、generation plan 和 `paths.scenes` 解析文件。
- 默认 `edit` 链接的 annotation 保存必须经过本地令牌、current timing/render/audio binding 校验与原子写入；失败不得覆盖原文件。保存只表示 `saved_current_technical`，必须使此前聊天确认重新判定，不得写任何批准。
- 打开链接、播放预览、保存技术 current annotation 或用户没有反对，都不构成人工确认。用户仍须回到聊天一次明确确认 current 标注内容、区域预览、`protectedRegions` 与 reveal 时序；保存修改后只重新生成受影响 scene 的预览，并使当前 annotation review approval stale，未变化且 binding 仍 current 的预览可复用。
- 若服务不能启动、端口被其他服务占用、项目 API 验证失败或 ready scene 不完整，必须报告 `BLOCKED`/`FAIL` 和真实原因；可以同时提供手动选择 `scenes` 的兜底，但不得把兜底说成项目 URL 已交付。

### Phase 5 区域预览与标注联合批准合同

全量 annotation 技术 current 后，coordinator 无需先取得人工确认即可调用 `generate_annotation_previews.py --project <项目根目录> --all`；这是本地确定性派生，不调用图片 provider，也不写人工批准。成功路径由其调用 `annotation_review.py` 写入/验证 `manifests/annotation-review-manifest.json`，摘要输出 `annotationReviewIdentitySha256`。coordinator 必须把标注内容、项目预览 URL、有序 contact sheet、必要的全分辨率预览、`protectedRegions` 与 reveal 时序作为同一 review bundle 交付，等待一次联合确认。只有收到用户对该 current bundle 的明确确认后，才能以 `approve_annotation_review.py` 把批准写入 `manifests/annotation-review-approval.json`；CLI 不得读取、推断或伪造聊天批准。下一关固定为 `annotation_review_confirmation`。

- `--all` 首先强制 Phase 4 全量技术 Gate：generation plan 中每个必需 scene 的 annotation 都必须存在、current 且 validator PASS。一次运行只构建一个 `FormalValidationContext` 并供全部 scene 复用，不按幕重复深验全局 timing/audio evidence。
- 预览 worker 数只读取 `execution.concurrency.annotationPreview`；缺失时按 worker pool 默认规则回落到 `1`。每幕只在本次 `.work/annotation-preview-<run-id>/` 写唯一 candidate，使用 `compress_level=1, optimize=False` 保存无损 PNG；候选必须重新完整打开并验证 PNG、RGB、1920×1080 后，coordinator 才按 generation plan 顺序逐幕、单文件原子发布。
- 任一候选失败都不得覆盖该幕旧 preview；其他独立幕可以发布，但 batch 总状态仍为 `FAIL` 并如实记录 `partialSuccess`。摘要必须记录 configured/effective/peak worker、task count 和 plan 顺序发布结果，路径错误要去敏，且不得写任何批准。修改 annotation 时只重做受影响 scene 的 preview；未变化且 current binding 相同的 scene 可复用。
- 全部幕成功后生成有序 `previews/annotation-preview-contact-sheet.png`，每个缩略图标明 scene ID、名称、元素数和时长。contact sheet 只用于快速总览，不能替代逐张查看必要的全分辨率 preview。
- 成功结果仍必须是 `userConfirmationRequired:true`、`approvalWritten:false`。coordinator 完整展示联合 review bundle 后必须停止；技术 `PASS`、图片已发布、页面已打开或用户未反对都不构成人工批准。联合批准 identity 至少绑定 generation plan 顺序、每幕 annotation SHA、每幕 preview SHA、timing plan SHA、render profile SHA、联合预览合同，以及 Edge 模式下 current audio/timeline/full approval evidence；任一绑定变化都会使旧批准 stale。

### Phase 7 正式渲染单次编码合同

正式单幕沿用现有绘制算法和 `write(frame)` 接口，但编码链固定为“Python/OpenCV 逐帧生成 BGR24 → FFmpeg stdin `rawvideo` → libx264 `medium`/CRF18 → H.264/yuv420p candidate”。正式路径不得调用 `cv2.VideoWriter`，不得生成 MP4V 中间文件，也不得再做一次解码转码；1920×1080、60fps、累计帧边界、0 音频流和首帧/遮罩/尾部合同保持不变。

- Windows 下必须由专门线程并发 drain FFmpeg stderr，避免 pipe 填满阻塞 stdin；错误只报告长度受限、路径与凭据已去敏的 stderr tail，不泄露正式目录、URL 或秘密。
- FFmpeg 提前退出、`BrokenPipe`、stdin/磁盘写入错误、非零退出、关闭超时、stderr drain 未完成、欠写或超写权威帧数都必须 fail closed：立即停止帧生成，删除或保留本次 run candidate 供既有证据规则处理，绝不覆盖旧正式 scene，也不得写成功 manifest/identity。
- 新 candidate 只执行一次 Phase 6 统计型 deep validation，receipt 必须绑定 current SHA/bytes、decoded frame count、streams 与验证器合同；原子发布后只用同一 receipt 做正式路径 SHA/bytes binding，不重复 full decode。binding 失败时按恢复合同保住或恢复旧正式 scene。
- 正式 batch 一次复用 project、annotation approval Gate、`FormalValidationContext` 与手部素材，按 `sceneRender` 有界并行生成彼此独立的单幕 candidate 并逐幕 deep validation；effective 为 configured 与 ready task 数的最小值，peak 记录实际同时运行数。worker 不得写正式 `scenes/*.mp4`、共享 manifest、identity 或批准；coordinator 复核 current binding 后按 generation plan 顺序原子发布并单写 manifest。单幕失败不取消其他已在途独立幕，失败候选不覆盖旧正式 scene；有成功发布但任一必需幕失败、缺失或 stale 时，batch 仍为 `FAIL` 且记录 `partialSuccess:true`。全部必需 scene current 后运行 `scene_review.py`，按冻结顺序形成 review bundle 并输出 `sceneReviewIdentityHash`；用户一次确认全部场景或指出不通过的 scene。修改时只用 `--scene-ids` 重做受影响 scene，再重建 current bundle。`approve_scene_review.py` 把批准持久化到 `manifests/render-manifest.json.sceneReviewApproval`；bundle 绑定 generation plan SHA、sceneOrder、timing plan file/SHA/activeTimeline、render profile SHA，以及逐幕 render identity、MP4 SHA/bytes/frameRange。`merge_scenes.py` 必须在任何 concat 写入前硬校验 current scene review approval，缺失、stale、scene 集合或输入顺序不匹配时以退出码 5 拒绝；该 Gate 已返回本次 merge 可复用的逐幕媒体 binding，merge 不得立即重复验证相同输入。批准后连续完成技术合并、字幕烧录和按需音频封装，不单独确认 clean master。自动 fixture 或技术媒体 PASS 不代表真实外部能力、真实单幕观看或人工验收 PASS。

### Phase 9 正式字幕编码合同

只从共享 loader 读取 `execution.videoEncoding.subtitlePreset`；缺失时使用 `medium` 且不改写本地配置，只允许 `medium | fast | veryfast`，CLI 无临时覆盖。完整配置、identity 和恢复合同见 [references/subtitles.md](references/subtitles.md)。

- 仅用软件 `libx264`，保持 CRF18、yuv420p、fps passthrough、0 音频和累计帧边界；NVENC/QSV/AMF 未实施并记为 `SKIP`。
- preset 进入 `subtitle-burn-v2`、manifest、technical receipt 与 subtitle/captioned/Disabled/Edge final identity；它会改变正式字节，不像 concurrency 那样排除在 identity 外。
- 相同 preset、current SHA/bytes/receipt 只走 binding；preset 变化重建字幕与 downstream final，并清空 `finalApproval`。新 candidate 只 deep 一次，失败不覆盖旧正式输出。
- `sceneRender` 的 configured/effective/peak 仅记录本机执行策略，不进入字幕、scene、clean master 或 final identity；clean master 仍须技术验证但不设人工 Gate，contact sheet 检查、完整看片/听音和最终明确确认保持不变。

## 阶段 0–10 权威工作流

### 阶段 0：输入、旁白稿与制作方案联合确认

1. 接受 `srt | topic | text`；SRT 直接进入阶段 1。
2. topic/text 由 coordinator 冻结主题/正文、rewritePolicy、target、voiceoverMode 与最小上下文。用户已经明确给出的具体值直接沿用；只有缺失字段才在生成草案前汇总为一次配置确认，禁止逐字段询问或在后续重复确认。随后按 runtime decision 创建 `contentDrafting` attempt；宿主条件满足时真实 spawn 新鲜 child 读取 task/input 文件并生成 `candidate.content-draft.json`，否则由 coordinator fallback；两条路径使用同一 `whiteboard-content-draft-v1` 合同。生成或润色 `narrationCues[].text` 时必须遵守以下精简旁白合同：
   - 旁白写给人耳，不写成文章摘要；使用自然、简洁的现代中文，像熟悉主题的人面对镜头解释，优先使用清楚的主语、动词和可见对象。
   - `text + preserve` 不做风格重写，只规范换行、拆 cue 和极少量不改变语义的口语标点；`text + polish` 在完整保留事实、数字、人物、结论、不确定性、因果强度和责任主体后局部润色；`topic + generate` 从第一稿直接按本合同创作，不确定事实不得写成已核验事实。
   - 每个 cue 只推进一个新信息，允许句长和节奏自然变化。避免书面腔、宣传腔、抽象拔高、助手路线词、无信息总结、无目的结尾提问，以及连续堆叠“不是……而是……”“真正……的是……”“首先／其次／最后”等模板句壳；逻辑确有需要时可以保留，不按单个词机械封禁。
   - `polish/generate` 口播不得出现暴露加工过程或素材来源的元话语，例如“原文认为／原文提到／按照原文”“在这段叙述里”“这份材料说明／无法回答”“正文中”等。需要保留归因、限定或不确定性时，直接写具体主体、条件和判断边界，例如“单伟建判断……”“当时审核已经收紧”“目前无法下结论”，不得把“原文／材料／叙述”当作主语或证据替身。只有用户的主题本身就是文本解读、作品分析、原文对比，或用户明确要求讨论素材来源时例外。
   - 不得为了“像人”添加口头禅、错别字、虚构经历、未经证实的例子或多余情绪，也不得刻意堆砌“其实吧”“你知道吗”“说白了”等假口语。
   - 生成审阅 artifact 前做两遍回读：先核对信息、target、cue 与 scene，再完整朗读旁白，只局部修正重复、拗口、抽象堆叠和过度整齐的句子。该回读只处理旁白，不得把 `coreIdea`、`visualSubject` 或 `imagePrompt` “口语化”。
   - 每幕图片都由彼此独立的 provider 请求生成；每个 `imagePrompt` 必须自包含地重复画布、纸张、线稿、配色、按该幕实际语义确定的造型锚点、构图、留白和禁字/禁水印要求。提示词必须落实“一个核心视觉命题、默认一个主要视觉簇、必要时一个空间独立辅助簇、簇间真实纸面留白”的视觉拓扑；禁止簇间嵌套、遮挡和贯穿性连续结构，必须连续构图的概念合并为一个簇。不得使用“延续”“沿用”“同上”“上一幕”“参照前图”等跨请求指代，也不得因运行时会拼接 `globalPrompt` 而省略单幕成立所需的视觉约束。
3. coordinator 收到的普通消息只能包含 result 路径、status、validator 状态和精简摘要；完整草案留在 attempt。把 candidate 的 UTF-8 JSON 送入 `validate_content_draft.py --stdin`，或调用同一纯函数只读校验；只有退出码为 0、`valid:true` 且 `writesPerformed:false` 才可继续。`candidate.content-draft.json` 是阶段 0 唯一机器权威源；未确认 candidate 只能作为 attempt 证据存在，不得写入 source 准备包或正式项目，`--draft` 仅限已获用户确认的输入或测试 fixture。
4. 校验通过后，由 coordinator 从 candidate 确定性生成 `drafts/<draft-id>/reviews/content-review-<contentDraftIdentity前12位>.md`。Markdown 是绑定完整 identity 的只读审阅视图，不是第二份机器权威源；child 不得直接写它或任何正式路径。主窗口只交付可点击文件链接、完整 identity、cue/scene 计数和短摘要，不把完整 Markdown 读回工具输出或粘贴进聊天。
5. 用户要求实质修改时，coordinator 把意见冻结为绑定 current `baseContentDraftIdentitySha256` 的 revision request，创建新 attempt 并 spawn 新鲜 `contentDrafting` child；新 candidate 重新校验并生成新 identity、新 Markdown，旧 candidate/review 保留为历史证据且判为 stale。只有同一冻结 attempt 的缺 result、schema 或漏项等执行性补正才可 followup 原 child；改文案、cue、scene 或图片提示词不是 followup。
6. **停止，等待用户明确确认 current `contentDraftIdentitySha256`，完成“内容与制作方案联合确认”。** 确认前不得执行脚本批准、`prepare_source.py`、把 attempt candidate 发布为正式草案或正式建项；只读技术校验和审阅 Markdown 都不构成人工批准。这一次确认同时覆盖内容草案与后续模式/语义分镜策略，不得把相同信息拆成第二次确认。
7. 明确确认 current identity 后才由 coordinator 单写持久化确认输入，并用 `prepare_source.py --draft --output-dir` 原子生成 `input.json`、`source.srt`、`generation-plan.json` 和 `manifest.json`；child 不得执行正式发布，脚本只做确定性校验与派生。
8. 准备包通过校验后进入阶段 1 的确定性 round-trip；只要派生结果与刚确认的方案一致，就直接进入阶段 2 并以成对的 `--source-input/--source-manifest` 创建正式 Edge 项目，不再询问相同策略。若派生结果发生实质差异，则返回本阶段生成新审阅 artifact 并重新联合确认。

旁白生成与润色只能发生在本阶段、内容与制作方案联合确认之前。确认后的 TTS、字幕和正式成片阶段只消费已确认文本并处理分段、排时、换行和媒体，不得再次“去 AI 味”或改写旁白；需要改稿时必须回到阶段 0，更新草案并重新经过对应人工关卡。完整合同见 [references/content-input.md](references/content-input.md)。

### 阶段 1：严格 SRT 与分支 Gate

1. 用共享 parser 严格读取传统或准备包派生的 SRT；空文本、零时长、倒序或重叠 cue 必须失败。
2. topic/text：复核 source package 的 cue、scene、generation plan、provisional 总时长、模式与阶段 0 已联合确认的方案完全一致，并说明该 SRT 是 provisional source timeline。一致时不再停止，直接进入阶段 2；有实质差异时回到阶段 0 重新联合确认，不能静默接受。
3. 传统 SRT：显式选择 `voiceoverMode`，但用户已明确给出合法 mode 时直接沿用；用 `parse_srt.py` 输出语义分镜，按视觉状态变化决定 scene 边界并可按需要增加 scene，不设固定数量。每幕只表达一个核心视觉命题，记录 scene ID、cue 范围、核心表达、画面主体和预期全局时长，并说明权威时钟与字幕来源；不得按叙事名词数量机械切幕。
4. **传统 SRT 路径在此停止一次，等待用户确认模式与语义分镜。** 确认前不得创建正式项目；topic/text 不设置第二个策略确认。

### 阶段 2：创建或升级项目

- 新项目创建 schema v2，冻结 `voiceoverMode`、`renderProfile`、generation plan 和独立的 `planning/timing-plan.json`。
- v1 项目只按 Disabled 兼容视图读取，loader 不静默改写。需要持久化 v2 或启用 Edge 时，显式执行原子升级。
- `planning/generation-plan.json` 只负责图片语义、提示词和图片 identity；source/audio 时钟、scene 毫秒边界和累计帧边界只进入 `planning/timing-plan.json`。禁止把 TTS 实际时长回写 generation plan。
- Disabled 创建后跳到阶段 5；Edge 继续阶段 3。

### 阶段 3：Edge 样音与 voice/rate 批准

1. 从已确认文本中选代表性中文自然句。
2. 生成 `previews/voice-sample.wav`，规范化为 canonical WAV 并用 `ffprobe` 校验。
3. 播放样音，等待用户明确确认 voice/rate。
4. 只有收到确认后，才以刚试听的 `SAMPLE_IDENTITY` 执行 `approve-sample`。

Edge TTS 不需要 API Key，但依赖外网和微软语音服务，不是离线模型。网络或服务不可用时记录 `BLOCKED`，保留可恢复证据，不得写成 PASS，也不得自动换 voice/provider。

### 阶段 4：完整旁白、字幕时间轴与真实时钟批准

1. 按已确认 scene 边界生成确定性 speech units；unit 不能跨 scene。
2. 按 `voiceGeneration` 做滚动有界请求；默认 `1` 保持原串行语义。worker 只请求和规范化 attempt candidate，coordinator 按 unit index 单写 checkpoint 与正式 segment。
3. 首错后停止派发新 unit，允许已在途合法 candidate 收尾；`unknown_external_outcome` 不得自动重请求。每个 validated 段立即保留，失败不能删除或覆盖它。
4. 全部 unit 成功后严格按 unit index 串行合并为 `audio/narration.wav`，生成 `audio/timeline.json` 与 `audio/narration.srt`。
5. 只读执行 `validate_voiceover.py`：相互独立的 segment validation 可按 `voiceValidation` 有界并发；timeline、SRT、累计帧、full identity 与 binding 串行。同时报告 provisional 与真实音频时长的差值和比例。
6. 用户必须完整试听 `audio/narration.wav`，不得只抽听首尾；确认配音内容、音色、语速、漏读/重复/断裂/异常停顿和真实时长。`audio/narration.srt` 作为 current 文本/时序证据保留，但字幕在真实画面上的换行、对比度、遮挡与安全区统一推迟到阶段 9 的 contact sheet 和阶段 10 的最终成片审查。
7. `approve-full` 只传 current `--identity-hash`。阈值内不传 `--duration-decision`，manifest 记录 `within_threshold`；偏差超过 10% 时还必须显式传 `accept_actual`，或修改 voice/rate/文本后重做。
8. 批准成功后原子更新 timing plan，使 audio timeline 成为权威时钟；generation plan 不变。

中断后可用 `--retry-failed` 只处理失败或未完成 unit。current、validated 且 synthesis identity 不变的段不得重复请求或覆盖。

### 阶段 5：生成并确认统一线稿

1. 按 generation plan 通过一次运行中显式选定的命名 `/images/generations` 供应商生图；每幕是独立请求并必须携带完整、自包含的 scene prompt；`imageGeneration` 默认 `1`，大于 `1` 时只并发彼此独立的场景请求，coordinator 仍按 plan 顺序单写 manifest 与正式发布。
2. 线稿必须为 1920×1080、暖米黄旧纸底、深灰草图线，统一人物与配色；场景源图禁止文字、字母、数字、标签、水印、标志、写实摄影感、3D 和复杂纹理。该限制只约束生成或导入的场景源图，不约束下述已批准的内置手部覆盖层。
   - 内置 `assets/drawing-hand.png` 的马克笔杆包含用户版权标识 `@moveR`。该标识属于受控绘制覆盖层中的合法版权标识，必须原样保留；不得将其判断为场景源图文字、水印污染或质量缺陷，也不得擦除、替换、重绘或重新生成无字版本。
   - 正式渲染、standalone 诊断和 `assets/preview.html` 默认都必须使用同一份 `assets/drawing-hand.png`。除非用户明确要求更换版权素材，不得静默切换到其他手部图片。
   - 每幕构图必须遵守视觉拓扑首版合同：默认一个主要视觉簇，必要时一个空间独立辅助簇；簇间保留真实纸面留白，不允许嵌套、遮挡或贯穿性连续结构。需要构成连续整体的内容不得为了增加揭示步骤而强行拆簇。
3. 部分失败时保留成功幕，只重试外部结果明确 failed 的幕；`unknown_external_outcome` 不自动重试，也不自动切换供应商。
4. 运行 `validate_generated_images.py`：先串行冻结 project/plan/manifest，再按 `imageValidation` 对独立 PNG 有界并发且每图只完整解码一次；之后用本地图片查看能力实际检查每一幕。
5. 可准备一个覆盖全部 current scene 的 global `visualReview` task 检查人物、配色、纸张和构图一致性。宿主条件满足时由具备真实图片查看能力的新鲜 child 读取冻结文件并写 attempt findings，否则由具备相同能力的 coordinator fallback。findings 只作辅助证据，不能批准线稿、修改图片或触发自动重生成。
6. **停止，等待用户明确确认线稿。** 技术 `validated` 和 visualReview findings 都不能代替人工判断。

### 阶段 6：标注、区域预览与联合批准

仅在线稿确认后开始：

1. 再次验证 generation plan、manifest、图片 SHA 和尺寸；构建一个 `FormalValidationContext`，让本批 timing/audio 全局 evidence 只深验一次，并以 `resolve_formal_scenes(..., context=...)` 解析全部目标幕。
2. coordinator 为每幕冻结独立 `annotationDrafting` task，并按 plan 顺序把最多 3 个独立 task 组成一个 `dispatchUnit`，一次填满 effective child 并发；宿主条件不足时由具备相同能力的 coordinator fallback。child 在 unit 内逐 task 写各自 candidate/result，单幕失败后继续后续幕；审计分别记录 task 数、dispatch unit 数、每 unit task 数与真实 peak child。每幕都必须同时阅读字幕并实际查看原图：字幕只决定叙事先后，元素边界必须按连续墨迹簇划分，不能按叙事名词或坐标机械拆框。一幕允许 1 个元素，默认 1–2 个，最多 3 个且各簇必须空间独立；连续构图必须合并为一个元素。reveal 顺序仍按“场景铺垫 → 关键主体 → 动作/变化 → 反应/结果”组织可独立元素。
3. 批量 validator 先复核 task/result/SHA/current binding，再校验每幕图片、candidate annotation、frame range、timing source、整数像素 region、`protectedRegions` 与局部 reveal 时序。`sequence` 必须连续，元素串行，最后元素结束时间不晚于 `sceneDurationMs - 500`。
4. annotation 绑定 current timing plan、render profile；Edge 还必须绑定 current audio/timeline SHA 和全局 scene 范围。元素 `startMs/durationMs` 始终是从本幕 0 开始的局部时间。
5. coordinator 按 generation plan 顺序把通过 validator 的 candidate 单文件原子发布到扁平 `scenes/` 的同名 `.annotation.json`；失败 candidate 不覆盖旧 current。部分发布时 batch 仍为 `FAIL` 且 `partialSuccess:true`，不得启动全量区域预览或写任何批准。
6. 只有全部必需 scene 都是 current 且 validator PASS，才运行 `serve_preview.py --ensure --project <项目根目录>`，确认服务与项目 API、ready scene 全量通过；随后无需先停顿，直接运行 `generate_annotation_previews.py --project <项目根目录> --all`。该命令先通过全 plan technical current Gate，再用一个 `FormalValidationContext`、JSON `annotationPreview` 有界并发、候选完整 PNG 重开验证和 generation plan 顺序原子发布生成各幕编号/方向预览及有序 contact sheet；成功路径调用 `annotation_review.py` 生成/验证 `manifests/annotation-review-manifest.json`，并在摘要输出 `annotationReviewIdentitySha256`。
7. 完整展示标注摘要、命令输出的可点击 `PREVIEW_URL`、contact sheet、必要的全分辨率预览、`protectedRegions`、叙事顺序与 reveal 时序，说明链接打开后无需手动导入。**只在这里停止一次，等待用户对 current 联合 review bundle 明确确认。** 技术 current、result completed、validator PASS、URL 已打开、页面已保存或用户未反对都不能替代人工批准。
8. 收到确认后，以 current `annotationReviewIdentitySha256` 执行 `approve_annotation_review.py`，把持久化批准写入 `manifests/annotation-review-approval.json`，绑定有序 annotation/preview bundle、timing/render 与 Edge 按需 evidence。未获批准或 identity stale 时 `annotation_review_confirmation` 及正式渲染必须以退出码 5 拒绝。
9. 用户要求修改时直接编辑并重验受影响 JSON，只重新生成受影响 scene 的 preview；未变化且 binding current 的 preview 保留。任何受绑定字节变化都会使旧 annotation review approval stale，重建 current bundle 后重新执行一次联合确认。仅当用户明确要求时才打开预览台拖拽。

### 阶段 7：正式多幕渲染与场景联合批准

1. 正式渲染必须使用 `--project` 与 `--scene-id`，先硬校验 current `manifests/annotation-review-approval.json` 与 `annotationReviewIdentitySha256`，再严格消费项目 `renderProfile` 和 timing plan 的 `frameCount`；缺失或 stale 时返回 5。
2. 不得用 `--fps`、`--total-ms` 或尺寸参数覆盖正式项目合同；这些参数仅属 standalone/预览路径。
3. 渲染器把每帧 BGR24 直接写入 FFmpeg stdin，由 libx264 `medium`/CRF18 一次编码为 H.264/yuv420p candidate；禁止 `cv2.VideoWriter`、MP4V 中间文件和二次转码。stderr 必须并发 drain，错误 tail 必须去敏；早退、BrokenPipe、非零退出、磁盘/管道错误、关闭异常、欠写或超写帧数全部 fail closed，不能覆盖旧正式 scene。
4. candidate 验证 H.264、1920×1080、60fps、yuv420p、0 音频流、权威目标帧数并完整解码一次；原子发布后只以该 deep receipt 复核 SHA/bytes binding，不重复 full decode。binding 失败时恢复旧正式 scene，不能写成功 identity/manifest。
5. 抽查首帧、重叠模块中段和最终完整画面。
6. 正式 `render-manifest.json` 必须记录并绑定 current `handSha256`。使用内置 `drawing-hand.png` 时，马克笔杆上的 `@moveR` 应按合法版权标识判定为通过，不得作为拒绝该幕或触发无字重渲染的理由。
7. coordinator 一次调用正式 batch render，按共享 loader 返回的 `sceneRender` 有界并行渲染、技术验证独立单幕 candidate；coordinator 按 generation plan 顺序复核 binding、原子发布并单写 manifest，不在每一幕完成后停止等待用户。摘要记录 configured/effective/peak worker 与 task count；这些并发字段不进入作品 identity。局部重做使用同一入口的 `--scene-ids`。
8. 全部必需 scene current 后运行 `scene_review.py --project <项目根目录>`，按 generation plan 顺序形成 bundle 并取得 `sceneReviewIdentityHash`；交付有序场景列表与可完整播放的 current 单幕媒体。**只在这里停止一次，等待用户确认全部场景，或明确指出不通过的 scene ID。** 技术 PASS 不得写成人工批准。
9. 用户拒绝部分 scene 时只重做受影响 scene，并重建 bundle；未变化且 render identity current 的 scene 保留。全部 scene 获确认后，以 current `sceneReviewIdentityHash` 执行 `approve_scene_review.py`，把 `sceneReviewApproval` 写入 `manifests/render-manifest.json` 顶层。未获批准、bundle identity stale、scene 集合或输入顺序不匹配时，`merge_scenes.py` 必须在写 concat/candidate 前以退出码 5 拒绝。

### 阶段 8：静音画面母版技术合并

1. 只合并 current scene review approval 精确绑定的有序单幕集合，生成 `output/final-video-only.mp4`；不得用聊天记录、技术 PASS 或旧 bundle 代替硬校验。
2. 单幕项目可以跳过 concat，但仍必须发布并技术验证同名 clean master。
3. 总帧数必须恰好等于 timing plan 最后一幕的 `endFrameExclusive`。
4. `final-video-only.mp4` 只作为确定性字幕输入、诊断和重新烧录工件，不设独立人工确认。技术验证通过后立即进入阶段 9；正常路径不得展示该工件并停下等待用户回复。

### 阶段 9：正式字幕烧录

1. 根据 mode 严格选择唯一权威 SRT。
2. 从 `execution.videoEncoding.subtitlePreset` 读取 `medium | fast | veryfast`；缺失时直接使用 `medium`，不改写本地配置，也不接受 CLI 临时覆盖。
3. 确定性编译 `subtitles/final.ass`，使用固定 `subtitle-style-v1` 与固定字体 identity。
4. 按 `subtitle-burn-v2` 以软件 `libx264`、所选 preset、CRF18、yuv420p、fps passthrough 和 0 音频流烧录为 `output/final-subtitled-video-only.mp4`；preset/contract 写入 manifest、technical receipt 和各层 identity/binding。
5. 仅当 preset、current SHA/bytes 与 receipt 全部匹配时复用 current burn 并走 binding；preset 变化时重建字幕与 downstream final、清空 `finalApproval`。失败 candidate 不覆盖旧正式输出。
6. 生成 `previews/final-subtitle-contact-sheet.png`，至少包含首条、中间、末条字幕中段；只有权威 SRT 确有空档时才添加空档中点。没有空档时记录 `gapEvidence: not_applicable_no_gap`。
7. 用本地图片查看检查真实画面上的位置、两行换行、描边、对比度和安全边距。技术 PASS 不构成字幕视觉确认。

旁白阶段不再生成字幕预审视频。本阶段的 contact sheet 才是字幕在最终线稿画面上的换行、对比度、遮挡和安全区证据。

此阶段完全不需要浏览器、预览台或文件选择框。Disabled 模式在 captioned candidate 验证后，同时把相同已验证字节原子发布为 `output/final.mp4`；Edge 模式继续阶段 10。

### 阶段 10：音频封装、技术验证和最终批准

- Disabled：`final.mp4` 必须为 1 路 H.264 视频、0 音频流，且字幕已烧录。
- Edge：将 current、approved canonical WAV 编码为 24kHz mono AAC，以 `-c:v copy` 与 captioned video 封装为 `final.mp4`。
- 验证三层输出、codec、分辨率、像素格式、fps、帧数、时长、音频采样率/声道、SHA、delivery identity 和完整 null-sink 解码。
- 技术验证和完整旁白批准都不会批准成片。向用户提供 contact sheet 和 `final.mp4`，等待完整看片；Edge 还要完整听音。
- 只有收到明确确认后，才用刚验证的 current final identity 执行 `approve_final_media.py`。

## 权威时序与累计帧边界

`renderProfile` 固定首版正式合同：`whiteboard-render-v2`、1920×1080、60fps、H.264、yuv420p、`cumulative-ceil-v1`。scene 毫秒范围是全局连续时间；annotation 元素时间是场景局部时间。

```text
globalEndFrame(scene) = ceil(scene.globalEndMs * fps / 1000)
sceneStartFrame        = 上一幕 globalEndFrame（第一幕为 0）
sceneEndFrameExclusive = globalEndFrame(scene)
sceneFrameCount        = sceneEndFrameExclusive - sceneStartFrame
globalFrameCount       = 最后一幕 sceneEndFrameExclusive
```

禁止对每幕 duration 各自 `ceil` 后相加，也禁止用 `-shortest`、隐式尾帧或 `gaze_until` 掩盖错误。Disabled 画面从全局 0 开始，以 source SRT 最后 cue 的 endMs 收口；首 cue 前导空白和跨幕空档必须保留在连续 scene 区间中。Edge 以 approved audio timeline 收口。音视频只有在 `max(1 帧, 80ms)` 内才可确定性补零，超出必须失败。

## 标注与遮罩不变量

- `canvas` 必须等于 1920×1080 正式源图尺寸；所有 region/protectedRegions 使用左上角原点的整数像素坐标。
- `sequence` 从 1 起连续；`narrativeRole` 与 `subtitle` 必须反映对应字幕事件。annotation 的 `subtitle` 是语义映射，不是正式烧录字幕时间源。
- `elements` 按连续墨迹簇而非叙事名词划分；允许 1 个，默认 1–2 个，最多 3 个且仅限彼此空间独立的墨迹簇。必须连续呈现的构图合并为同一个元素。
- region 之间不得嵌套、交叉或横穿其他 region 的有效墨迹；不得把共享背景、共同底面或贯穿性连接结构拆到不同 region。`protectedRegions` 不能作为错误分区的补丁。
- 同一幕内元素串行作画；每区 `durationMs` 按 `ink:color = 2:1` 分配。
- 每个区域的允许掩码 = 本区域 `region` 扣除所有后续区域和本区域 `protectedRegions`。未开始内容不得露线。
- 未揭示区域与已揭示区域中的普通纸纹必须规范到同一张确定性暖米黄画布底色；中途不得出现因纸纹色差形成的纯白或异色矩形遮罩块。正式 `paper-content-mask-v3` 必须在墨迹检测前，以深色线稿和显著色彩为内容种子，移除小型纸纹连通块，并抑制触碰画布边缘但缺少深色线稿的浅色组件；人物、物体、箭头、浅色光束和主要水彩色块必须保留。
- `reveal.direction` 与 `handPath` 只供预览台矩形代理；正式 stream 笔迹由 grid/skeleton 自动生成。
- 最后元素必须在权威 scene 尾部前至少 500ms 完成，渲染器不得自行延长总时长。

## 项目与扁平目录合同

正式项目只位于 `config/workspace.local.json` 配置的 D 盘工作区，默认 `D:\SRTWhiteboard\projects\<项目名>`。目标盘不可用或不可写时必须停止，不得回退到 C 盘、用户目录或系统临时目录。

```text
<project>/
  project.json
  source/input.json                  # topic/text 项目按需
  source/source-manifest.json        # topic/text 项目按需
  source/source.srt
  planning/
    generation-plan.json
    timing-plan.json
    voice-plan.json                 # Edge 按需
  scenes/                           # 扁平目录，不使用每幕子目录
    scene-01-<名称>.png
    scene-01-<名称>.annotation.json
    scene-01-<名称>-whiteboard.mp4
  audio/                            # Edge 按需
    segments/unit-0001.wav
    narration.wav
    timeline.json
    narration.srt
  subtitles/final.ass
  previews/
    voice-sample.wav                # Edge 按需
    scene-01-<名称>-annotation-preview.png
    final-subtitle-contact-sheet.png
  manifests/
    generation-manifest.json
    voice-manifest.json             # Edge 按需
    annotation-review-manifest.json
    annotation-review-approval.json
    render-manifest.json             # 顶层 sceneReviewApproval
    delivery-manifest.json
  output/
    final-video-only.mp4
    final-subtitled-video-only.mp4
    final.mp4
  .work/<run-id>/
```

图片与标注严格同名：`foo.png` 对应 `foo.annotation.json`。所有 JSON 只保存项目相对 POSIX 路径，不保存密钥、Cookie、Token、临时 URL、PID 或盘符绝对路径。

三层输出用途固定：

- `final-video-only.mp4`：无字幕、无音频的 clean master，供诊断和重新烧录。
- `final-subtitled-video-only.mp4`：已烧录 current 字幕、无音频的诊断母版。
- `final.mp4`：正式交付，始终有烧录字幕；Disabled 无音频，Edge 有 AAC。

## 命令行

先捕获解释器。每次调用都会把基础依赖与显式选择的 feature 合并后，在同一个解释器子进程中一次批量探测；下面的 `--check` 与准备命令是二选一，不要作为两步连续执行：

```powershell
# 仅检查已有基础环境
python scripts/prepare_env.py --check

# 或：建立/补齐基础环境
python scripts/prepare_env.py
```

成功末行输出 `ENV_PY=<路径>`。Disabled 和字幕流程不要求安装 `edge-tts`；Edge 模式从下列两条中选择一条，基础依赖与固定版本 `edge-tts` 仍只做一次批量 probe：

```powershell
# 建立/补齐 Edge 环境
<ENV_PY> scripts/prepare_env.py --feature edge-tts

# 或：仅检查已有 Edge 环境
<ENV_PY> scripts/prepare_env.py --check --feature edge-tts
```

内容准备、解析、创建或升级：

```powershell
# topic/text：先冻结 contentDrafting attempt 并输出宿主 spawn package；只 prepare，不实际 spawn：
<ENV_PY> scripts/prepare_draft_agent_task.py contentDrafting `
  --draft-root <D:\SRTWhiteboard\drafts\草案ID> `
  --content-input <whiteboard-content-input-v1.json>

# 用户实质修改：revision request 与 base candidate 成对传入，并与 --content-input 互斥；创建新 attempt：
<ENV_PY> scripts/prepare_draft_agent_task.py contentDrafting `
  --draft-root <D:\SRTWhiteboard\drafts\草案ID> `
  --revision-request <whiteboard-content-revision-request-v1.json> `
  --base-content-draft <上一版-candidate.content-draft.json>

# child 完成后把 candidate JSON 字节直接写入 stdin；人工确认前不得改用 --draft：
<ENV_PY> -B scripts/validate_content_draft.py --stdin

# 只读校验 PASS 后确定性生成 draft scope 审阅文件；主窗口只交付路径、identity 与短摘要：
<ENV_PY> scripts/render_content_review.py `
  --draft-root <D:\SRTWhiteboard\drafts\草案ID> `
  --candidate <attempt\candidate.content-draft.json>

# 仅当用户明确确认 current identity 后，才持久化输入并使用 --draft
<ENV_PY> scripts/prepare_source.py `
  --draft <已获用户确认的-content-draft.json> `
  --output-dir <D:\SRTWhiteboard\drafts\项目名>

# topic/text：source evidence 必须成对传入；首版仅 edge-tts
<ENV_PY> scripts/create_project.py --name <项目名> `
  --srt <draft-dir\source.srt> `
  --plan <draft-dir\generation-plan.json> `
  --source-input <draft-dir\input.json> `
  --source-manifest <draft-dir\manifest.json> `
  --voiceover-mode edge-tts

# 传统 SRT：严格解析并冻结 storyboardPlanning attempt，输出宿主 spawn package：
<ENV_PY> scripts/prepare_draft_agent_task.py storyboardPlanning `
  --draft-root <D:\SRTWhiteboard\drafts\草案ID> `
  --source-srt <字幕.srt> `
  --target-sec 30 --min-sec 25 --max-sec 35

# standalone 严格解析/诊断入口保持可用，但不能代替上述 task/result 交接：
<ENV_PY> scripts/parse_srt.py <字幕.srt> --target-sec 30 --min-sec 25 --max-sec 35

<ENV_PY> scripts/create_project.py --name <项目名> --srt <字幕.srt> `
  --plan <已确认策略.json> --voiceover-mode disabled

<ENV_PY> scripts/create_project.py --name <项目名> --srt <字幕.srt> `
  --plan <已确认策略.json> --voiceover-mode edge-tts

<ENV_PY> scripts/upgrade_project.py --project <项目根目录> `
  --to-schema 2 --voiceover-mode edge-tts
```

两种 draft prepare 成功都输出 `whiteboard-draft-agent-prepare-v1`：若 `spawnPackage.spawnAgentCall` 非空，立即调用宿主；为空则按 `dispatchAudit` 进入 coordinator fallback。`formalPublished:false`、`approvalWritten:false` 和 spawn package 的 `hostSpawnExecuted:false` 表示只冻结了 attempt；不得继续翻阅 Python 源码推导派发，也不得运行 `prepare_source.py`、创建项目或写批准。content child 返回后仍须用 `validate_content_draft.py --stdin` 只读校验；storyboard candidate 仍须展示并取得传统 SRT 分镜确认。

续接已有项目时使用 `create_project.py --resume <项目根目录> --srt <原始字幕.srt>`，不能同时传 `--plan` 或 `--voiceover-mode`。

生图与消费验证：

```powershell
<ENV_PY> scripts/generate_images.py --project <项目根目录>
<ENV_PY> scripts/validate_generated_images.py --project <项目根目录>

# 技术验证通过后冻结 global visualReview task，并输出可直接交给宿主的 spawn package；
# 此命令只 prepare，不创建 child，也不批准线稿：
<ENV_PY> scripts/validate_generated_images.py --project <项目根目录> `
  --prepare-visual-review
```

`visualReview.spawnPackage.spawnAgentCall` 非空时立即调用宿主真实 `spawn_agent`；`preparedOnly:true`、`hostSpawnExecuted:false` 与 `peakChildAgents:0` 表示尚未派发，不能继续阅读 Python 源码代替宿主调用，也不能把 prepare 写成 dispatch PASS。spawn call 为空时由具备真实图片查看能力的 coordinator fallback；两条路径都不得写线稿批准。

Edge 样音、完整旁白、状态与技术验证：

```powershell
<ENV_PY> scripts/generate_voiceover.py sample --project <项目根目录> `
  --voice zh-CN-YunjianNeural --rate 0

<ENV_PY> scripts/generate_voiceover.py approve-sample --project <项目根目录> `
  --identity-hash <刚完整试听的 SAMPLE_IDENTITY>

<ENV_PY> scripts/generate_voiceover.py full --project <项目根目录>
<ENV_PY> scripts/generate_voiceover.py full --project <项目根目录> --retry-failed
<ENV_PY> scripts/generate_voiceover.py status --project <项目根目录>
<ENV_PY> scripts/validate_voiceover.py --project <项目根目录>

# 技术 receipt 缺失/过期、验证器合同变化，或显式要求重新完整解码时：
<ENV_PY> scripts/validate_voiceover.py --project <项目根目录> --force-deep

<ENV_PY> scripts/generate_voiceover.py approve-full --project <项目根目录> `
  --identity-hash <刚完整试听旁白所对应的 FULL_IDENTITY>

# 仅当真实时长偏差超过 10% 且用户明确接受时：
<ENV_PY> scripts/generate_voiceover.py approve-full --project <项目根目录> `
  --identity-hash <FULL_IDENTITY> `
  --duration-decision accept_actual
```

`full` 成功输出 `FULL_AUDIO` 与 `FULL_IDENTITY`，不会生成 1920×1080 旁白预审视频。`validate_voiceover.py` 只读验证 current WAV/timeline/narration SRT，绝不写人工批准；full identity 不匹配或技术证据 stale 时，`approve-full` 返回 5。

标注预览与正式逐幕渲染：

```powershell
# 仅在线稿已明确确认后，先冻结 annotationDrafting tasks 并输出宿主 spawn 计划；
# runtime 参数必须来自当前宿主，不能猜测或重复扣减 coordinator 槽位：
<ENV_PY> scripts/validate_annotations.py prepare --project <项目根目录> `
  --images-confirmed `
  --runtime-child-slots <宿主已换算的-child-slots> `
  --coordinator-resource-budget <coordinator资源预算> `
  --runtime-role-capability readFiles `
  --runtime-role-capability viewImage `
  --runtime-role-capability writeCandidateJson `
  --coordinator-can-view

# coordinator 只按 dispatchUnits[].spawnRequest 与 dispatchPlan.maxParallel 完成真实 spawn/wait；
# orderedTasks 仅是逐幕 validator/retry/publish 元数据，不能再逐项 spawn。
# 再验证 result/candidate 并按计划顺序发布：
<ENV_PY> scripts/validate_annotations.py validate --project <项目根目录> `
  --candidate-root <项目根目录\.work\run-id\agent-tasks>

# 全量 AI annotation 已发布且 current 后：后台启动或复用本地服务，
# 验证项目 API 与全部场景配对，并输出必须直接交给用户的 PREVIEW_URL
<ENV_PY> scripts/serve_preview.py --ensure --project <项目根目录>

# 可选：打开后直接定位某一幕；只读审阅可附加 --mode view
<ENV_PY> scripts/serve_preview.py --ensure --project <项目根目录> `
  --scene-id scene-03

# 全量 annotation 技术 current 后即可生成本地区域预览，无需先人工确认；
# 成功摘要直接给出 annotationReviewIdentitySha256：
<ENV_PY> scripts/generate_annotation_previews.py --project <项目根目录> --all

# 仅在用户一次确认 current 标注、区域预览、protectedRegions 与 reveal 时序后：
<ENV_PY> scripts/approve_annotation_review.py --project <项目根目录> `
  --identity-hash <annotationReviewIdentitySha256>

# 单幕 standalone 诊断兼容入口：
<ENV_PY> scripts/render_annotation_preview.py <图片> <标注> <预览图输出>

# 正常正式路径：一次初始化，按 sceneRender 有界并行生成候选，再按 plan 顺序发布
<ENV_PY> scripts/render_stream_whiteboard.py --project <项目根目录> `
  --all --ink-path grid --color-fill contour-wipe

# 局部重做：输入顺序不改变 plan 提交顺序
<ENV_PY> scripts/render_stream_whiteboard.py --project <项目根目录> `
  --scene-ids scene-05 scene-08 --ink-path grid --color-fill contour-wipe

# 单幕兼容入口
<ENV_PY> scripts/render_stream_whiteboard.py --project <项目根目录> `
  --scene-id scene-01 --ink-path grid --color-fill contour-wipe

# 全部单幕渲染/检查并按 plan 发布完成后，形成有序 bundle 并取得 sceneReviewIdentityHash：
<ENV_PY> scripts/scene_review.py --project <项目根目录>

# 仅在用户联合确认 current bundle 后：
<ENV_PY> scripts/approve_scene_review.py --project <项目根目录> `
  --identity-hash <sceneReviewIdentityHash>
```

annotation prepare 的 `--runtime-child-slots`、`--coordinator-resource-budget` 默认都是 `0`，runtime capabilities 默认空集合；省略时 effective 为 `0`，不会假装宿主可派发。成功 JSON 使用 `whiteboard-annotation-prepare-v2`；`dispatchUnits` 与 `dispatchPlan.maxParallel` 是真实 spawn/wait 的唯一权威输入，`orderedTasks` 只保留逐幕 task/result/candidate/retry/publish 元数据。每个 unit 最多 3 个连续 scene；`hostSpawnPerformed:false` 表示宿主尚未完成派发。

正式项目通过 BGR24 stdin → libx264 的单次编码链生成 candidate，不经过 OpenCV MP4V 中间文件。不得传 `--fps`、`--total-ms` 或 `--cap-long-edge` 覆盖持久化合同；`sceneRender` 只从共享 workspace loader 读取并控制独立单幕候选的有界 worker 数，coordinator 仍按 plan 顺序单写发布。standalone 位置参数路径只用于诊断/预览，不可充当正式交付证据。

成片连续构建、验证和最终批准：

```powershell
<ENV_PY> scripts/merge_scenes.py --project <项目根目录> `
  --inputs <幕1.mp4> <幕2.mp4> <幕3.mp4>

<ENV_PY> scripts/burn_subtitles.py --project <项目根目录>

# 仅 Edge 模式：
<ENV_PY> scripts/mux_voiceover.py --project <项目根目录>

<ENV_PY> scripts/validate_final_media.py --project <项目根目录>

# 仅在用户已完整确认 current final 后：
<ENV_PY> scripts/approve_final_media.py --project <项目根目录> `
  --identity-hash <刚完整看片听音的 FINAL_IDENTITY>
```

current scene review approval 通过后，连续执行 `merge_scenes.py → burn_subtitles.py →（仅 Edge）mux_voiceover.py → validate_final_media.py`。`merge_scenes.py` 在任何 concat/candidate 写入前硬校验批准所绑定的有序 scene 集合与 current render identities；缺失或 stale 时返回 5。clean master 只接受技术验证，不向用户索要独立确认；只有链路失败时才停下修复。链路成功后直接提供 `output/final.mp4` 供最终完整看片，Edge 模式同时完整听音，再等待 final identity 的明确确认。

字幕 preset 只读取 workspace JSON 的 `execution.videoEncoding.subtitlePreset`，CLI 不接受 `--preset` 临时覆盖。默认 `medium`；`fast` 与 `veryfast` 只改变正式编码 preset，并随 `subtitle-burn-v2` 进入 manifest、technical receipt、subtitle/captioned/final identity。硬件编码器自动探测与 NVENC/QSV/AMF 均未实施。

Edge mux 成功会直接输出 `FINAL_IDENTITY`；两种模式都可从 `validate_final_media.py` 的 JSON 结果取得 current `finalIdentitySha256`。该验证器会把独立技术验证证据写入 delivery manifest，但绝不写人工批准。提交批准前必须确认 identity 仍与刚完整观看的文件一致。

## 退出码

| 码 | 含义 |
|---:|---|
| 0 | 操作成功且对应技术验证通过 |
| 1 | 批处理有失败/取消 unit；环境准备和旧 standalone helper 也用 1 表示其本地操作失败 |
| 2 | content draft、参数、项目、配置、plan、manifest、SRT 或 timeline 无效 |
| 3 | Edge 外部请求失败或限流重试耗尽 |
| 4 | FFmpeg、ffprobe、字体、字幕、WAV 或媒体验证失败 |
| 5 | stale、identity 不匹配或缺少人工批准 |

上表是正式交付的权威语义；各脚本只返回与自身职责有关的子集。`prepare_env.py` 是独立环境工具，其依赖安装/检查失败为 1；`parse_srt.py` 和 standalone 诊断渲染保留原有输入读取失败码。正式项目渲染必须遵循上表：无效 project/plan/timeline 为 2，媒体/FFmpeg 验证失败为 4，stale 或缺批准为 5。

## stale 与恢复

- topic/body/rewritePolicy/target/narration cue/scene mapping 变化：content draft、source package、generation/timing plan、voice plan/segments/audio/timeline/narration SRT、annotation、场景视频、字幕、final 与最终批准全部重新判定。
- 只有 imagePrompt 改变且 narration cue/scene boundary 不变：音频可复用；generation plan、图片与视觉下游 stale。
- provisional SRT 排时算法版本变化：source identity/timing 重新判定；synthesis identity 未变的已验证 Edge segments 可按现有规则复用，但时长决定、timing plan 与下游重新验证。
- voice/rate/朗读文本/分段边界/provider synthesis contract 变化：样音、完整旁白批准、受影响 segment、WAV、timeline、narration SRT、annotation 时序、场景视频、字幕、final 和最终批准失效。
- 只有 source timing/audit 改变且朗读文本、scene 边界和 synthesis identity 不变：validated Edge segment/WAV 可复用，但偏差决定、完整旁白批准、timeline 和下游重新判定；Disabled 时间轴、场景视频、字幕和 final stale。
- audio、timeline/timing plan、render profile 或 mode 变化：相关 annotation、场景视频、clean/captioned/final 和最终批准 stale；纯时序变化不得改变 generation plan/image manifest identity。
- 任一 annotation、区域 preview、`protectedRegions`、reveal 时序或联合 review binding 变化：annotation review approval stale；只重建受影响 preview，未变化且 binding current 的 preview 可保留。
- 任一正式 scene 视频、render identity、hand SHA、scene 集合或 generation plan 顺序变化：scene review approval stale；未变化且 current 的 scene 可保留，但合并前必须重建并重新批准有序 review bundle。
- 手部覆盖素材、其 `handSha256` 或 `@moveR` 版权标识发生变化：受影响的场景视频、clean/captioned/final 和最终批准 stale；修改或移除该版权标识还必须先取得用户明确授权。
- 字幕样式、字体、权威 SRT、`subtitlePreset` 或 `subtitle-burn-v2` encoding contract 变化：subtitle identity、captioned、Disabled/Edge final 和最终批准 stale，clean video 与有效音频可保留；preset 变化必须重建字幕烧录与 downstream final，并清空 `finalApproval`。
- clean video 或 final SHA 变化：下游 identity 和最终批准 stale。
- 任何 stale 文件可作为历史证据保留，但不得作为 current 输入进入下一阶段。失败候选不得覆盖已验证正式文件。

## 质量与验收

- 首帧为干净暖米黄纸底，未开始区域无提前露线；末尾保留至少 0.5 秒完整画面，但不突破权威总时长。
- generation plan、manifest、图片 SHA、1920×1080 实际尺寸和人工线稿确认均 current。
- annotation 绑定 current timing/render identity，元素使用局部时钟且不越界。
- annotation review approval 绑定 current 有序 annotation/preview bundle、`protectedRegions`、reveal 时序和所需 timing/audio evidence；技术 current 不能替代联合人工批准。
- 正式单幕使用 current `assets/drawing-hand.png`，manifest 中的 `handSha256` 与实际素材一致；`@moveR` 作为已批准版权层保留，不参与场景源图禁字判定。
- 单幕和 clean master 帧数严格符合累计全局帧边界，全部完整解码；scene review approval 精确绑定 generation plan 顺序的 current scene 集合，`merge_scenes.py` 已硬校验。
- `subtitles/final.ass`、字体 hash、样式 hash、权威 SRT 和 contact sheet 写入 delivery 证据。
- Disabled final：1 路 H.264、0 音频；Edge final：1 路 H.264 + 1 路 24kHz mono AAC；两者都有烧录字幕。
- 自动测试使用 fake provider/fixture，不调用真实 Edge 或图片 provider。真实 Edge 必须另行通过样音、完整旁白试听与真实时长、最终成片人工关卡；外网或服务不可用时写 `BLOCKED`，绝不以 fixture PASS 或 SKIP 冒充外部 PASS。

详细合同见 [references/content-input.md](references/content-input.md)、[references/voiceover.md](references/voiceover.md)、[references/subtitles.md](references/subtitles.md) 和 [references/image-generation.md](references/image-generation.md)。
