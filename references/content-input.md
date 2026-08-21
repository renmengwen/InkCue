# 主题 / 正文内容入口合同

本文说明阶段 0 的 `topic | text` 首版入口、`whiteboard-content-draft-v1`、artifact-first 审阅与修订、内容与制作方案联合确认、确定性 source package、正式项目 provenance、stale/恢复与验收边界。传统 `inputMode=srt` 路径保持一次独立的模式/语义分镜确认；其 `voiceoverMode=disabled | edge-tts | minimax`、严格 SRT、人工关卡和最终交付语义不得回归。

## 首版能力边界

外部输入模式固定为：

```text
inputMode = srt | topic | text
rewritePolicy = preserve | polish | generate
```

合法组合固定为：

| inputMode | 必要内容 | rewritePolicy | 首版 voiceoverMode 来源 |
|---|---|---|---|
| `srt` | 现有严格 SRT | 不适用 | 默认读取 `activeProvider`；明确静音时为 `disabled` |
| `topic` | 非空主题 | 仅 `generate` | 自动读取 `activeProvider` |
| `text` | 非空正文 | `preserve | polish` | 自动读取 `activeProvider` |

`topic + preserve`、`topic + polish`、`text + generate` 以及非 SRT + `disabled` 均须拒绝。topic/text 首版不使用估算阅读时长作为最终权威时钟；target 只用于内容预算与 provisional SRT，获批的真实音频 provider timeline 才接管正式时钟。

首版不接入通用文本模型 provider。旁白稿、cue、scene 和画面建议由宿主真实派发的 `contentDrafting` child 生成 candidate，再由 coordinator 校验并确定性生成 Markdown 审阅 artifact；`prepare_draft_agent_task.py` 只冻结 attempt 和宿主中立 task descriptor，`content_source.py` 与 `prepare_source.py` 只做确定性规范化、校验、排时、hash、持久化和派生文件。上述脚本都不判断宿主能力、不生成宿主调用参数、不发起文本模型请求、不读取外部凭据、不自行改写或批准草案，也不创建正式项目。

## 阶段 0 内容与制作方案联合关卡

topic/text 的权威顺序是：

1. 接收原始主题或正文，并冻结 `rewritePolicy` 与 `targetDurationSeconds`；`voiceoverMode` 始终从 skill 根目录 `config/voice-providers.local.json` 的 `activeProvider` 派生。用户无需提供或选择 Edge TTS/MiniMax，编排层不得询问“旁白方式”。派生结果只可规范化为 `edge-tts` 或 `minimax`，并写入冻结的 content input/review，供用户知情查看。只有用户明确要求静音时，才走传统 SRT 的 `disabled` 显式入口。
2. coordinator 运行 `prepare_draft_agent_task.py contentDrafting` 冻结 attempt 和 `preparedTask`，再直接根据当前宿主状态调用 `spawn_agent` 或 `followup`；脚本不参与 dispatch/fallback 决策。首次草案默认使用短上下文 child；真实派发不可用时才由具备相同能力的 coordinator fallback。所有路径使用同一 task/result 合同。
3. coordinator 重验 result、SHA 与 candidate 合同后，从 `candidate.content-draft.json` 确定性生成不可变 Markdown 审阅 artifact。主窗口只发送文件链接、完整 identity、cue/scene 计数和短摘要，不把长正文、逐幕提示词或整份 Markdown 读回、转述或粘贴到聊天。
4. **停止并等待用户明确确认 current `contentDraftIdentitySha256`，完成“内容与制作方案联合确认”。** 这一次确认同时覆盖内容草案与模式/语义分镜策略；未回复、此前笼统授权、技术校验通过或“用户没有反对”都不是批准。
5. 用户要求实质修改时，把意见冻结为 revision request，绑定 current base identity并创建新 attempt。attempt 是版本边界，不是执行者边界：上一 attempt 的 `contentDrafting` child 仍存在、idle、上一结果 completed 且 role contract 兼容时，优先 followup 原 child读取新 task/base/revision；原 child 不可用、失败、role 改变、修订升级为全面独立重写或用户明确要求换执行者时才 spawn 新 child。新 candidate 重新校验并生成新 identity、新 Markdown；旧版保留但判为 stale。
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

