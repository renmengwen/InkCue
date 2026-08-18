# 主题 / 正文内容入口合同

本文说明阶段 0 的 `topic | text` 首版入口、`whiteboard-content-draft-v1`、artifact-first 审阅与修订、内容与制作方案联合确认、确定性 source package、正式项目 provenance、stale/恢复与验收边界。传统 `inputMode=srt` 路径保持一次独立的模式/语义分镜确认；其 `voiceoverMode=disabled | edge-tts`、严格 SRT、人工关卡和最终交付语义不得回归。

## 首版能力边界

外部输入模式固定为：

```text
inputMode = srt | topic | text
rewritePolicy = preserve | polish | generate
```

合法组合固定为：

| inputMode | 必要内容 | rewritePolicy | 首版 voiceoverMode |
|---|---|---|---|
| `srt` | 现有严格 SRT | 不适用 | `disabled | edge-tts` |
| `topic` | 非空主题 | 仅 `generate` | 仅 `edge-tts` |
| `text` | 非空正文 | `preserve | polish` | 仅 `edge-tts` |

`topic + preserve`、`topic + polish`、`text + generate` 以及非 SRT + `disabled` 均须拒绝。topic/text 首版不使用估算阅读时长作为最终权威时钟；target 只用于内容预算与 provisional SRT，获批的真实 Edge audio timeline 才接管正式时钟。

首版不接入通用文本模型 provider。旁白稿、cue、scene 和画面建议由宿主真实派发的 `contentDrafting` child 生成 candidate，再由 coordinator 校验并确定性生成 Markdown 审阅 artifact；`prepare_draft_agent_task.py` 只冻结 attempt 和 host spawn package，`content_source.py` 与 `prepare_source.py` 只做确定性规范化、校验、排时、hash、持久化和派生文件。上述脚本都不发起文本模型请求、不读取外部凭据、不自行改写或批准草案，也不创建正式项目。

## 阶段 0 内容与制作方案联合关卡

topic/text 的权威顺序是：

1. 接收原始主题或正文，并冻结 `rewritePolicy`、`targetDurationSeconds` 与 `voiceoverMode`。用户已经明确给出的具体值直接沿用；仅当字段缺失时，才把所有缺失配置汇总为一次前置确认，不逐字段询问，也不在后续重复确认相同值。
2. coordinator 运行 `prepare_draft_agent_task.py contentDrafting` 冻结 attempt；`spawnPackage.spawnAgentCall` 非空时立即调用宿主，由新鲜 child 生成完整 `whiteboard-content-draft-v1` candidate；为空时由 coordinator fallback。两条路径使用同一 task/result 合同。
3. coordinator 重验 result、SHA 与 candidate 合同后，从 `candidate.content-draft.json` 确定性生成不可变 Markdown 审阅 artifact。主窗口只发送文件链接、完整 identity、cue/scene 计数和短摘要，不把长正文、逐幕提示词或整份 Markdown 读回、转述或粘贴到聊天。
4. **停止并等待用户明确确认 current `contentDraftIdentitySha256`，完成“内容与制作方案联合确认”。** 这一次确认同时覆盖内容草案与模式/语义分镜策略；未回复、此前笼统授权、技术校验通过或“用户没有反对”都不是批准。
5. 用户要求实质修改时，把意见冻结为 revision request，绑定 current base identity；创建新 attempt 并 spawn 新鲜 `contentDrafting` child。新 candidate 重新校验并生成新 identity、新 Markdown；旧版保留但判为 stale。followup 只用于同一冻结 attempt 的执行性补正。
6. 只有 current identity 获明确确认后才允许运行 `prepare_source.py`。准备包生成后，确定性复核 provisional 总时长、cue、scene、generation plan 与已确认方案一致，并说明它不是最终真实语音时钟。一致时直接创建正式项目，不再询问相同策略；出现实质差异时必须回到步骤 3 生成新审阅 artifact 并重新联合确认。

