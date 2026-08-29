# Codex Subagent 编排合同

合同版本：`whiteboard-subagent-orchestration-v1`

本合同定义 coordinator 如何以 artifact-first 方式准备、真实派发、校验和回收候选任务。child 不拥有正式写入、批准或用户交互权限。

## 1. 唯一 Coordinator

主 agent 是唯一 coordinator、用户接口和正式写者。只有 coordinator 可以决定阶段和 role 是否 ready，接收用户确认/修改或根据项目冻结的 `agentApprovalEnabled` 执行代理审阅，创建 task，计算有效并发，派发或 retry，验证 task/result 与 current binding，发布正式候选，并写 manifest、timeline、SRT、identity、stale、checkpoint 和批准。

每个 task 必须显式包含：

```json
{
  "formalWritesAllowed": false,
  "approvalWritesAllowed": false
}
```

任一字段缺失或不是 JSON `false`，task 必须在派发前失败。

## 2. Role Allowlist

首版只允许四种 role：

- `contentDrafting`：阶段 0 的 topic/text 内容草案；输出 attempt 内的 `candidate.content-draft.json`、`result.json` 和可选 `agent.log`。
- `storyboardPlanning`：传统 SRT 分镜；输出 attempt 内的 `candidate.generation-plan.json`、`result.json` 和可选 `agent.log`。
- `visualReview`：按冻结 scope 查看 current scene 图片、annotation preview bundle，或全部 current 单幕的少量关键帧；输出 attempt 内的 `findings.json`、`result.json` 和可选 `agent.log`。
- `annotationDrafting`：每个 task 只处理一幕；child 仅输出 attempt 内的 `candidate.annotation.json` 和可选 `agent.log`，`result.json` 由 coordinator 在候选就绪后确定性生成。

视觉 role 必须实际具备 `viewImage`；只读取文件名、尺寸、metadata 或 SHA 不算查看图片。

## 3. Artifact-first Attempt

每次执行使用独立 attempt 目录。逻辑 `taskId` 在 retry 间保持稳定，`attempt` 从 1 递增；旧 attempt 不原地覆盖。

```text
task.json
result.json
role-contract.md
scene-brief.json
base.content-draft.json
candidate.content-draft.json
revision-request.json
candidate.generation-plan.json
candidate.annotation.json
findings.json
agent.log
```

只有 coordinator 能创建和冻结 `role-contract.md`。task/result 中的业务路径必须是可信 scope 下的 POSIX 相对路径。validator 拒绝绝对路径、`..`、反斜杠、scope 外路径、其他 run/task/attempt、task 目录外输出、符号链接逃逸和 role output allowlist 之外的文件。

`allowedOutputs`、SHA 和 pre/post inventory 用于合同校验与事后侦测；任何命中都按失败处理。

## 4. Task 与 Result

`task.json` 使用 `whiteboard-agent-task-v1`，至少冻结 task identity、role contract version/SHA、input 相对路径和 SHA、current bindings、required capabilities、allowed outputs，以及两个显式 false 的写权限字段。

`result.json` 使用 `whiteboard-agent-result-v1`。`status` 只允许 `completed | failed | cancelled`。对于 `annotationDrafting`，result 由 coordinator 在 candidate artifact ready 后生成；其他 role 仍由 child 生成。result 必须回显 task identity、派发 task SHA、role contract version/SHA、实际检查过的 frozen inputs，以及 attempt 内 output 路径和 SHA。

coordinator 收取 result 后必须重验：

1. task 字节仍等于派发 SHA；
2. role contract 和 input SHA 未变化；
3. current bindings 仍 current；
4. result schema 与 task identity 完全匹配；
5. inspected inputs 完整回显；
6. output 路径、文件存在性和 SHA 有效；
7. role 对应的确定性业务 validator PASS。

自然语言完成消息、退出码、目录时间戳或 `completed` 字段不能单独成为成功证据。

## 5. Prompt 最小化

child prompt 只提供 attempt 内 `role-contract.md` 与 `task.json` 的绝对定位路径、对应 SHA、唯一允许写入的 attempt 目录，以及固定 `TASK_STATUS`/`RESULT_JSON` 返回格式。

prompt 不复制完整主对话、完整 SRT/正文、所有 scene 数组、provider 配置、凭据、批准信息、长工具日志或未冻结状态。

### 5.1 Draft/Plan role 的冻结上下文与提示词边界

