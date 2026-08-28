# 恢复、identity、stale 与重试合同

合同版本：`whiteboard-recovery-identity-v1`

本文件是所有阶段共用的状态、身份绑定、stale 和 retry 唯一规范来源。图片、语音、字幕
和渲染 reference 只说明本阶段如何调用它；出现冲突时以本文为准。本文不引入 receipt、
benchmark 或 runner，也不改变现有业务脚本行为。

## 1. 状态边界

候选和正式产物必须按以下单向边界记录，前一状态不能推出后一状态：

```text
candidate created
→ result/业务 schema validated
→ technical validator PASS
→ current binding rechecked
→ coordinator atomic publish
→ 指定审阅主体完成真实审阅并明确接受 current identity
→ coordinator 调用既有批准脚本写入 approval
```

`PASS` 只表示实际执行的技术合同通过；`待确认` 表示 current 已准备但当前 Gate 尚未获得指定审阅决定；
`FAIL` 表示合同、路径、SHA、binding 或 validator 失败；`BLOCKED` 表示缺能力或外部条件；
`SKIP` 表示真实步骤未执行。child、worker、技术 manifest、agent findings、“用户没有反对”或“AI 没有报错”均不得直接推出或写入批准。

coordinator 是唯一正式写者：只有它能发布 current 并写 manifest、timeline、SRT、identity、
stale、checkpoint 与 approval。candidate/旧 attempt 失败时保留诊断证据，但不得覆盖旧 current。

### 1.1 Gate 决策模式

新项目先以 `project.json.initialApproval.status=pending` 创建。项目 loader 将其投影为 `pending_initial_approval=true` / `initial_approval_completed=false`；旧正式项目缺少 `initialApproval` 时兼容为已完成。pending 项目只允许阶段 0 review、current 样音及修订、联合批准；完整旁白、图片、annotation、render、merge、burn、mux、final 的每个入口都必须硬拒绝。该 marker 是最小持久化边界，不引入第二套状态机。

阶段 0 的一次用户回复必须绑定 current content identity 与旁白项目 current `SAMPLE_IDENTITY`。原子动作重验 pending、identity、完整句选项和能力条件，成功时一次写入 BGM、agent 模式、生图方式、sample approval 与 `initialApproval.status=approved`；失败不留部分状态。审计 basis 固定区分 `user_joint_content_and_sample`、`user_joint_initial_approval` 与静音 `user_joint_silent_plan`。

`project.json.agentApprovalEnabled` 缺失或为 `false` 时保持人工批准。为 `true` 时，用户已用 current 样音完成唯一声音主观 Gate；full/final 在严格技术证据 current 后用最小 `approvalBasis/reviewBasis` 记录“用户样音授权后的技术推进”。视觉 Gate 仍需要真实查看。该字段不跳过技术 validator、current/identity/stale、既有批准脚本，也不新增 identity、manifest、状态机或恢复协议；basis 不进入作品 identity。

- 人工模式：coordinator 交付 current artifact/identity，等待用户完成所需真实审阅并明确确认。
- 自主视觉模式：coordinator 必须证明实际完成了本 Gate 要求的查看图像或观看视频，对
  current identity 作出独立明确决定；具备所需媒体能力的 child 可完成冻结 scope 内的真实检查，但
  child 始终 `approvalWritesAllowed:false`，coordinator 仍须重验其 scope、result、current binding 和具体 findings 后
  自行决策并调用原批准脚本。`completed`、无 findings、技术 PASS 或关键帧/contact sheet 都不能单独成为批准。
- AI 决定驳回时，只使受影响阶段及下游 stale，按既有 attempt/恢复语义返工并重审，不因普通返工
  等待用户。若修复必须实质改变阶段 0 已冻结的内容、target、cue/scene、图片提示词、BGM 选择
  或其他用户意图，必须回到阶段 0 取得用户新的明确确认。
- 视觉宿主缺少实际查图/观看视频能力时必须 `BLOCKED`。自主 full/final 不因缺听音能力阻塞，但必须只报告技术推进；metadata、SHA、ffprobe、完整解码和固定测试都不能冒充真实听审。

## 2. Identity 组成与 current binding

identity 是规范化业务输入与合同版本的 SHA-256，不包含创建时间、机器路径、worker/agent
并发等调度字段。正式使用前必须同时复核：

- contract/schema version、输入和派生文件 SHA-256、bytes；
- 产物格式、解码/技术 validator 结果和项目/scene 顺序；
- 当前 source、timing、render profile、provider synthesis/image binding；
- 所需 Gate 批准是否绑定同一个 current identity。

任何字节、规范化输入、合同版本、provider 参数、字幕/时间轴、render profile 或批准绑定
变化都不能沿用旧 identity。调度并发变化若不在作品内容合同中，只记运行审计，不改变 identity。

## 3. Stale 传播矩阵