脚本不提供“自动批准”参数，也不能从 JSON 或 Markdown 字段推断用户已经同意。是否允许调用 `prepare_source.py` 是代理在聊天层必须执行的人工关卡；技术验证不能冒充用户确认。联合确认前仍处于 draft scope，不存在正式 project；审阅文件只能位于 workspace 的 `drafts/<draft-id>/reviews/`，不得提前写入 `projects/<项目名>`。联合确认后的确定性准备、严格 round-trip 与一致性校验不新增第二次策略确认。

### Artifact-first 审阅与修订

`candidate.content-draft.json` 是阶段 0 唯一机器权威源。Markdown 只能由 coordinator 在 candidate 通过只读校验后确定性渲染，不得由 child 自由撰写，也不得被下游解析为 source、SRT、plan、identity 或批准输入。

```text
<workspace>/drafts/<draft-id>/
  reviews/
    content-review-<contentDraftIdentity前12位>.md
  .work/<run-id>/agent-tasks/<task-id>/
    attempt-001/
      task.json
      role-contract.md
      candidate.content-draft.json
      result.json
    attempt-002/
      task.json
      role-contract.md
      base.content-draft.json
      revision-request.json
      candidate.content-draft.json
      result.json
```

review 文件名必须使用完整 identity 的前 12 位；正文至少展示完整 identity、输入模式、rewritePolicy、target、voiceoverMode、原始 topic/body、完整旁白、实质改动说明、cue→scene、每幕核心表达/画面主体/`imagePrompt`、provisional SRT 与权威时钟说明，以及 `pending` 审阅状态。相同 canonical candidate 必须产生相同 Markdown 字节；文件内绝对路径、秘密和不确定审计字段不得进入内容。Markdown 的 `pending` 只是显示状态，不能作为批准字段。

主窗口交付 review 时不得用整文件读取把内容回灌到主上下文；只消费渲染器的短结构化摘要并发送可点击文件链接。用户可用稳定的 `cueId`、`sceneId` 或全局说明提出修改；只有无法定位时才按需读取 review 的局部片段。

用户修改意见由 coordinator 冻结为 `whiteboard-content-revision-request-v1`：

```json
{
  "schemaVersion": 1,
  "contractVersion": "whiteboard-content-revision-request-v1",
  "baseContentDraftIdentitySha256": "<64位 current identity>",
  "globalInstructions": ["整体语气更克制"],
  "cueChanges": [
    {"cueId": "cue-003", "instruction": "保留结论，缩短为两句话"}
  ],
  "sceneChanges": [
    {"sceneId": "scene-02", "instruction": "改为单一人物构图"}
  ],
  "mustPreserve": ["所有数字和责任主体"]
}
```

revision request 固定 `schemaVersion: 1`，只保存用户本轮真实要求，不复制上一版全文；`globalInstructions`、`cueChanges`、`sceneChanges` 至少一项非空，单独填写 `mustPreserve` 不构成修改。`baseContentDraftIdentitySha256` 必须等于派发时的 current candidate identity，否则以 stale 拒绝。实质修改一律创建新 attempt 和新鲜 child，上一版 candidate/review 不覆盖、不删除，并在新版本成为 current 时判为 stale。缺失 `result.json`、result schema 错误、漏写已冻结输出等同一任务的执行性问题才允许 followup 原 child；任何文案、cue、scene、提示词或用户要求变化都必须冻结新 revision request 并创建新 attempt。

### 分幕与视觉拓扑合同

`contentDrafting` 生成 cue→scene、`visualSubject` 和 `imagePrompt` 时，必须同时遵守以下首版编写规则；这些规则不增加 `whiteboard-content-draft-v1` 字段，也不改变联合确认关卡：

- scene 边界由视觉状态变化决定。出现新的状态、因果阶段、构图中心或需要独立呈现的结果时可以增加 scene，不设固定 scene 数量；不得按名词数量或固定时长机械切幕。
- 每幕只表达一个核心视觉命题，默认使用一个主要视觉簇；叙事确有需要时，最多再使用一个空间独立的辅助视觉簇。
- 两个视觉簇之间必须存在真实、连续的干净纸面留白，不得互相嵌套、遮挡，也不得由连续背景、共同底面、长线或其他贯穿性结构连接。
- 多个概念若必须组成同一个连续构图，应合并为一个视觉簇；需要更多表达时优先增加 scene，而不是在同一张图内继续拆分相互依赖的簇。
- 这些是通用空间关系约束。不得通过固定的场景对象清单或预设对象类别来编写提示词；具体视觉内容仍由当前旁白语义决定。