`contentDrafting`、`storyboardPlanning`、`visualReview` 和 `annotationDrafting` 共用同一条最小 prompt 规则：prompt 是定位器，不是业务输入的第二份副本。child 必须从冻结的 `task.json`、其 `inputs` 与 `role-contract.md` 读取业务内容；主窗口不得把正文、完整 SRT、完整 scene 数组或上一轮对话重新拼进 prompt。provider 名称、endpoint、模型参数、API key/token/cookie、批准或拒绝内容、完整工具日志、外部响应和未冻结状态都不得进入 prompt。

除固定返回协议外，prompt 只允许传递以下冻结值：`taskId`/`taskKind`、`taskSha256`、`roleContractPath`/`roleContractSha256`、`TASK_JSON_PATH`、唯一 `ALLOWED_ATTEMPT_DIR`、候选或 findings/result 的 attempt 内路径，以及固定的返回字段/枚举。`formalWritesAllowed:false` 与 `approvalWritesAllowed:false` 必须留在 task schema 中由 validator 校验，不得以自然语言授权代替。

`contentDrafting` 的 `candidate.content-draft.json` 使用 scene 字段 `imagePrompt`；用户确认 current 草案后，coordinator 只能按以下唯一确定性映射生成正式 generation plan：

```text
formal.scenes[i].prompt = candidate.scenes[i].imagePrompt
formal.scenes[i].sceneId/name/coreIdea/visualSubject/cueRange = candidate 对应字段（按原顺序）
```

其中 `candidate` 已是 `validate_content_draft()` 返回的 canonical candidate；映射必须逐字复制该 canonical `imagePrompt`，只 materialize 正式 schema 的字段名和 coordinator 从 current cue/timing evidence 确定的字段，不得再次 trim、拼接、调用模型、改写语义或调换 scene/cue 顺序，也不允许 formal plan 保留 `imagePrompt`。child 不直接写 formal plan，provider 只消费 formal `prompt`；任何需要改变提示词的意见都必须回到新的 revision attempt，并回到阶段 0 取得用户对实质新方案的确认；`agentApprovalEnabled` 不授权 coordinator 改写已冻结用户意图。

## 6. 调度与并发

`execution.agents` 只配置 agent 并发：

```json
{
  "execution": {
    "agents": {
      "default": 1,
      "contentDrafting": 1,
      "storyboardPlanning": 1,
      "visualReview": 1,
      "annotationDrafting": 1
    }
  }
}
```

effective concurrency 取 configured、ready task 数、宿主已换算 child slots 与 coordinator resource budget 的最小值。coordinator 始终保留自己的槽位；总 slot 到 child slot 只换算一次。

只有 coordinator 能从当前实际宿主工具、live agents 和任务状态得知 child slots 与 role capability，并据此调用真实 `spawn_agent`、`followup` 和等待机制。Python prepare/validate 脚本只冻结 task descriptor 或有序 unit：不得接收/推断 runtime child slots、coordinator budget、宿主 capability，不得输出 `spawnAgentCall`/`spawnRequest`、`dispatchAllowed` 或替宿主选择 fallback。真实派发不可用时，才由具备相同 role capability 的 coordinator fallback，并报告当前宿主的真实原因；双方都缺能力时报告 `BLOCKED`。

attempt 是 artifact 版本边界，不是 agent 生命周期边界。首次独立 draft/plan/review 使用短上下文 child；用户修订 content 草案仍创建新 attempt，但上一 attempt 的同 role child 仍存在、idle、上一结果 completed 且 role contract 兼容时，优先 followup 原 child，让它只读取新 task/base/revision 的路径与 SHA。原 child 不可用、失败、role 改变、修订升级为全面独立重写或用户明确要求换执行者时才 spawn 新 child。同一 attempt 的执行性补正也 followup 原 child；无论是否复用，磁盘 current 与 SHA 始终高于代理记忆。

`contentDrafting`、`storyboardPlanning` 和 global `visualReview` 使用一 task一 child。`annotationDrafting` 保持一幕一 task/attempt/candidate/result（child 只写 candidate，result 由 coordinator 生成），但把按 plan 连续的最多 3 个 task 组成一个 dispatch unit，由同一 child 顺序执行。prepare 根据 configured concurrency 平衡连续切分：5 task/4 configured 为 `2+1+1+1`，9/3 为 `3+3+3`，5/2 为 `3+2`。多个 ready unit 必须先填满 effective 并发再等待，不能串行伪装成并发。

