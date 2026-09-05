# 主题 / 正文内容入口合同

本文说明阶段 0 的 `topic | text` 首版入口、`schemaVersion=1` content draft、artifact-first 审阅与修订、内容与制作方案联合确认、确定性 source package、正式项目 provenance、stale/恢复与验收边界。传统 `inputMode=srt` 路径保持一次独立的模式/语义分镜确认；其 `voiceoverMode=disabled | edge-tts | minimax | doubao`、严格 SRT、质量 Gate 和最终交付语义不得回归。

## 正常新任务的 fast path

对正常 `topic | text` 新任务，入口读取 `SKILL.md` 后即执行入口中已记录的一次
`python <SKILL_ROOT>\scripts\prepare_env.py --bootstrap-content-draft ...` bootstrap。该单次调用
已完成 workspace-access、环境 check、provider/preset/input/draft/task fast-prepare 并输出紧凑
descriptor；派发前不得预先另跑两条 `prepare_env`，也不得先落 `body-file`。派发前不以阅读本文件、
`phase-0-content.md`、`subagent-orchestration.md` 或 `prompt-writing.md` 为前置；也禁止
memory lookup、整份/分段 reference 重读、源码/tests/examples/CLI `--help` 搜索及任何额外
探路。descriptor 已携带 canonical schema/skeleton、可原样派发的 prompt 与校验/
materialize argv；`nextAction=spawn_now` 即直接 direct spawn。

这些 reference 只在 child 已真实派发后，按 candidate 校验、result materialize、受限预项目、
pending approval 或具体 revision 的当前需要读取相应小节。该时序优化不改变输入、
批准或恢复合同，也不新增 Gate。

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

`topic + preserve`、`topic + polish`、`text + generate` 以及非 SRT + `disabled` 均须拒绝。topic/text 首版不使用估算阅读时长作为最终权威时钟，也不按固定字数或“每秒字符数”拒绝、反复压缩 candidate。target 只用于内容目标与 provisional SRT；整轨请求以 prompt 中的总目标时长和每幕 provisional 时间窗口控制节奏，生成后由真实 provider duration、原生词级字幕与既有 duration review/Gate 接管正式时钟。可证明的 provider 请求长度或单次时长技术上限仍须遵守。

首版不接入通用文本模型 provider。旁白稿、cue、scene 和画面建议由宿主真实派发的 `contentDrafting` child 生成 candidate，再由 coordinator 校验、确定性生成 `result.json` 与 Markdown 审阅 artifact；bootstrap 已冻结 attempt 和宿主中立 task descriptor，并直接给出可派发的 `agentPrompt`、candidate 校验 argv 与 result materialize argv。`prepare_draft_agent_task.py` 保留为兼容、恢复或特殊路径接口。`content_source.py` 与 `prepare_source.py` 只做确定性规范化、校验、排时、hash、持久化和派生文件。上述脚本都不判断宿主能力、不发起文本模型请求、不读取外部凭据、不自行改写或批准草案，也不创建正式项目。

## 阶段 0 内容与制作方案联合关卡

topic/text 的权威顺序是：

用户明确要求“新任务”或“不要沿用旧任务”时，coordinator 立即停止旧项目/旧 draft 的恢复推断，为阶段 0 分配新的 `draft-root`，后续只走新建项目命令；仅在用户明确指定续接既有项目时才允许恢复。该优先级不增加状态字段或恢复协议。

