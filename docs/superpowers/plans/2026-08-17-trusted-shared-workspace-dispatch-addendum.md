# Trusted Shared Workspace 真实调度纠偏 Addendum

> 日期：2026-08-17  
> 修正范围：仅修正 `2026-08-15-configurable-concurrency-and-performance-optimization-plan.md` 的 agent dispatch/trust model 结论；其余 worker、candidate/current、正式单写、人工关卡、identity、stale、恢复和媒体严格性合同继续有效。

## 被替代的旧结论

旧计划中“当前共享文件系统 runtime 一律 `dispatchAllowed=false/effective=0`”“topic/text 必须由主窗口生成”“只有强隔离 runtime 才能真实 dispatch”的产品结论，由本 addendum 替代。对应的 Phase 1B、Phase 4、Phase 10、完成定义和自检条目按下述双模式解释，不整篇重写旧计划。

## 新的双模式合同

- `strict_isolation` 保留原 fail-closed 语义：强隔离证据不足时 `securityIsolationEnforced=false`、`dispatchAllowed=false`、effective/peak 为 0，并走同合同 coordinator fallback。
- `trusted_shared_workspace` 由用户显式选择，承认 coordinator 与 Codex child 共享文件系统和工具权限；它允许宿主真实 `spawn_agent`/followup/等待调度，但必须记录 `securityIsolationEnforced=false`，不得写成 isolation PASS。
- role allowlist 增加阶段 0 的 `contentDrafting`，并保留 `storyboardPlanning | visualReview | annotationDrafting`。topic/text child 从冻结文件读取输入，只写 attempt candidate/result；coordinator 负责展示、用户确认、validator 和正式单写。
- effective agent concurrency 取 configured、ready task 数、真实 child slots 与 coordinator 资源预算的最小值；保留 coordinator 槽位且只换算一次。多个 ready task 先并行 spawn 再等待；agent 与 worker 并发资源池分离，禁止嵌套乘法。
- task prompt 只携带 role-contract/task 的绝对路径与 SHA、唯一 attempt 目录和固定返回格式。child 只返回 result 路径、status、validator 状态与精简摘要；完整正文、SRT、逐幕推理和长日志留在 attempt。
- allowedOutputs、task/result SHA 与 pre/post inventory 仍只是协议约束和事后侦测，不能冒充权限隔离。child 不得写正式 PNG/WAV、manifest、timeline、SRT、identity、stale、checkpoint 或批准。

## 不变边界

所有内容确认、样音、完整旁白试听与真实时长批准、线稿、annotation、区域预览、最终时序、逐幕和 final 人工关卡保持不变；candidate/result/validator PASS 不等于 current 或批准。完整旁白阶段不再生成无画面的预审视频。Phase 8 仍不实施，`sceneRender` 仍只能为 `1`，真实图片 provider 与真实 Edge TTS 不纳入自动 fixture 验收。