coordinator 可在等待期间调用 `annotation_dispatch.py observe` 重验 candidate 并更新 coordinator-owned audit。首次 ready 的 candidate SHA 必须冻结；之后字节变化标记为 stale，当前观察不是 ready 时不得沿用旧 `candidateReadyAt` 建议 materialize。candidate ready 后 child 已结束则立即建议 materialize；仍运行时只等待固定 tail grace，随后返回 `finalizeRecommended=true`。公开 materialize 入口必须重验 current dispatch manifest、`finalizeRecommended` 和冻结 SHA 后才写 coordinator-owned result/materialized candidate。该 CLI 不调用、followup、取消或结束 child，也不写 result/formal/approval；coordinator 仍使用宿主协作工具处理 child 生命周期，再调用既有确定性 materialize/validate。audit 分别记录 prepare、first/last candidate ready、child tail 与 result materialize，阶段耗时不得全部留空。

annotation unit 内不是“生成完三幕再统一校验”：child 每写完一个 `candidate.annotation.json` 就返回该 task 的 `candidate_ready`，coordinator 立即执行 descriptor 中的 `candidateLint.command`。只有 lint `PASS` 才允许该 child 继续 unit 内下一 task；lint `FAIL` 时只补正当前 candidate，避免统一 schema 错误扩散到整批。dispatch manifest 的 `LINT_CANDIDATE_BEFORE_NEXT_TASK` 是硬约束。

一个 annotation child 只服务一个 dispatch unit，图片上下文不得跨 unit 累积。同一 attempt 的执行性补正仍优先 followup 原 child，但只传冻结定位、candidate SHA 与精简错误，不重新附带原图或长日志。`413`、`Payload Too Large` 或 context length exceeded 表示原 child 对本次补正已不可用：禁止原样重试，改用新短上下文 child 做 JSON-only 补正；只有错误确实涉及 region/视觉簇时，才让新 child 单独加载当前一幕图片。换 child 不创建新 attempt，也不改变正式写入和批准边界。

prepare 摘要只记录 configured concurrency、task/unit 数和冻结 descriptor；effective/peak、mode、真实原因和 task/agent 映射只能由 coordinator 在真实派发或 fallback 后记录。fake scheduler 和 prepared artifact 都不算真实 dispatch。

agent pool 与 worker pool 独立，不做乘法。agent task 不得内部再启动 provider、FFmpeg、深验或其他 worker batch。

## 7. Candidate、Current 与 Approval

状态必须严格区分：

```text
candidate created
result contract validated
business validator passed
coordinator published current
指定审阅主体对 current 作出明确决定
coordinator 调用既有批准脚本绑定 current identity
```

前一步不能自动推出后一步。失败、取消或 stale candidate 保留在 attempt 目录供诊断，不覆盖旧 current 文件。subagent、worker、CLI、fake scheduler、findings 和技术 manifest 都不得写任何批准。即使 `agentApprovalEnabled=true`，child task 也始终保持 `approvalWritesAllowed:false`；只有 coordinator 在重验真实审阅证据和 current identity 后才可调用原批准脚本。批准主体不进入作品 identity。

retry 只针对 `failed | cancelled | stale` 创建新 attempt。current completed 且 input/role contract SHA 未变化的候选不得重复执行；`blocked` 必须先改变能力或外部条件。

## 8. Annotation 批量合同

Phase 4 只能在线稿已获用户明确确认后开始。coordinator 先构建一次只读 `FormalValidationContext`，一次 batch 只深验一次 timing/voice/review 全局 evidence。

每个 ready scene 建立独立 `annotationDrafting` task。child 只负责 `elements` 视觉判断；sceneId、canvas、duration、frame range、timing/render/timeline binding 与 timingSource 由 coordinator 从 current evidence 确定性注入。

每个 task descriptor 必须携带只读 `candidateLint` 命令。该 lint 仅检查 UTF-8 JSON、顶层合同、element/region/reveal 字段、`reveal.protectedRegions` 嵌套和基础几何/时序 schema，不写 result、materialized candidate、正式 annotation、manifest 或批准。全量发布前仍必须执行绑定 current project/timing 的完整 validator；早期 lint 不能替代完整验证。

候选可有界并行校验，但正式 annotation 必须按 generation plan 顺序逐幕原子发布。任一必需 scene 失败、缺失或 stale 时 batch 为 `FAIL`；已有发布则记录 `partialSuccess:true`，不得启动全量预览或写批准。全部 scene current 且 validator PASS 后，才启动项目预览与区域预览并进入 annotation 联合 Gate：人工模式交付用户确认，AI 代理模式由 coordinator 接回真实审阅。

## 8.1 生成后审阅策略