revision request 固定 `schemaVersion: 1`，只保存用户本轮真实要求，不复制上一版全文；`globalInstructions`、`cueChanges`、`sceneChanges` 至少一项非空，单独填写 `mustPreserve` 不构成修改。`baseContentDraftIdentitySha256` 必须等于派发时的 current candidate identity，否则以 stale 拒绝。实质修改一律创建新 attempt，上一版 candidate/review 不覆盖、不删除，并在新版本成为 current 时判为 stale；但新 attempt 不自动创建新 child。上一执行者满足复用条件时优先 followup，且仍必须从新的冻结 task/base/revision 与 SHA 重新建立 current 事实，不能只依靠对话记忆。任何文案、cue、scene、提示词或用户要求变化都必须冻结新 revision request 并创建新 attempt。

### 分幕与视觉拓扑合同

`contentDrafting` 的 scene 边界、独立视觉簇、贯穿性结构、annotation 可消费性和
`imagePrompt` 写法统一由 [`prompt-writing.md`](prompt-writing.md) 定义。本文只规定
阶段 0 的 schema 与确认边界；该视觉规范不增加字段，也不改变联合确认关卡。

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

成功 stdout 是 `whiteboard-draft-agent-prepare-v2`，包含 `configuredAgentConcurrency`、宿主中立 `preparedTask`、`formalPublished:false` 与 `approvalWritten:false`。descriptor 只列冻结 task/role 的绝对路径和 SHA、唯一 attempt 根、result 路径、allowed outputs 与 required capabilities，不包含 `spawnAgentCall`、child slots、fallback 或 agentId。coordinator 收到后直接使用宿主协作工具；prepare 既不代表真实派发，也不代表 candidate 完成或用户批准。

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

旁白写法、三类 rewritePolicy 边界、两遍回读和“批准后不得继续改写”统一见
[`phase-0-content.md`](phase-0-content.md)。`natural-spoken-zh-v1` 只约束
`narrationCues[].text`，不是新的 schema/identity 字段；本文后续只定义
`whiteboard-content-draft-v1` 的机器合同。

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
- 每个 `imagePrompt` 会作为独立场景请求的一部分发送，必须在本条内完整写出画布、纸张、线稿、配色、按该幕实际语义确定的造型锚点、构图、留白和禁字/禁水印约束；还必须明确一个核心视觉命题、必要时 2–3 个可独立揭示区域、区域间真实纸面留白，并禁止跨区域贯穿性连续结构。必须不可分割的连续构图才合并为一个簇。不得依赖其他 scene 或先前图片，不得使用“延续”“沿用”“同上”“上一幕”“参照前图”等跨请求指代。`globalPrompt` 的确定性拼接不能替代单幕提示词的自包含性。
- 不接受 API Key、Cookie、Token、临时 URL、PID、本机绝对路径或完整模型内部响应；JSON 和 manifest 都不得成为凭据载体。

### `imagePrompt` 到正式 `prompt` 的确定性映射

`imagePrompt` 只属于 `whiteboard-content-draft-v1` 内容草案；正式 `planning/generation-plan.json` 的 scene 字段名固定为 `prompt`。用户确认 current 内容草案后，coordinator 通过 `prepare_source.py` / `scripts.content_source.build_generation_plan()` 做逐幕确定性映射：

```text
content draft scenes[i].imagePrompt
  → formal generation plan scenes[i].prompt
```

映射保持同一 scene 的提示词文本和顺序，不让 child、provider 或人工复制时另行改写。正式 generation plan 不得保留 `imagePrompt`，内容草案也不得把该字段提前改名为 `prompt`；传统 SRT 的 storyboard candidate 则从一开始就使用正式 `prompt` schema。后续 provider 请求使用 `globalPrompt + scene.prompt` 的确定性组合，但 `globalPrompt` 仍不能替代单幕 prompt 的自包含约束。

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
  --source-manifest <draft-dir\manifest.json>
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
  --srt <字幕.srt> --plan <已确认策略.json>
```

旧 v1/v2 项目没有 `contentSource` 时继续按传统 SRT 项目读取，不静默改写或强制升级。新建项目的旁白 provider 唯一读取 `config/voice-providers.local.json` 的 `activeProvider`；topic/text 的 content input 缺少 `voiceoverMode` 时由该配置自动补入，调用方传入不一致值则拒绝。需要静音时才显式使用 `--voiceover-mode disabled`。Disabled/Edge/MiniMax 的字幕权威来源、样音、人工批准、渲染和最终交付合同保持不变。

## stale、恢复与失败语义

跨阶段的 current binding、stale 传播、attempt 恢复、自动重试和
`unknown_external_outcome` 统一由
[`recovery-and-identity.md`](recovery-and-identity.md) 定义。下列条目仅说明阶段 0 的
受影响产物，不得作为另一套 retry 或 identity 规则。

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