后续 annotation 按连续墨迹簇划分，而不是按这里的叙事名词逐项建框。因此每幕规划时就应避免生成无法由独立矩形完整包围的交错构图。

阶段 0 的生产准备命令为：

```powershell
<ENV_PY> scripts/prepare_draft_agent_task.py contentDrafting `
  --draft-root <D:\SRTWhiteboard\drafts\草案ID> `
  --content-input <whiteboard-content-input-v1.json>
```

用户要求实质修改时，coordinator 先冻结 revision request，再以成对参数创建新 attempt；它们与 `--content-input` 互斥：

```powershell
<ENV_PY> scripts/prepare_draft_agent_task.py contentDrafting `
  --draft-root <D:\SRTWhiteboard\drafts\草案ID> `
  --revision-request <whiteboard-content-revision-request-v1.json> `
  --base-content-draft <上一版-candidate.content-draft.json>
```

成功 stdout 是 `whiteboard-draft-agent-prepare-v1`，包含 `dispatchAudit`、`spawnPackage`、`formalPublished:false` 与 `approvalWritten:false`。spawn package 包含冻结 task/role 的绝对路径和 SHA、唯一 attempt 根、result 路径、allowed outputs 与最小 `spawnAgentCall`；`hostSpawnExecuted:false` 表示只准备、未派发。coordinator 收到后应立即调用宿主，不得重新阅读 Python 源码研究如何建 task，也不得把 prepare 当作 candidate 完成或用户批准。

### 人工确认前的只读草案校验

coordinator 在生成 topic/text 审阅 artifact 前，必须通过标准输入执行只读校验，不能为了调用校验器先把未确认正文写成临时 JSON：

```powershell
# JSON 由 coordinator 以 UTF-8 写入子进程 stdin；此命令本身不创建输入文件
<ENV_PY> scripts/validate_content_draft.py --stdin
```

该命令仅在内存调用 current `validate_content_draft()` 和 `build_source_package()`，用于同时检查草案合同和确定性派生是否成立。成功时向 stdout 输出单个结构化 JSON，只包含 content draft identity、合同/模式/策略/target、cue 数、scene 数、`valid: true` 与 `writesPerformed: false`；不输出 topic/body、旁白正文、图片提示词、绝对路径或秘密。它不调用文本模型或 provider，不读取本地 secret 配置，不运行 `prepare_source.py`，不创建 source 准备包或正式项目，也不写任何批准记录。

`--stdin` 与 `--draft` 互斥。`--draft <confirmed-content-draft.json>` 只允许读取已经持久化的已确认输入或测试 fixture；人工确认前的当前对话只能使用 `--stdin` 或直接调用纯函数。无效 UTF-8、无效 JSON、草案合同错误、文件读取错误和参数错误均以退出码 2 拒绝，并只输出不含输入值或路径的结构化错误码。

`writesPerformed: false` 只是机器可读声明，不能单独作为零写入证据。自动测试必须在 C 盘 `TemporaryDirectory` 内记录调用前后的完整目录项，并对每个文件比较大小和 SHA-256；只有前后文件树完全一致，才证明校验没有生成草案文件、source 包、项目目录、批准记录、缓存或其他副作用。

candidate 通过只读校验后，由 coordinator 调用确定性 renderer；renderer 只写 draft scope 的 `reviews/`，不得写 attempt 或正式项目：

```powershell
<ENV_PY> scripts/render_content_review.py `
  --draft-root <D:\SRTWhiteboard\drafts\草案ID> `
  --candidate <attempt\candidate.content-draft.json> `
  --workspace-config <可选-workspace.local.json>
```

coordinator 只消费 renderer 的结构化短摘要和 review 路径，不再读取 Markdown 全文。

