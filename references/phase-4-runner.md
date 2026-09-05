# Phase 4：可选阶段编排 runner

Phase 4 的 runner 是现有逐步 CLI 的可选入口，用于减少 Python 子进程启动和主窗口往返。它只串联本地、确定性的步骤，不替代任何 Gate 审阅或批准，也不把 provider 请求放进 agent task。

## annotation-preview

```powershell
& $envPy scripts\run_phase.py --project <项目根目录> --phase annotation-preview
```

runner 会在同一进程内复用 project/config/formal validation context（实现允许时），依次执行技术校验、current receipt 复用、annotation candidate 校验/发布、区域预览和 contact sheet，并在 annotation 联合 Gate 停止。成功摘要至少包含：

- `schemaVersion`、`phase`、`status`、`projectId`、`runId`、`taskCount`；
- `configuredConcurrency`、`effectiveConcurrency`、`peakConcurrency`（适用时）；
- `successCount`、`failureCount`、`partialSuccess`、`currentIdentity`；
- `approvalWritten: false`、`userConfirmationRequired: true`、`nextGate`；
- `failures`、artifact/preview URL（或项目内相对路径）；
- `deepValidationSkipped`/`deepValidationReused` 及其 current binding 依据。

runner 不接受自动批准参数，不读取聊天记录或项目开关推断批准。技术 PASS、candidate、receipt、agent findings、用户未反对或 AI 无异常摘要，都不能视为批准。摘要中的 `approvalWritten` 在每个 Gate 都必须为 `false`：人工模式的用户回复必须明确指向 artifact 和 identity；AI 代理模式由 coordinator 在 runner 返回后接回真实审阅、决策和既有批准脚本调用。

runner 外层 stdout 使用严格白名单投影：保留状态、identity、并发、计数、artifact、短 deep-validation/timing/provider/dispatch/fallback 摘要，以及 receipt/manifest 路径；不得嵌入 adapter 的完整 `outputs`、深层 media receipt、manifest 或长日志。恢复命令中的 `run_phase.py` 必须来自 `Path(__file__).resolve()` 的绝对路径，不依赖调用者 cwd。

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
`WAITING_HUMAN_GATE`，进程退出码为 0。退出码 0 只表示技术链完成。人工模式等待用户完整看片听音；`agentApprovalEnabled=true` 时 runner 仍不自行批准，而是交回 coordinator 重验 current full audio、字幕、AAC、流结构、完整解码、时长/帧数/尾部、实际 BGM 模式与 `FINAL_IDENTITY`，再以阶段 0 授权后的技术推进 `reviewBasis` 调用原批准脚本。Edge/MiniMax 启用 BGM 时验证固定混音 receipt；豆包 prompt-only v3 启用时验证 `provider_embedded`、纯文本音色与 scene 时间窗口 prompt/audio identity binding，且不得出现固定曲目或 mix receipt。不得因宿主没有听音能力而再次阻塞，也不得声称 AI 完整听过 final。

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

- runner 在 annotation 联合确认处停止，输出 artifact、identity、状态和 Gate 审阅要求；不会写 approval。视觉自主模式仍由 coordinator/具备能力的 child 实际查看 current artifact。
- 自动化不得仅凭进程退出码 0 继续需要批准的下游；必须同时读取结构化 `status` 与 `approvalWritten`。`WAITING_HUMAN_GATE` 时 runner 不能自动调用批准脚本；人工模式等待用户，自主 final 模式由 coordinator 接回并完成严格技术证据复核，视觉 Gate 则仍完成真实查看。
- 确定性失败只重做受影响阶段；已验证且 binding current 的阶段不得重复执行。
- provider `unknown_external_outcome` 不得普通重跑或自动重发；必须等待用户单独授权新的外部调用。
- 旧批准不能跨 identity、manifest、timeline、SRT 或 receipt 变化复用。
- runner 不是黑盒或唯一恢复路径；所有阶段仍可使用独立 CLI 检查、修复和重试。