1. 正常 topic/text 新任务直接运行一次入口已记录的 `prepare_env.py --bootstrap-content-draft`。它在一次调用中完成 workspace-access、环境 check、provider/preset/input/draft/task fast-prepare，输出紧凑 descriptor 并冻结 active provider、具体 preset、managed input、唯一不覆盖既有内容的 `draft-root` 与 `preparedTask`；用户明确要求“新任务”时绝不恢复或覆盖旧任务。派发前不另跑两条 `prepare_env`、provider status/recommend，不先落 `body-file`、不手写 `content-input.json`、不试名称，也不做 memory/reference/`prompt-writing.md` 预读。仅当用户主动要求浏览模板时才运行 `visual-style-catalog`；目录不新增批准选择轴。BGM、后续模式和生图方式仍只由联合动作原子冻结。
2. descriptor 为 `nextAction=spawn_now` 时，coordinator 使用其 `agentPrompt` 和 `dispatchPolicy` 立即调用 `spawn_agent`。首版 `contentDrafting` 固定 `fork_turns="none"`、`reasoning_effort="medium"`，从当前宿主可用模型中选择满足文本能力的最快者，不继承主任务上下文或 `high` effort，也不硬编码单一模型。task 自带 schema/skeleton 与全部输入定位，无需搜索 schema 或重读 reference；当前用户要求主代理只编排时，真实派发失败即准确停止，不允许 coordinator fallback 生成 candidate。
3. child 只读取冻结 task/role/input 并按它们一次生成 `candidate.content-draft.json`（及可选 `agent.log`），不得猜 schema，不写 `result.json`。candidate validator 一次返回完整结构错误清单；首次结构失败只允许同 attempt followup 原 child 一次，要求按完整清单和 schema/skeleton 做一次全量归一。若仍结构失败，直接换更强的短上下文 child，不逐字段反复修，也不因固定字符预算反复改写正文。
4. child 返回 `candidate_ready` 后，coordinator 只执行 descriptor 的 `contentDraftFinalizeArgv`，即一次 `coordinator_cli.py finalize-content-draft`，由该确定性动作完成 candidate 校验、result materialize、不可变 Markdown 审阅 artifact、source package 和唯一 `initialApproval.status=pending` 预项目创建。不得搜索脚本、猜 source 目录或项目名，也不复制新的 Gate、identity 或状态机；成功摘要为 `status=待确认`、`technicalStatus=PASS`、`nextGate=initial_content_plan_approval`。预项目只能承载阶段 0 review、草案修订和联合动作；阶段 0 不生成或试听样音，完整旁白、生图、annotation、render、merge、burn、mux 与 final 都必须调用 pending guard。
5. 固定生图方式时展示 BGM × 后续模式共 4 个完整通过句；仅登录态 `image_gen` 与已配置图片供应商同时可用时展示 8 个。active provider 不进入选项。传统 `disabled` SRT 使用“字幕与分镜方案通过……”语义。
6. 用户复制当前完整句或回复编号；parser 只接受当前选项及规定修改前缀，不猜自由文本，并只绑定 current `contentDraftIdentitySha256`。项目层重验 pending、identity、能力和组合后原子提升，任一失败不部分写 BGM、agent、生图方式或批准。
7. 用户要求实质修改时，把意见冻结为 revision request，绑定 current base identity并创建新 attempt。content 变化使受影响 source 和下游 stale；voice/rate 变化使 full 和下游 stale，旧批准不能静默复用。

内容准备脚本不批准草案，也不能从 JSON 或 Markdown 推断用户同意。联合确认前允许存在受限预项目，但 `initialApproval.status=pending` 必须由 loader 暴露为 `pending_initial_approval=true`，并由下游入口 fail-closed。技术验证不能冒充联合批准；不因此新增第二套 preview identity 或 manifest。

联合确认后，`agentApprovalEnabled=false` 或字段缺失继续使用人工 full/final Gate；为 `true` 时，后续 full/final 仅在全部严格技术证据 current 后以明确 `approvalBasis/reviewBasis` 推进，不能声称 AI 完整听音。视觉 Gate 仍实际检查 current artifact，`reviewPolicy` 确定性派生为 `agent_first`。

即使启用代理批准，`unknown_external_outcome` 后可能重复的新外部请求、冻结计划之外的新费用/凭据/服务授权、版权授权，以及必须实质改变已冻结用户意图的修订，仍须单独询问用户。计划内正常有界调用和常规返工不打断用户；不为此增加 identity、manifest、状态机或专用恢复协议。

### Artifact-first 审阅与修订

`candidate.content-draft.json` 是阶段 0 唯一机器权威源。Markdown 只能由 coordinator 在 candidate 通过只读校验后确定性渲染，不得由 child 自由撰写，也不得被下游解析为 source、SRT、plan、identity 或批准输入。