## 自然中文旁白合同（`natural-spoken-zh-v1`）

`natural-spoken-zh-v1` 是阶段 0 的生成与人工审稿规则，不是 `whiteboard-content-draft-v1` 的新字段，也不进入 JSON、schema 或 content identity。它只约束面向观众朗读的 `narrationCues[].text`；`coreIdea`、`visualSubject`、`imagePrompt`、JSON、CLI、验证报告和审计字段仍按各自的结构化合同编写，不能为了“说人话”而删掉机器消费所需的明确约束。

### rewritePolicy 边界

| 模式 | 是否启用旁白润色 | 允许的处理 | 禁止的处理 |
|---|---|---|---|
| `text + preserve` | 否 | Unicode/换行规范化、拆 cue、极少量不改变语义的口语标点 | 换声口、增删信息、改写句意、补例子或把书面原文强制口语化 |
| `text + polish` | 是 | 在事实与关系账本不变的前提下，局部改成自然可听的现代中文 | 改变数字、人物、结论、责任主体、因果方向、完成态、证据强度或不确定性 |
| `topic + generate` | 从首稿启用 | 围绕已确认主题创作完整、具体、可朗读的旁白 | 先生成模板文章再整篇换声口；把未知、推断或示例写成已核验事实 |

传统 `inputMode=srt` 不自动启用本合同，也不因旁白审稿而改写用户提供的字幕。用户另行明确要求重写 SRT 时，必须先展示完整改稿并重新确认，不能在 TTS 或字幕阶段静默处理。

### 正向写作目标

- 旁白写给人耳，不写成文章摘要。使用自然、简洁的现代中文，像熟悉主题的人面对镜头解释。
- 直接进入具体问题、场景、动作或判断；优先使用清楚的主语、直接动词和可见对象，少用连续抽象名词撑起句子。
- 每个 cue 只推进一个新信息，并与所属 scene 的单一核心表达一致。相邻 cue 不得只换一组近义词重复同一结论。
- 句长和节奏允许自然变化；该短则短，需要解释时可以展开。标点应服务于朗读、停顿和字幕理解，不用破折号、冒号或排比制造“写得很像文案”的表演感。
- 具体信息优先于空泛总结；已经说清的关系不再追加“这说明了什么”“这背后意味着什么”式旁白。
- `polish/generate` 直接讲主题中的人物、事件、条件和判断，不暴露改稿、提示词或素材来源。避免“原文认为／原文提到／按照原文”“在这段叙述里”“这份材料说明／无法回答”“正文中”等元话语；需要保留归因或不确定性时，改写为具体主体的判断、当时条件或“目前无法下结论”。只有主题本身是文本解读、作品分析、原文对比，或用户明确要求讨论素材来源时才保留这类表述。
- 需要举例时，例子必须与主题相关，并明确保持示例属性；不得虚构来源、数据、个人经历或把常识性说明包装成已核验研究结论。

### 高风险模式与处理原则

以下内容是编辑信号，不是禁词表。单次出现且承担真实逻辑时可以保留；同类句壳连续出现、可以预判下一句形状或没有新增信息时，才需要局部改写：

- 二元纠正壳：`不是 A，而是 B`、`不只是 A，更是 B`、`真正重要的是`。A 只是铺垫时直接说 B；A、B 都重要时写清两者的具体关系。
- 仪式性顺序：`首先／其次／最后`、`先 A，再 B`、`第一步／第二步`。真实操作顺序应保留；只为显得有条理时删掉路线词，直接说动作、条件或结果。
- 助手路线词：`下面我们来`、`接下来我们看看`、`值得注意的是`、`总的来说`、`希望这能帮到你`。通常直接进入实际内容。
- 加工痕迹词：`原文认为`、`根据这段材料`、`在这段叙述里`、`正文提到`、`这份材料无法回答`。在 `polish/generate` 中应直接说清人物、条件、动作和判断边界，不把素材来源当作口播主体；文本解读、作品分析和用户明确要求的原文对比除外。
- 抽象拔高和宣传腔：`本质上`、`底层逻辑`、`赋能`、`开启全新篇章`、泛化的积极结论。改为可观察的动作、影响、条件或限制。
- 假互动和假口语：无目的的 `你觉得呢`、`你有没有类似经历`，以及刻意堆叠的 `其实吧`、`你知道吗`、`说白了`、`划重点`。只有明确需要互动、CTA 或人物声口时才保留。
- 过度整齐：每条 cue 长度、句式、起手和落点几乎相同，或连续三项使用同一语法与情绪强度。通过删掉空项、合并重复信息、拆开过载长句或补足原本已有的具体关系调整，不能靠忙碌的同义词替换制造变化。