| 变化 | 必须判 stale | 可保留 |
| --- | --- | --- |
| topic/body/rewritePolicy/target/narration cue/scene mapping | content/source、voice plan、音频、timeline、SRT、图片/annotation/视频及批准 | 仅按新 identity 重新判定可复用段 |
| 仅 `imagePrompt`/正式 `prompt` 改变，cue 与 scene boundary 不变 | generation plan、图片及视觉下游 | current 音频、timeline、narration SRT |
| voice/rate/朗读文本/分段/provider synthesis contract | sample/full 批准、受影响音频、timeline、SRT、annotation、视频和最终批准 | identity 未变的其他 segment |
| source 仅改时间或 narration WAV 改变 | 时长决定、full approval、timeline、SRT、annotation 与视频下游 | 未受影响的图片/合成段 |
| 仅 ASR/对齐/语义切句合同或 narration SRT 改变，且 scene 全局边界逐项不变 | full approval、字幕烧录、captioned/final 与最终批准 | current canonical WAV、图片、annotation、scene bundle、clean master；按新 binding 重验 |
| ASR/对齐修复使任一 scene 尾音边界改变 | full approval、timing plan、annotation 时序、scene bundle、clean master、字幕、final 与相关批准 | current canonical WAV、图片 generation plan/manifest |
| timing plan、render profile、字幕源/样式/字体或编码 contract 改变 | 受影响 annotation、scene/video、subtitle/final 与批准 | 未绑定输入的上游候选，需重新 binding |
| `backgroundMusic.enabled`、内置 BGM 字节或固定混音参数改变 | final 与最终批准 | current 旁白、timeline、画面和字幕 |
| pending 预项目的 content identity、voice/rate 或 sample identity 改变 | 初始联合 choice、sample approval 及受影响下游 | 不受影响的历史候选仅作证据 |

stale 文件可留作历史证据，但不得作为 current 输入；批准必须重新绑定新 identity。

## 4. Attempt 与恢复

每次外部调用或候选生成使用新的 attempt 目录；逻辑 task/scene ID 在 retry 间保持稳定，
旧 attempt 不原地覆盖。状态至少使用：`prepared → requesting → candidate_ready →
publishing → validated`；明确失败使用 `failed`，用户取消使用 `cancelled`。

恢复只相信 manifest/attempt 登记的状态和可信路径，不扫描 `.work` 猜测结果：

- `prepared`/`not_started` 可安全派发；
- `requesting` 且完整 candidate 与 receipt 存在时先做 binding/deep validator，provider
  调用数为 0；
- `candidate_ready`/`publishing` 可从 candidate 或字节完全相同的已发布文件继续，调用数为 0；
- `validated` 只完成必要清理或 current binding，不伪报为本次新生成；
- manifest、文件 SHA/bytes 或输入 identity 不一致时失败并保持旧 current。

## 5. Retry 与 `unknown_external_outcome`

自动重试仅针对已明确失败且合同允许的瞬态外部错误（网络连接、明确 timeout、408/429、
502/503/504 等），次数有限并采用现有退避。参数/合同错误、401/403/404、非法响应、内容
安全失败、路径冲突、媒体无效、缺少 FFmpeg、身份变化和用户取消不得自动重试。

`requesting` 已发出但结果不确定、candidate/receipt 不完整，或 provider 无法按同一幂等键
查询时，必须写 `unknown_external_outcome`：

- 不得自动重试、自动切 provider 或被 `--retry-failed` 绕过；
- 旧 current 保持不变，attempt 保留供诊断；
- coordinator 必须在新的外部请求前取得用户明确授权，并记录承担重复费用/重复结果的决定；
- 仅在用户授权且新 attempt 输入/identity 重新冻结后，才可发起新的 provider 请求。

`agentApprovalEnabled=true` 不扩张外部授权。冻结计划内的正常有界请求、合同允许的瞬态重试和
普通受影响阶段返工可继续执行；下列事项仍必须停止并取得用户的针对性明确授权：

- `unknown_external_outcome` 之后发起可能重复计费或重复结果的新请求；
- 冻结计划外的额外付费调用、新 provider/新外部服务、新凭据或未授权账号；
- 版权层修改/移除、素材许可变更或其他需要权利人授权的行为；
- 实质改变阶段 0 已冻结用户意图。

`--retry-failed` 只能选择 manifest 明确登记为 `failed`、`cancelled` 或 stale 且允许重试的
unit；不得把缺失/损坏状态升级成 failed 来绕过 fail-closed。

## 6. 原子发布与失败码

候选在本次 `.work/<run-id>` 内生成、解码和校验；同卷临时文件 flush/fsync 后 `os.replace`，
再复核正式 SHA/bytes 与 current binding。候选失败、发布失败或 manifest 更新失败均不得覆盖
旧正式文件，并保留诊断目录。

沿用现有失败码含义：`0` 技术成功；`1` 批处理有失败/取消；`2` 参数/schema/输入无效；
`3` 外部网络/服务或限流重试耗尽；`4` FFmpeg/媒体验证失败；`5` stale、identity 不匹配
或缺少 current Gate 批准。具体 CLI 可以扩展摘要，但不能弱化这些 fail-closed 边界。

## 7. 阶段引用

- 图片 provider、并发与候选发布：[`image-generation.md`](image-generation.md)
- Edge/MiniMax 样音、完整旁白和 timeline：[`voiceover.md`](voiceover.md)
- 字幕、烧录与最终媒体：[`subtitles.md`](subtitles.md)
- 阶段 0 草案和内容 identity：[`phase-0-content.md`](phase-0-content.md)
