# Codex Subagent 编排合同

合同版本：`whiteboard-subagent-orchestration-v1`

本合同定义 coordinator 如何以 artifact-first 方式准备、真实派发、校验和回收候选任务。child 不拥有正式写入、批准或用户交互权限。

## 1. 唯一 Coordinator

主 agent 是唯一 coordinator、用户接口和正式写者。只有 coordinator 可以决定阶段和 role 是否 ready，接收用户确认或修改，创建 task，计算有效并发，派发或 retry，验证 task/result 与 current binding，发布正式候选，并写 manifest、timeline、SRT、identity、stale、checkpoint 和批准。

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

role capability 齐全且 effective 大于 0 时，由 coordinator 调用宿主真实 `spawn_agent`、`followup` 和等待机制。否则由具备相同 role capability 的 coordinator fallback；双方都缺能力时报告 `BLOCKED`。

`contentDrafting`、`storyboardPlanning` 和 global `visualReview` 使用一 task一 child。`annotationDrafting` 保持一幕一 task/attempt/candidate/result（child 只写 candidate，result 由 coordinator 生成），但把按 plan 连续的最多 3 个 task 组成一个 dispatch unit，由同一 child 顺序执行。多个 ready unit 必须先填满 effective 并发再等待，不能串行伪装成并发。

审计只记录 configured/effective/peak/task count、`dispatchAllowed`、mode、adapter、reason 和真实 task/agent 映射。fake scheduler 只验证协议，不算真实 dispatch。

agent pool 与 worker pool 独立，不做乘法。agent task 不得内部再启动 provider、FFmpeg、深验或其他 worker batch。

## 7. Candidate、Current 与 Approval

状态必须严格区分：

```text
candidate created
result contract validated
business validator passed
coordinator published current
user explicitly approved current
```

前一步不能自动推出后一步。失败、取消或 stale candidate 保留在 attempt 目录供诊断，不覆盖旧 current 文件。subagent、CLI、fake scheduler、findings 和技术 manifest 都不得写人工批准。

retry 只针对 `failed | cancelled | stale` 创建新 attempt。current completed 且 input/role contract SHA 未变化的候选不得重复执行；`blocked` 必须先改变能力或外部条件。

## 8. Annotation 批量合同

Phase 4 只能在线稿已获用户明确确认后开始。coordinator 先构建一次只读 `FormalValidationContext`，一次 batch 只深验一次 timing/voice/review 全局 evidence。

每个 ready scene 建立独立 `annotationDrafting` task。child 只负责 `elements` 视觉判断；sceneId、canvas、duration、frame range、timing/render/timeline binding 与 timingSource 由 coordinator 从 current evidence 确定性注入。

候选可有界并行校验，但正式 annotation 必须按 generation plan 顺序逐幕原子发布。任一必需 scene 失败、缺失或 stale 时 batch 为 `FAIL`；已有发布则记录 `partialSuccess:true`，不得启动全量预览或写批准。全部 scene current 且 validator PASS 后，才启动项目预览与区域预览并进入聊天人工确认。

## 8.1 生成后审阅策略

生图消费验证、annotation preview bundle 和 scene review bundle 都接受 `--review-policy user_first|agent_first`。两种策略都必须先完成当前阶段的确定性技术验证，并保留对应人工批准；策略只决定用户关卡之前是否多一次 AI 语义预审，不进入作品内容 identity，也不得写批准。

该策略应在交付完整旁白、等待用户确认的同一条消息中征询，使用户一次回复即可同时表达完整旁白与真实时长决定以及 `user_first|agent_first` 选择。coordinator 必须先成功写入 current `approve-full`，再采用策略并开始视觉阶段；不得在旁白批准后另设一次只用于选择策略的聊天停顿，也不得在旁白未获批准时仅凭策略选择启动生图。用户确认旁白但未指定时默认 `user_first`。

- `user_first`：不创建或派发额外 `visualReview`，机器摘要记录 `semanticReview.status=skipped_by_user`，只把 current 线稿 review Markdown 链接、identity、计数和异常摘要交给用户；不得把全部图片重新嵌入主聊天。
- `agent_first`：只冻结 `visualReview` task 并准备宿主 `spawnPackage`；`preparedOnly:true` 和 `hostSpawnExecuted:false` 不能报告成真实派发或 review 完成。coordinator 必须调用宿主协作工具完成真实 spawn/wait，child 把完整意见写入 attempt 的 `findings.json/result.json`；coordinator 只接收路径、status、validator 状态和精简摘要，不得再次逐图审阅，再把 advisory findings 摘要与 current review 文件一并交用户。
- 生图复用覆盖全部 current PNG 的 global `visualReview`；annotation 只复查已生成的 preview bundle，不能跳过 `annotationDrafting` 对原图的实际查看；scene 只在全部 current 单幕形成一次有序 bundle 后预审，每幕仅抽首帧、中段和完成帧等少量关键帧，不逐幕重复 AI review。

## 9. 人工关卡

coordinator 必须分别等待用户明确确认 topic/text 内容草案、传统 SRT 分镜与策略、Edge 样音、完整旁白与真实时长、线稿、标注/区域/时序联合 bundle、全部正式单幕和最终成片。

技术 PASS、agent findings、candidate、批次完成或用户没有反对，都不能替代明确确认。

## 10. 状态边界

- `PASS`：实际执行的 task/result 或业务 validator 通过；
- `FAIL`：合同、路径、binding、SHA、validator 或执行失败；
- `BLOCKED`：执行者缺少 role 必需能力；
- `SKIP`：当前阶段未执行真实派发或外部调用；
- `待确认`：技术结果已准备，仍等待用户人工关卡。

真实宿主协作只有在 child 从冻结 task 读取输入、写出 candidate（annotationDrafting 的 result 由 coordinator 随后生成），并记录真实 agent/task 标识后才能报告 dispatch `PASS`。真实图片 provider、Edge 服务、声音接受度和视觉接受度必须与自动 fixture/技术验证分别报告。