### 两遍回读

`contentDrafting` child 在提交 candidate 前必须完成两遍回读：

1. **保真与内容回读**：逐项核对原始 topic/body、人物、事实、数字、时间、结论、限定、不确定性、因果强度和责任主体；确认总内容量适合 `targetDurationSeconds`，cue→scene 连续且每条 cue 都有新信息。`text + preserve` 在此遍后停止风格处理。
2. **口播与残留回读**：完整朗读 `narrationCues`，只检查并局部修正拗口句、抽象名词堆叠、重复解释、助手路线词、无目的收尾、同型句壳过密和节奏过匀。原稿已经自然时不强行改写；不得为了“人味”添加口头禅、错别字、emoji、虚构细节、个人经历或多余情绪。

这两遍回读不能替代用户的内容确认，也不能在确认后继续发生。内容草案一旦获批，后续 provisional SRT、TTS、narration SRT、字幕和成片只消费已确认文本并处理排时、分段、换行与媒体。任何旁白文字变化都必须回到阶段 0，更新草案并重新经过对应人工关卡。

## `whiteboard-content-draft-v1`

草案 JSON 的顶层字段固定为明确 allowlist：

```json
{
  "schemaVersion": 1,
  "contractVersion": "whiteboard-content-draft-v1",
  "inputMode": "topic",
  "topic": "为什么人会拖延",
  "body": null,
  "rewritePolicy": "generate",
  "targetDurationSeconds": 60,
  "voiceoverMode": "edge-tts",
  "narrationCues": [
    {
      "cueId": "cue-001",
      "sceneId": "scene-01",
      "text": "你有没有过这样的经历？明知道事情很重要，却总想再等一会儿。"
    }
  ],
  "scenes": [
    {
      "sceneId": "scene-01",
      "name": "拖延的表象",
      "coreIdea": "重要任务面前反复推迟",
      "visualSubject": "人物面对任务清单却转向轻松活动",
      "imagePrompt": "暖米黄纸张上的极简手绘场景……"
    }
  ]
}
```

硬校验包括：

- `schemaVersion=1`，`contractVersion=whiteboard-content-draft-v1`；未知敏感字段拒绝或至少不得持久化。
- `topic` 与 `body` 分开保存，不能用其中一个覆盖另一个。topic 模式必须有非空 topic；text 模式必须有非空 body。
- 文本统一 Unicode NFKC、CRLF→LF 并移除首尾空白；不得静默改变正文语义。
- topic 建议上限 200 Unicode 字符；body 上限 128 KiB UTF-8。
- `targetDurationSeconds` 必须是 15–600 的有限数字；布尔值、NaN、无限值和范围外值拒绝。用户未指定时，Codex 可建议 60 秒，并与其他缺失配置一次性展示等待确认；用户已指定合法值时不得再问一次。
- narration cue 文本非空；`cueId` 从 `cue-001` 起连续且唯一；`sceneId` 必须存在。
- scene 从 `scene-01` 起连续编号；每个 cue 只属于一个 scene，scene cue 必须连续，不能离开后又返回旧 scene。
- 每个 `imagePrompt` 会作为独立场景请求的一部分发送，必须在本条内完整写出画布、纸张、线稿、配色、按该幕实际语义确定的造型锚点、构图、留白和禁字/禁水印约束；还必须明确一个核心视觉命题、默认一个主要视觉簇、必要时一个空间独立辅助簇、簇间真实纸面留白，并禁止簇间嵌套、遮挡与贯穿性连续结构。必须连续构图的概念合并为一个簇。不得依赖其他 scene 或先前图片，不得使用“延续”“沿用”“同上”“上一幕”“参照前图”等跨请求指代。`globalPrompt` 的确定性拼接不能替代单幕提示词的自包含性。
- 不接受 API Key、Cookie、Token、临时 URL、PID、本机绝对路径或完整模型内部响应；JSON 和 manifest 都不得成为凭据载体。