生图消费验证、annotation preview bundle 和 scene review bundle 都接受 `--review-policy user_first|agent_first`。两种策略都必须先完成当前阶段的确定性技术验证，并保留对应 Gate 批准；策略只决定是否准备 AI 语义审阅，不进入作品内容 identity，也不得直接写批准。

- `agentApprovalEnabled` 缺失或为 `false`：在交付完整旁白、等待用户确认的同一条消息中征询策略，使用户一次回复即可同时表达完整旁白与真实时长决定以及 `user_first|agent_first` 选择。用户确认旁白但未指定时继续停在 Gate 追问，禁止静默默认。
- `agentApprovalEnabled=true`：审阅策略确定性为 `agent_first`，不再询问。coordinator 无需伪造完整听音，但必须重验阶段 0 current 样音授权，以及整轨 provider、canonical WAV 完整解码、对应 provider 的词级时间证据（Edge TTS/豆包为本地 FunASR，MiniMax 为同次响应原生 word 字幕）、原稿对齐、timeline/narration SRT、current `FULL_IDENTITY` 和时长偏差等技术证据，按技术推进 basis 成功执行 `approve-full --review-policy agent_first` 后才能开始视觉阶段。

两种模式都不得在旁白未获 current Gate 批准时仅凭策略选择启动生图。

- `user_first`：不创建或派发额外 `visualReview`，机器摘要记录 `semanticReview.status=skipped_by_user`，只把 current 线稿 review Markdown 链接、identity、计数和异常摘要交给用户；不得把全部图片重新嵌入主聊天。
- `agent_first`：只冻结宿主中立 `visualReview` task descriptor；coordinator 必须直接调用宿主协作工具完成真实 spawn/wait。child 把完整意见写入 attempt 的 `findings.json/result.json`；coordinator 只接收路径、status、validator 状态和精简摘要，不得再次逐图审阅。人工模式将 advisory findings 摘要与 current review 文件一并交用户；AI 代理模式下，coordinator 必须重验 child 确实具备所需媒体能力、已检查全部冻结 scope、findings 内容和 current binding，再作出独立审阅决定；`completed` 或无 findings 不能自动推出批准。prepared descriptor 不能报告成真实派发或 review 完成。
- 生图复用覆盖全部 current PNG 的 global `visualReview`；annotation 只复查已生成的 preview bundle，不能跳过 `annotationDrafting` 对原图的实际查看；scene 只在全部 current 单幕形成一次有序 bundle 后预审。自主模式的 scene bundle 仍须具备视频能力的审阅者实际检查 current 视频；final 不再重复声音主观 Gate，而按 current full audio/字幕/AAC/完整解码/时长帧数尾部/BGM/`FINAL_IDENTITY` 技术证据推进。

## 9. Gate 决策与自动代理

topic/text 内容草案与传统 SRT 的初始分镜/策略属于阶段 0 用户意图冻结；旁白项目还必须由用户试听 current 样音。它们通过一次绑定 current content/sample identity 的联合回复原子批准 pending 预项目。阶段 0 之后，coordinator 读取 `project.json.agentApprovalEnabled`：

- 缺失或 `false`：阶段 0 样音已联合批准，后续继续等待用户确认完整旁白/真实时长、视觉 bundle 和最终成片。
- `true`：full/final 以用户样音授权后的严格技术推进 basis 继续，不能声称 AI 完整听音；视觉 bundle 仍由具备真实查看能力的 coordinator/child 审阅 current artifact。

技术 PASS 不能冒充真实听审；自主 full/final 只能在完整技术证据 current 且样音授权 current 时写明确 basis。视觉 agent findings/candidate/批次完成也不能替代真实查看与决定。`unknown_external_outcome`、额外费用/服务/凭据、版权授权和实质改变阶段 0 意图仍须询问用户。

## 10. 状态边界

- `PASS`：实际执行的 task/result 或业务 validator 通过；
- `FAIL`：合同、路径、binding、SHA、validator 或执行失败；
- `BLOCKED`：执行者缺少 role 必需能力；
- `SKIP`：当前阶段未执行真实派发或外部调用；
- `待确认`：技术结果已准备，仍等待指定审阅主体的 Gate 决定；AI 代理模式下 coordinator 应立即接回审阅，不把该状态转成用户停顿。

真实宿主协作只有在 child 从冻结 task 读取输入、写出 candidate（annotationDrafting 的 result 由 coordinator 随后生成），并记录真实 agent/task 标识后才能报告 dispatch `PASS`。真实图片 provider、Edge 服务、声音接受度和视觉接受度必须与自动 fixture/技术验证分别报告；报告中应按项目模式准确区分“待用户确认”和“待 coordinator 代理审阅”。
