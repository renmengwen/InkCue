# Phase 4：可选阶段编排 runner

Phase 4 的 runner 是现有逐步 CLI 的可选入口，用于减少 Python 子进程启动和主窗口往返。它只串联本地、确定性的步骤，不替代任何 Gate 审阅或批准，也不把 provider 请求放进 agent task。

## annotation-preview

```powershell
& $envPy scripts\run_phase.py --project <项目根目录> --phase annotation-preview
```

runner 会在同一进程内复用 project/config/formal validation context（实现允许时），依次执行技术校验、current receipt 复用、annotation candidate 校验/发布、区域预览和 contact sheet，并在 annotation 联合 Gate 停止。成功摘要至少包含：

- `contractVersion`、`status`、`projectId`、`runId`、`taskCount`；
- `configuredConcurrency`、`effectiveConcurrency`、`peakConcurrency`（适用时）；
- `successCount`、`failureCount`、`partialSuccess`、`currentIdentity`；
- `approvalWritten: false`、`userConfirmationRequired: true`、`nextGate`；
- `failures`、artifact/preview URL（或项目内相对路径）；
- `deepValidationSkipped`/`deepValidationReused` 及其 current binding 依据。

runner 不接受自动批准参数，不读取聊天记录或项目开关推断批准。技术 PASS、candidate、receipt、agent findings、用户未反对或 AI 无异常摘要，都不能视为批准。摘要中的 `approvalWritten` 在每个 Gate 都必须为 `false`：人工模式的用户回复必须明确指向 artifact 和 identity；AI 代理模式由 coordinator 在 runner 返回后接回真实审阅、决策和既有批准脚本调用。

## final-delivery

```powershell
& $envPy scripts\run_phase.py --project <项目根目录> --phase final-delivery
```

该入口只消费 current、已批准的 scene bundle，按 generation plan 固定顺序连续执行
`merge → burnSubtitles →（旁白模式）muxVoiceover → validateFinalMedia`。它不调用
图片/TTS provider，不要求模型在步骤间重新决策，也不写 `finalApproval`。摘要额外包含：

- `timingsMs.preflight/merge/burnSubtitles/muxVoiceover/validateFinalMedia/total`；
- `lastCompletedStep`、可复制的同 phase 恢复命令；
- current `FINAL_IDENTITY`、`output/final.mp4` 与 `nextGate=final_media_review`。

任一步失败即停止后续步骤，保留正式 current 文件及失败工作目录供诊断；成功后状态仍投影为
`WAITING_HUMAN_GATE`，进程退出码为 0。这些旧有状态名和字段不因 `agentApprovalEnabled` 新增或改名：`status=WAITING_HUMAN_GATE`、`technicalStatus=PASS`、`processOutcome=completed_waiting_for_user`、`approvalWritten=false`、`userConfirmationRequired=true` 与 `nextGate`。退出码 0 只表示本次技术链成功完成，不表示 Gate 批准。人工模式必须等待用户完整看片，旁白模式还须完整听音；AI 代理模式则必须交回 coordinator，由具备真实视听能力的审阅者完整看片/听音、核对 current identity、决策并调用原批准脚本。这样 PowerShell/桌面命令包装层不会把预期 Gate 显示成技术失败，同时独立批准脚本和 current identity 校验仍保持 fail-closed。

## 逐步调试与恢复

runner 随时可以退回逐步 CLI，产物和 current binding 不变：

```powershell
# 只做技术校验/准备（按项目当前状态选择）
& $envPy scripts\validate_annotations.py validate --project <项目根目录> --candidate-root <candidate-root>
& $envPy scripts\generate_annotation_previews.py --project <项目根目录> --all --review-policy user_first

# 指定审阅主体确认 current annotation identity 后，由 coordinator 显式写批准
& $envPy scripts\approve_annotation_review.py --project <项目根目录> `
  --identity-hash <annotationReviewIdentitySha256>
```

runner 中断或失败后不要从头重做：保留已 current 的 receipt，重新执行 runner 或受影响的逐步命令即可。若 timing、render profile、图片、annotation 或配置 binding 发生变化，旧 receipt/批准必须 stale，重新 deep validation；binding 未变时可复用深校验，并在摘要中写明跳过/复用原因。

## Gate 与恢复语义

- runner 在 annotation 联合确认处停止，输出 artifact、identity、状态和 Gate 审阅要求；不会写 approval。人工模式交付用户，AI 代理模式立即交回 coordinator。
- 自动化不得仅凭进程退出码 0 继续需要批准的下游；必须同时读取结构化 `status` 与 `approvalWritten`。`WAITING_HUMAN_GATE` 时 runner 本身不能自动调用批准脚本；人工模式等待用户，AI 代理模式由 coordinator 接回并在完成真实审阅后调用既有批准脚本。
- 确定性失败只重做受影响阶段；已验证且 binding current 的阶段不得重复执行。
- provider `unknown_external_outcome` 不得普通重跑或自动重发；必须等待用户单独授权新的外部调用。
- 旧批准不能跨 identity、manifest、timeline、SRT 或 receipt 变化复用。
- runner 不是黑盒或唯一恢复路径；所有阶段仍可使用独立 CLI 检查、修复和重试。