三种 rewritePolicy 必须同时遵守上面的模式边界：`text + preserve` 保持原文与声口，`text + polish` 展示完整新稿和实质改动，`topic + generate` 从首稿直接生成自然口播。不确定事实不能伪装成已核验事实；确认前不得准备 source 或建项。

仓库示例分别见：[`topic + generate`](../examples/topic-habit-loop-content-draft.json)、[`text + preserve`](../examples/text-habit-loop-content-draft.json) 和 [`text + polish`](../examples/text-habit-loop-polish-content-draft.json)。示例只证明结构与策略边界，不代表用户已确认内容，也不替代真实朗读和人工审稿。

## 确定性 source package

只有当前草案已获用户明确确认后才执行：

```powershell
<ENV_PY> scripts/prepare_source.py `
  --draft <已获用户确认的-content-draft.json> `
  --output-dir <D:\SRTWhiteboard\drafts\项目名>
```

成功原子发布：

```text
<draft-dir>/
  input.json
  source.srt
  generation-plan.json
  manifest.json
```

成功命令输出为：

```text
CONTENT_DRAFT_IDENTITY=<64位 sha256>
SOURCE_INPUT=<draft-dir>/input.json
SOURCE_SRT=<draft-dir>/source.srt
GENERATION_PLAN=<draft-dir>/generation-plan.json
SOURCE_MANIFEST=<draft-dir>/manifest.json
INPUT_MODE=<topic|text>
TARGET_DURATION_SECONDS=<...>
CUE_COUNT=<...>
SCENE_COUNT=<...>
```

`input.json` 保存规范化后的 allowlist 内容；`manifest.json` 至少绑定 contract version、normalized input SHA、narration cue identity、source SRT SHA、generation plan SHA、target duration、inputMode/rewritePolicy 和工具版本。审计字段不得含秘密；manifest 内的文件引用必须是准备包内相对路径，不保存本机绝对路径。

相同规范化草案必须产生相同 canonical JSON hash、source SRT 和 generation plan。创建时间等非确定性审计字段不得进入内容 identity。失败候选在本次工作目录内保留或清理，但不得覆盖上一次有效准备包。

### Provisional SRT 排时

排时只消费已确认 narration cue，不在脚本中重新改写或分句：

1. 对有效中文、字母和数字计算朗读权重；标点不计主要权重，句末停顿加入固定小权重。
2. 在每条 cue 的最短可读时长约束下，按权重分配 target。
3. 使用整数毫秒和累计边界；cue 从 0 连续排列，无人为字幕空档。
4. 最后一条 cue 的 `endMs` 必须精确等于 `targetDurationSeconds * 1000`，不能逐 cue 四舍五入后累加漂移。
5. 立即使用共享 `parse_srt()` 严格 round-trip 验证。
6. generation plan 的 scene `subtitleRange` 与 `sceneDurationMs` 从已生成 cue 边界派生，不信任草案中的重复毫秒字段。

该 SRT 是 Edge 完整旁白批准前的 provisional source timeline，不是真实语音时间轴。完整旁白获批后，current canonical audio timeline 才成为正式 timing plan；不得把 TTS 实际时长写回图片 generation plan。

## 正式项目创建与传统 SRT 兼容

topic/text 准备包创建正式项目时使用：

```powershell
<ENV_PY> scripts/create_project.py --name <项目名> `
  --srt <draft-dir\source.srt> `
  --plan <draft-dir\generation-plan.json> `
  --source-input <draft-dir\input.json> `
  --source-manifest <draft-dir\manifest.json> `
  --voiceover-mode edge-tts
