# Phase 4：可选阶段编排 runner

Phase 4 的 runner 是现有逐步 CLI 的可选入口，用于减少 Python 子进程启动和主窗口往返。它只串联本地、确定性的步骤，不替代任何人工确认，也不把 provider 请求放进 agent task。

## annotation-preview

```powershell
& $envPy scripts\run_phase.py --project <项目根目录> --phase annotation-preview
```

runner 会在同一进程内复用 project/config/formal validation context（实现允许时），依次执行技术校验、current receipt 复用、annotation candidate 校验/发布、区域预览和 contact sheet，并在 annotation 联合人工 Gate 停止。成功摘要至少包含：

- `contractVersion`、`status`、`projectId`、`runId`、`taskCount`；
- `configuredConcurrency`、`effectiveConcurrency`、`peakConcurrency`（适用时）；
- `successCount`、`failureCount`、`partialSuccess`、`currentIdentity`；
- `approvalWritten: false`、`userConfirmationRequired: true`、`nextGate`；
- `failures`、artifact/preview URL（或项目内相对路径）；
- `deepValidationSkipped`/`deepValidationReused` 及其 current binding 依据。

runner 不接受自动批准参数，不读取聊天记录推断批准。技术 PASS、candidate、receipt、agent findings 或用户未反对，都不能视为批准；人工回复必须明确指向摘要给出的 artifact 和 identity。摘要中的 `approvalWritten` 在每个人工 Gate 都必须为 `false`。

## 逐步调试与恢复

runner 随时可以退回逐步 CLI，产物和 current binding 不变：

```powershell
# 只做技术校验/准备（按项目当前状态选择）
& $envPy scripts\validate_annotations.py validate --project <项目根目录> --candidate-root <candidate-root>
& $envPy scripts\generate_annotation_previews.py --project <项目根目录> --all --review-policy user_first

# 用户确认 current annotation identity 后，显式写批准
& $envPy scripts\approve_annotation_review.py --project <项目根目录> `
  --identity-hash <annotationReviewIdentitySha256>
```

runner 中断或失败后不要从头重做：保留已 current 的 receipt，重新执行 runner 或受影响的逐步命令即可。若 timing、render profile、图片、annotation 或配置 binding 发生变化，旧 receipt/批准必须 stale，重新 deep validation；binding 未变时可复用深校验，并在摘要中写明跳过/复用原因。

## Gate 与恢复语义

- runner 在 annotation 联合确认处停止，输出 artifact、identity、状态和需要用户明确回复的内容；不会写 approval。
- 确定性失败只重做受影响阶段；已验证且 binding current 的阶段不得重复执行。
- provider `unknown_external_outcome` 不得普通重跑或自动重发；必须等待用户单独授权新的外部调用。
- 旧批准不能跨 identity、manifest、timeline、SRT 或 receipt 变化复用。
- runner 不是黑盒或唯一恢复路径；所有阶段仍可使用独立 CLI 检查、修复和重试。