```text
<workspace>/drafts/<draft-id>/
  reviews/
    content-review-<contentDraftIdentity前12位>-<模板配方SHA前12位>.md
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

review 文件名必须使用完整 content identity 和模板配方 SHA 各自的前 12 位，避免只改模板时与旧审阅文件冲突；正文至少展示完整 content identity、当前模板名称/ID、输入模式、rewritePolicy、target、voiceoverMode、原始 topic/body、完整旁白、实质改动说明、cue→scene、每幕核心表达/画面主体/`imagePrompt`、provisional SRT 与权威时钟说明，以及 `pending` 审阅状态。相同 canonical candidate 必须产生相同 Markdown 字节；能力相关选项由 renderer 的短结构化摘要动态返回，避免把宿主能力写进 content identity。Markdown 的 `pending` 不是项目批准字段。

主窗口交付 review 时不得用整文件读取把内容回灌到主上下文；只消费渲染器的短结构化摘要并发送可点击文件链接。用户可用稳定的 `cueId`、`sceneId` 或全局说明提出修改；只有无法定位时才按需读取 review 的局部片段。

用户修改意见由 coordinator 冻结为 `schemaVersion=1` revision request：

```json
{
  "schemaVersion": 1,
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

正常 topic/text 新任务的阶段 0 生产准备只有一次 fast-prepare：

```powershell
python <SKILL_ROOT>\scripts\prepare_env.py --bootstrap-content-draft `
  --workspace <workspace-root> `
  --new-draft-label <label> `
  (--topic <text> | --body <正文>) `
  --rewrite-policy <generate|preserve|polish> `
  --target-sec <15..600> `
  [--visual-style-preset <具体模板ID>]
```

该单次 bootstrap 已完成 workspace-access、环境 check、provider/preset/input/draft/task fast-prepare 并输出紧凑 descriptor；`--visual-style-preset` 缺失时自动推荐并冻结具体 ID，用户明确提供时以该 ID 为准。正常新任务不得预先另跑两条 `prepare_env`，也不得先落 `body-file`。只有用户主动要求浏览模板时才单独运行只读 `coordinator_cli.py visual-style-catalog`。旧 `prepare_draft_agent_task.py`、`--body-file` 及 `--draft-root --content-input` 接口保留兼容，供恢复或特殊路径使用，不是正常 topic/text 新任务快路径。

用户要求实质修改时，coordinator 先冻结 revision request，再以成对参数创建新 attempt；它们与 `--content-input` 互斥：

```powershell
<ENV_PY> scripts/prepare_draft_agent_task.py contentDrafting `
  --draft-root <D:\SRTWhiteboard\drafts\草案ID> `
  --revision-request <revision-request.json> `
  --base-content-draft <上一版-candidate.content-draft.json>
```

若用户变更模板，上述修订命令必须增加 `--visual-style-preset <具体模板ID>` 并创建新 attempt；传统 SRT 的 `storyboardPlanning` 也通过同名参数在 attempt 前冻结具体模板。不得原地修改已存在的 task、candidate 或 review。

成功 stdout 使用 `schemaVersion=1` 和 `operation=prepareDraftAgentTask`，包含 `configuredAgentConcurrency`、宿主中立 `preparedTask`、`formalPublished:false` 与 `approvalWritten:false`。descriptor 除冻结 task/role 的绝对路径和 SHA、唯一 attempt 根、candidate/result 路径、allowed outputs 与 required capabilities 外，还必须直接给出：

- `agentPrompt`：只含冻结定位、SHA、允许 attempt 和固定返回协议，可原样用于宿主派发；
- `candidateValidationArgv`：使用已捕获绝对 `ENV_PY` 与绝对脚本路径的纯本地 candidate 校验命令；
- `resultMaterializeArgv`：由 coordinator 根据冻结 task 和 candidate SHA 确定性生成 `result.json` 的命令；
- `nextAction=spawn_now`（或等价稳定字段）：告诉 coordinator 无需继续探路即可派发。
- `candidateSchema` 与 `candidateSkeleton`：该 role 的 canonical 输出 schema 和可直接填充的完整骨架，随 task 冻结并由 SHA/identity 边界保护。

descriptor 不包含 `spawnAgentCall`、child slots、fallback 或 agentId，也不替宿主选择模型/派发方式。coordinator 收到后直接使用宿主协作工具；prepare 既不代表真实派发，也不代表 candidate 完成或用户批准。

同一主任务的 workspace/Python preflight 只由 coordinator 执行一次。`contentDrafting` child 不运行 `prepare_env`，不主动读取本文件或其他跨阶段 reference，不搜索源码、tests、examples、CLI `--help`、provider 配置或长日志；它只读取短入口、`role-contract.md`、`task.json`、冻结 inputs 以及 task 自带的 canonical candidate schema/skeleton。child 不需要理解 result schema，也不得自行拼装 `result.json`；candidate 结构不得靠猜测。

candidate 校验失败时必须一次返回全部可确定的结构错误，不能 fail-fast 到首个字段。coordinator 只允许一次同 attempt followup，让原 child 依据完整错误清单整体重写为 canonical schema；第二次仍为结构失败就升级更强代理。不得为每个缺失/多余字段分别 followup，也不得借结构补正修改冻结业务意图、attempt identity、current/SHA/stale 或批准状态。

准备失败仍使用退出码 2，并输出稳定的 `error=draft_agent_prepare_invalid`；`reasonCode` 区分参数、draft scope、输入不可读、content/revision/SRT 合同、managed input 路径冲突及 attempt 冲突。`message` 只来自固定文案，不回显正文、输入值、本机绝对路径或底层异常。外部 `--content-input`/`--source-srt` 不得与 draft-root 中脚本管理的 `content-input.json`/`source.srt` 同路径。

### 人工确认前的只读草案校验

coordinator 在生成 topic/text 审阅 artifact 前，必须通过标准输入执行只读校验，不能为了调用校验器先把未确认正文写成临时 JSON：

```powershell
# JSON 由 coordinator 以 UTF-8 写入子进程 stdin；此命令本身不创建输入文件
<ENV_PY> scripts/validate_content_draft.py --stdin
```

该命令仅在内存调用 current `validate_content_draft()` 和 `build_source_package()`，用于同时检查草案结构和确定性派生是否成立。成功时向 stdout 输出单个 `schemaVersion=1` 结构化 JSON，只包含 operation、content draft identity、模式/策略/target、cue 数、scene 数、`valid: true` 与 `writesPerformed: false`；不输出 topic/body、旁白正文、图片提示词、绝对路径或秘密。它不调用文本模型或 provider，不读取本地 secret 配置，不运行 `prepare_source.py`，不创建 source 准备包或正式项目，也不写任何批准记录。

`--stdin` 与 `--draft` 互斥。`--draft <candidate.content-draft.json>` 可以由 coordinator 在联合确认前读取已持久化的 attempt candidate；它只做只读校验，不能表示用户已确认。人工确认前若 candidate 尚未持久化，当前对话只能使用 `--stdin` 或直接调用纯函数。测试 fixture 同样不表示批准。无效 UTF-8、无效 JSON、草案合同错误、文件读取错误和参数错误均以退出码 2 拒绝，并只输出不含输入值或路径的结构化错误码。

`writesPerformed: false` 只是机器可读声明，不能单独作为零写入证据。自动测试必须在 C 盘 `TemporaryDirectory` 内记录调用前后的完整目录项，并对每个文件比较大小和 SHA-256；只有前后文件树完全一致，才证明校验没有生成草案文件、source 包、项目目录、批准记录、缓存或其他副作用。

candidate 通过只读校验后，由 coordinator 调用确定性 renderer；renderer 只写 draft scope 的 `reviews/`，不得写 attempt 或批准；结构化摘要按传入的真实能力返回完整句选项：

```powershell
<ENV_PY> scripts/render_content_review.py `
  --draft-root <D:\SRTWhiteboard\drafts\草案ID> `
  --candidate <attempt\candidate.content-draft.json> `
  --workspace-config <可选-workspace.local.json>
```

能力参数为 `--gpt-login-image-generation-available`、`--configured-image-provider-available|--configured-image-provider-unavailable` 与可选 `--fixed-image-generation-mode provider|gpt-login`。脚本不自行探测宿主登录态，也不读取 provider secret；原子批准层仍须重新验证能力。

coordinator 只消费 renderer 的结构化短摘要和 review 路径，不再读取 Markdown 全文。

## 自然中文旁白规范

旁白写法、三类 rewritePolicy 边界、两遍回读和“批准后不得继续改写”统一见
[`phase-0-content.md`](phase-0-content.md)。该规范只约束 `narrationCues[].text`，
不是新的 schema/identity 字段；本文后续只定义 `schemaVersion=1` content draft 的机器结构。

## Content draft（`schemaVersion=1`）

草案 JSON 的顶层字段固定为明确 allowlist：

```json
{
  "schemaVersion": 1,
  "inputMode": "topic",
  "topic": "为什么人会拖延",
  "body": null,
  "rewritePolicy": "generate",
  "targetDurationSeconds": 60,
  "voiceoverMode": "edge-tts",
  "visualStylePreset": "warm-paper-minimal-v1",
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

- `schemaVersion=1`；未知敏感字段拒绝或至少不得持久化。
- `topic` 与 `body` 分开保存，不能用其中一个覆盖另一个。topic 模式必须有非空 topic；text 模式必须有非空 body。
- 文本统一 Unicode NFKC、CRLF→LF 并移除首尾空白；不得静默改变正文语义。
- topic 建议上限 200 Unicode 字符；body 上限 128 KiB UTF-8。
- `targetDurationSeconds` 必须是 15–600 的有限数字；布尔值、NaN、无限值和范围外值拒绝。用户未指定时，Codex 可建议 60 秒，并与其他缺失配置一次性展示等待确认；用户已指定合法值时不得再问一次。
- narration cue 文本非空；`cueId` 从 `cue-001` 起连续且唯一；`sceneId` 必须存在。
- scene 从 `scene-01` 起连续编号；每个 cue 只属于一个 scene，scene cue 必须连续，不能离开后又返回旧 scene。
- 每个 `imagePrompt` 会作为独立场景请求的一部分发送，必须在本条内完整写出画布、纸张、线稿、配色、按该幕实际语义确定的造型锚点、构图、留白、画内文字策略和禁水印约束；默认允许语义需要的画内文字，若使用文字须写明准确内容并避免乱码、拼写错误或意外文字。还必须明确一个核心视觉命题、必要时 2–3 个可独立揭示区域、区域间真实纸面留白，并禁止跨区域贯穿性连续结构。必须不可分割的连续构图才合并为一个簇。不得依赖其他 scene 或先前图片，不得使用“延续”“沿用”“同上”“上一幕”“参照前图”等跨请求指代。`globalPrompt` 的确定性拼接不能替代单幕提示词的自包含性。
- 不接受 API Key、Cookie、Token、临时 URL、PID、本机绝对路径或完整模型内部响应；JSON 和 manifest 都不得成为凭据载体。

### `imagePrompt` 到正式 `prompt` 的确定性映射

`imagePrompt` 只属于 content draft；正式 `planning/generation-plan.json` 的 scene 字段名固定为 `prompt`。用户确认 current 内容草案后，coordinator 通过 `prepare_source.py` / `scripts.content_source.build_generation_plan()` 做逐幕确定性映射：

```text
content draft scenes[i].imagePrompt
  → formal generation plan scenes[i].prompt
```

映射保持同一 scene 的提示词文本和顺序，不让 child、provider 或人工复制时另行改写。正式 generation plan 不得保留 `imagePrompt`，内容草案也不得把该字段提前改名为 `prompt`；传统 SRT 的 storyboard candidate 则从一开始就使用正式 `prompt` schema。后续 provider 请求使用 `globalPrompt + scene.prompt` 的确定性组合，但 `globalPrompt` 仍不能替代单幕 prompt 的自包含约束。

三种 rewritePolicy 必须同时遵守上面的模式边界：`text + preserve` 保持原文与声口，`text + polish` 展示完整新稿和实质改动，`topic + generate` 从首稿直接生成自然口播。不确定事实不能伪装成已核验事实；联合批准前只允许确定性准备 source 并创建受限 pending 预项目，不得执行下游。

仓库示例分别见：[`topic + generate`](../examples/topic-habit-loop-content-draft.json)、[`text + preserve`](../examples/text-habit-loop-content-draft.json) 和 [`text + polish`](../examples/text-habit-loop-polish-content-draft.json)。示例只供维护者说明结构与策略边界，不代表用户已确认内容，也不替代真实朗读和人工审稿；prepared child 不得为了执行 task 主动搜索或读取 examples。

## 确定性 source package

current candidate 通过确定性校验后执行，用于创建 pending 预项目；它不表示用户已经批准：

```powershell
<ENV_PY> scripts/prepare_source.py `
  --draft <已通过-current-确定性校验、尚待联合批准的-candidate.content-draft.json> `
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

`input.json` 保存规范化后的 allowlist 内容；`manifest.json` 使用数字 `schemaVersion`，至少绑定 normalized input SHA、narration cue identity、source SRT SHA、generation plan SHA、target duration、inputMode/rewritePolicy，以及结构化 `timingAlgorithm.algorithm/version/parameters`。审计字段不得含秘密；manifest 内的文件引用必须是准备包内相对路径，不保存本机绝对路径。

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

## pending 预项目、原子提升与传统 SRT 兼容

topic/text 准备包创建 `pending_initial_approval` 预项目时使用（实际 CLI 以项目脚本当前参数为准）：

```powershell
<ENV_PY> scripts/create_project.py --name <项目名> `
  --srt <draft-dir\source.srt> `
  --plan <draft-dir\generation-plan.json> `
  --source-input <draft-dir\input.json> `
  --source-manifest <draft-dir\manifest.json> `
  --pending-initial-approval
```

`--source-input` 与 `--source-manifest` 必须成对出现，且只用于新建项目。创建时必须重新校验 input/manifest/SRT/plan 的全部 hash 与绑定关系，不能只复制文件。项目冻结：

```text
source/input.json
source/source-manifest.json
source/source.srt
```

`project.json` 可增加 `contentSource` 绑定 input/manifest hash，但实际下游 source 仍是 `source/source.srt`。续接项目只读取项目内冻结证据；不得回到外部 draft 目录重新推断 current state。创建失败只回滚本次唯一新项目目录，保留准备包供修复后重试。

传统 SRT 新任务同样先建 pending 预项目，并直接展示阶段 0 联合选项：

```powershell
<ENV_PY> scripts/create_project.py --name <项目名> `
  --srt <字幕.srt> --plan <已确认策略.json> --voiceover-mode disabled `
  --pending-initial-approval

<ENV_PY> scripts/create_project.py --name <项目名> `
  --srt <字幕.srt> --plan <已确认策略.json> `
  --pending-initial-approval

<ENV_PY> scripts/approve_initial_project.py --project <项目根目录> `
  --selection <绑定-current-identities-的-selection.json> `
  --configured-image-provider-available [--gpt-login-capable]
```

`--pending-initial-approval` 与 `--background-music/--agent-approval/--image-generation-mode` 互斥；新任务不得省略 pending flag 来创建兼容已批准项目。联合动作消费结构化 choice 并原子写成 approved，调用者不得依次改多个字段模拟事务。新批准统一使用 `approvalBasis=user_joint_content_and_plan`。旧项目缺少 `initialApproval` 时兼容为已批准，缺少 `agentApprovalEnabled` 时为 false，缺少 `imageGenerationMode` 时为 provider。传统 `disabled` SRT 保持 H.264/0 音频交付。

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
| 5 | stale、identity 不匹配或缺少所需批准 |

## 验收与报告边界

自动 fixture 应覆盖合法/非法 rewritePolicy、规范化、时长边界、cue/scene 连续性、确定性 hash/SRT/plan/manifest、严格 SRT round-trip、文件篡改拒绝、传统 SRT 无回归，以及 topic draft → source package → formal project。自动测试只能使用 fake provider/固定 WAV，不调用真实 Edge、真实图片 provider 或任何外部付费模型，也不得读取本地凭据。

fixture PASS 不证明以下事项已经通过：

- 用户已经确认某份具体内容草案；
- 微软 Edge 服务当前可用或某个 voice/rate 已被当前批准主体接受；
- 真实图片 provider 可用或线稿审美合格；
- 完整旁白、最终字幕 contact sheet 或最终成片已经过用户亲自批准或 AI 代理批准。

未实际执行的真实 provider 和质量 Gate 必须明确写为 SKIP、BLOCKED 或待确认，不能声称 PASS。阶段 0 的一次联合确认原子覆盖 current content、BGM、后续模式和生图方式。它不替代 full/final 技术证据、线稿、annotation review bundle 或 scene review bundle；自主模式只让 full/final 使用阶段 0 授权后的技术推进 basis，人工模式仍保留真实听审。clean master 只作为技术中间工件。