```

`--source-input` 与 `--source-manifest` 必须成对出现，且只用于新建项目。创建时必须重新校验 input/manifest/SRT/plan 的全部 hash 与绑定关系，不能只复制文件。项目冻结：

```text
source/input.json
source/source-manifest.json
source/source.srt
```

`project.json` 可增加 `contentSource` 绑定 input/manifest hash，但实际下游 source 仍是 `source/source.srt`。续接项目只读取项目内冻结证据；不得回到外部 draft 目录重新推断 current state。创建失败只回滚本次唯一新项目目录，保留准备包供修复后重试。

传统 SRT CLI 与语义保持不变：

```powershell
<ENV_PY> scripts/create_project.py --name <项目名> `
  --srt <字幕.srt> --plan <已确认策略.json> --voiceover-mode disabled

<ENV_PY> scripts/create_project.py --name <项目名> `
  --srt <字幕.srt> --plan <已确认策略.json> --voiceover-mode edge-tts
```

旧 v1/v2 项目没有 `contentSource` 时继续按传统 SRT 项目读取，不静默改写或强制升级。Disabled/Edge 的字幕权威来源、样音、人工批准、渲染和最终交付合同保持不变。

## stale、恢复与失败语义

- topic/body/rewritePolicy/target/narration cue/scene mapping 改变：content draft、source SRT、generation/timing plan、voice plan/segments/audio/timeline/narration SRT、annotation、场景视频、字幕、final 与最终批准全部重新判定。
- 只有 imagePrompt 改变且 narration cue/scene boundary 不变：音频可复用；generation plan、图片及视觉下游 stale。
- provisional SRT 排时算法版本改变：source identity/timing 重新判定；synthesis identity 未变的已验证 Edge segments 可按现有规则复用，但 duration decision、timing plan 与下游重新验证。
- 准备包任一文件或 manifest hash 被篡改：正式建项拒绝，不得修补 hash 后静默继续。
- source input 改变后，不得复用旧项目 current identity。
- 所有候选先在本次工作目录生成并验证，再同卷原子发布；失败候选不能覆盖 current。

退出码沿用统一合同：

| 码 | 本功能含义 |
|---:|---|
| 0 | 准备、验证或创建成功 |
| 1 | 独立环境/批处理操作失败（若相应 CLI 使用） |
| 2 | content draft、参数、SRT、plan、manifest 或绑定无效 |
| 3 | Edge 外部请求失败或重试耗尽；确定性内容准备本身不应访问网络 |
| 4 | 后续 FFmpeg、ffprobe、字体、ASS、WAV 或媒体验证失败 |
| 5 | stale、identity 不匹配或缺少人工批准 |

## 验收与报告边界

自动 fixture 应覆盖合法/非法 rewritePolicy、规范化、时长边界、cue/scene 连续性、确定性 hash/SRT/plan/manifest、严格 SRT round-trip、文件篡改拒绝、传统 SRT 无回归，以及 topic draft → source package → formal project。自动测试只能使用 fake provider/固定 WAV，不调用真实 Edge、真实图片 provider 或任何外部付费模型，也不得读取本地凭据。

fixture PASS 不证明以下事项已经通过：

- 用户已经确认某份具体内容草案；
- 微软 Edge 服务当前可用或某个 voice/rate 已被用户接受；
- 真实图片 provider 可用或线稿审美合格；
- 完整旁白、最终字幕 contact sheet 或最终成片已经过人工确认。

未实际执行的真实 provider 和人工关卡必须明确写为 SKIP、BLOCKED 或待用户确认，不能声称 PASS。内容与制作方案联合确认只允许进入 source 准备、确定性 round-trip 与一致时的正式建项；它不替代样音、完整旁白试听与真实时长批准、线稿、annotation review bundle、scene review bundle、正式字幕烧录/contact sheet 或最终成片批准。annotation 的内容/区域/时序在一次持久化联合批准中确认，全部正式单幕在一次持久化有序 bundle 批准中确认；clean master 只作为技术中间工件，不设独立人工确认。
