# SRT 白板动画 Skill 完整优化方案

**版本**：v1

**制定日期**：2026-08-20

**适用范围**：`srt-whiteboard-animation` skill、其 references、README、workspace 配置、图片/TTS/annotation/正式渲染/媒体验证脚本与测试。

**当前能力基线**：正式多幕 `sceneRender` 并发已经是当前能力，不是未来 Phase 8 占位能力。

**文档性质**：这是执行计划，不是“立即修改全部代码”的授权。每个阶段都必须有明确改动范围、测试、验收和回滚边界。

---

## 1. 目标与结论

本计划要解决的是四类问题：

1. **合同漂移**：主 `SKILL.md`、README、references、历史性能计划和实现对同一能力的描述不一致。
2. **上下文负担**：主 skill 与 references 约 11 万字符、约 2.75 万粗略 token，视觉拓扑、并发、人工批准和 stale 规则在多处重复。
3. **重复工作**：连续阶段会重复构建全局验证上下文、重复读取/解码/计算 SHA，正式渲染 worker 也会重复加载固定素材。
4. **速度缺少测量闭环**：当前已经有并发正确性测试，但还缺少对 wall time、峰值内存、FFmpeg 负载和外部调用数量的固定基准。

本计划明确不做以下事情：

- 不删除或合并样音、完整旁白、线稿、annotation、scene bundle、最终成片等人工确认关卡。
- 不把技术 `PASS`、agent findings、candidate 或“用户没有反对”变成人工批准。
- 不允许 worker/subagent 写正式 manifest、identity、stale、checkpoint 或批准。
- 不把 `sceneRender` 并发配置写入作品 identity。
- 不自动重试 `unknown_external_outcome`，不自动切换 provider。
- 不用“批量优化”绕过 current binding、原子发布和 fail-closed 语义。
- 不把历史设计计划直接当作当前实现合同；当前能力以本计划第 2 节和代码/测试证据为准。

---

## 2. 当前真实合同（先冻结，再优化）

### 2.1 正式多幕并发已经启用

正式入口是 `scripts/render_stream_whiteboard.py`：

- `execution.concurrency.sceneRender` 读取自共享 workspace loader。
- `effectiveSceneRenderConcurrency = min(configuredSceneRenderConcurrency, readySceneCount)`。
- `sceneRender > 1` 时，独立单幕 candidate 通过有界 `ProcessPoolExecutor` 并行生成和 deep validation。
- worker 只能写本次 `.work` 下的 candidate 和结果；不得写正式 `scenes/*.mp4`、共享 manifest、identity 或批准。
- coordinator 在 worker 完成后按 generation plan 顺序复核 current image/annotation/hand binding，并原子发布正式场景。
- worker 完成乱序不改变正式发布顺序。
- 单幕失败不会取消其他独立幕；部分成功时 batch 仍为 `FAIL`，不能进入全量 scene review。
- 全部 current scene 形成有序 scene review bundle，并在用户明确批准后才能 `merge_scenes.py`。
- 并发字段只进入运行审计，不进入 scene、clean master、subtitle、final identity。

当前实现证据：

- `scripts/render_stream_whiteboard.py::_execute_formal_candidate_tasks()`。
- `scripts/render_stream_whiteboard.py::_run_formal_batch()`。
- `tests/test_scene_render_concurrency.py`。
- `tests/test_render_timing.py`。

### 2.2 必须清除的旧说法

以下表述不再代表当前能力，实施 Phase 0 时必须清理或明确标注为历史设计：

- “Phase 8 多幕正式候选并发仍未实施”。
- “`sceneRender=1`；场景只串行渲染”。
- “当前版本 `sceneRender` 无条件只能为 1”。
- “正式多幕并发属于未来设计备忘”。

重点检查：

- `references/subtitles.md`。
- `docs/superpowers/plans/` 下 2026-08-15 并发计划中的历史状态段落。
- README 的配置和流程说明。

历史计划可以保留，但必须加醒目标记：

```text
历史设计状态（2026-08-15）：当时尚未开放正式 sceneRender 并发。
当前实现状态请以 SKILL.md、scripts/render_stream_whiteboard.py 和测试为准。
```

### 2.3 并发的安全默认值与示例值分离

当前 `workspace.example.json` 展示了偏性能型配置，而 README 又建议首次运行使用全 1。优化后必须区分：

- **安全基线**：首次运行不主动增加外部请求和本机负载。
- **性能示例**：用户明确选择后使用，必须说明 CPU、内存、磁盘、provider 限流和费用影响。

不在本计划中未经测量地承诺“某个固定并发一定最快”。

---

## 3. 优化原则

### 3.1 规则单一来源

机器合同只能有一个权威来源；README 和 `SKILL.md` 是面向执行的派生视图；历史计划是历史记录，不得反向覆盖当前实现。

建议建立如下责任边界：

| 内容 | 权威来源 | 派生/说明来源 |
|---|---|---|
| schema、字段、identity | Python validator/contract module | references、README |
| 当前阶段流程 | `SKILL.md` 阶段路由 | README 快速开始 |
| child role 规则 | 冻结 `role-contract.md` 模板 | orchestration reference |
| workspace 配置 | `project_workspace.py` loader | `workspace.example.json`、README |
| 并发语义 | loader + worker batch implementation | SKILL、README、reference |
| 人工批准边界 | approval scripts + identity validators | SKILL、README |

### 3.2 安全性不因提速降级

任何优化必须证明以下顺序仍然成立：

```text
candidate created
→ result/receipt validated
→ business validator PASS
→ current binding rechecked
→ coordinator atomic publish
→ current technical
→ user explicitly approves current identity
```

### 3.3 先测量，再提高默认并发

并发是资源策略，不是质量保证。每一阶段都要记录：

- configured concurrency；
- effective concurrency；
- peak active workers；
- task count；
- wall time；
- peak RSS（可测时）；
- provider request count；
- retry count；
- unknown external outcome count；
- adopted candidate count；
- 输出 identity/SHA 是否与串行基线一致。

### 3.4 主窗口只消费摘要

主窗口不得回灌完整正文、完整 prompt、全部图片、完整 JSON、长日志或重复 validator 输出。每个阶段只返回：

- status；
- current/stale/approval 状态；
- identity；
- 计数；
- 异常 scene/unit ID；
- 可点击 artifact 路径或 verified preview URL；
- 下一步和人工关卡。

---

## 4. 目标架构

### 4.1 文档三层结构

将当前约 604 行的 `SKILL.md` 收敛为“路由和不变量”，目标约 180–250 行；详细规则按阶段分层。

建议目录：

```text
SKILL.md                              # 触发、路由、总 Gate、核心不变量、命令索引
references/
  phase-0-content.md                  # topic/text、自然旁白、草案审阅
  phase-1-srt-storyboard.md           # SRT 严格解析、传统分镜
  phase-2-project-and-timing.md       # project/timing/generation plan
  phase-3-voiceover.md                # sample/full/真实时长/Edge 恢复
  phase-4-images.md                   # provider、image candidate、线稿 review
  phase-5-annotation.md               # annotation candidate、preview、联合批准
  phase-6-scene-render.md              # sceneRender 并发、deep receipt、发布顺序
  phase-7-subtitles-final.md           # merge、字幕、mux、final validation/approval
  orchestration.md                    # agent task/result、runtime capability、fallback
  recovery-and-identity.md            # stale、identity、retry、unknown outcome
  prompt-writing.md                   # 视觉拓扑、prompt 字段映射、旁白提示词
  annotation-drafting-role.md         # child 冻结 role contract
```

拆分时不改变 schema 和人工关卡，只改变文档承载方式。

### 4.2 统一阶段摘要

每个批量脚本输出统一摘要字段：

```json
{
  "contractVersion": "...",
  "status": "PASS | FAIL | BLOCKED | SKIP | 待确认",
  "projectId": "...",
  "runId": "...",
  "taskCount": 0,
  "configuredConcurrency": 1,
  "effectiveConcurrency": 1,
  "peakConcurrency": 1,
  "successCount": 0,
  "failureCount": 0,
  "partialSuccess": false,
  "currentIdentity": "...",
  "approvalWritten": false,
  "userConfirmationRequired": true,
  "nextGate": "...",
  "failures": []
}
```

各阶段可以附加专属字段，但不能重新发明同义字段，例如不要同时使用 `configured`、`configuredConcurrency`、`configuredWorkerCount` 表示同一含义。

### 4.3 FormalValidationContext receipt

连续命令间增加可复用、短生命周期的验证 receipt：

```text
project/.work/formal-context-<run-id>/receipt.json
```

receipt 至少绑定：

- projectId；
- generation plan SHA；
- timing plan SHA；
- render profile SHA；
- active timeline SHA；
- voice manifest/full approval identity（Edge）；
- 全部 annotation SHA；
- validator contract version；
- `createdAt`、`expiresAt`；
- resolved scene IDs 和顺序。

receipt 只能跳过相同 run 内已完成的重复深验，不能越过当前 binding 复核。任何文件字节、全局证据或 validator contract 改变，都必须丢弃 receipt 并重新深验。

### 4.4 统一 candidate receipt/binding

图片、annotation preview、WAV、MP4 candidate 都应逐步统一为：

```json
{
  "candidateSha256": "<64 hex>",
  "candidateBytes": 0,
  "decoded": true,
  "format": "PNG | WAV | MP4",
  "validatorContract": "...",
  "validatedAt": "..."
}
```

worker 生成 candidate 并返回 receipt；coordinator 发布前复核 receipt 与 current binding；原子发布后只做 binding 验证，不无条件重复 full decode。`--force-deep`、receipt 缺失、字节变化或合同版本变化时才重新深验。

---

## 5. 分阶段实施计划

## Phase 0：合同、文档和配置收敛

### 目标

先消除“同一能力多种说法”，不改变业务行为。

### 改动范围

- `SKILL.md`
- `README.md`
- `references/subtitles.md`
- `references/subagent-orchestration.md`
- `references/image-generation.md`
- `config/workspace.example.json`
- 历史性能计划中的状态说明
- 新增文档一致性检查脚本或测试

### 具体任务

1. 将正式多幕 `sceneRender` 并发写成当前能力。
2. 删除/标注“Phase 8 未实施”“只能为 1”的旧表述。
3. 在 README 中写明并发计算、顺序发布、部分失败和 scene review Gate。
4. 将安全基线配置与性能示例分离，或明确示例文件不是首次运行默认。
5. 补齐 README 正式流程中的：
   - `approve_annotation_review.py`；
   - `approve_scene_review.py`；
   - Edge sample/full/approve；
   - `mux_voiceover.py`；
   - `approve_final_media.py`。
6. 明确 `merge_scenes.py` 的正式输入以 current approved bundle 为权威。
7. 增加字段映射说明：
   - content draft 使用 `imagePrompt`；
   - formal generation plan 使用 `prompt`；
   - coordinator 负责确定性映射。

### 验收

- `rg` 不再在当前文档中找到未加历史标记的“sceneRender 未实施/只能为 1”。
- README 的正式路径可以从输入走到最终批准，不遗漏必需 Gate。
- 示例配置与 README 首次运行建议不矛盾。
- 新增测试检查关键合同词和当前实现状态至少有一处可机器验证的来源。
- 359 项既有测试仍全绿。

### 回滚

纯文档/示例配置变更可独立回滚；不得回滚代码行为而保留新的并发合同描述。

---

## Phase 1：主 Skill 上下文减负与 prompt 单一来源

### 目标

减少主窗口必须承载的规则数量，降低重复、漂移和 child prompt 误解。

### 具体任务

1. 将 `SKILL.md` 收敛为：
   - 触发条件；
   - 输入组合；
   - 7 个阶段；
   - 人工 Gate；
   - 10–15 条核心不变量；
   - 当前能力和失败边界；
   - reference 路由；
   - 命令索引。
2. 将视觉拓扑规则集中到 `references/prompt-writing.md`。
3. 将旁白写作规则集中到 `references/phase-0-content.md`。
4. 将 stale/identity/retry 规则集中到 `references/recovery-and-identity.md`。
5. role contract 只保留 child 必须执行的局部规则，不复制整个产品合同。
6. 在主 skill 中加入“当前阶段只读取哪些 reference”的表格。
7. 给每个冻结 role contract 写入 contract version 和 SHA，继续保留 attempt 冻结语义。

### prompt 规则整理

统一使用以下抽象描述：

```text
禁止用跨区域贯穿性结构把本来可以独立揭示的视觉簇连接成一个整体。
道路、河流、山脉、箭头等仅是解释性示例，不是全局固定禁用对象。
如果该结构本身就是本幕不可分割的核心语义，可以保留。
```

修正 storyboard role 中 `prompt` 与 `imagePrompt` 的混用，加入明确映射表和反例。

### 验收

- `SKILL.md` 目标不超过 250 行，且不丢失人工 Gate、sceneRender 当前能力、失败码和关键不变量。
- 直接引用 references 的重复规则减少；同一规范只保留一个权威版本。
- child prompt 不包含完整主对话、完整 SRT、provider 配置、凭据、长日志或批准信息。
- content drafting、storyboard planning、annotation drafting 的 candidate schema 测试继续通过。
- 抽取 3 个典型输入（SRT、topic、text）人工检查流程指向没有歧义。

### 回滚

保留旧 references 的 Git 历史；如果拆分导致某阶段缺合同，恢复引用链接而不是将全部内容重新复制回 `SKILL.md`。

---

## Phase 2：统一 FormalValidationContext 与 candidate receipt

### 目标

减少连续阶段的重复深验和大文件重复读取，同时保持独立 CLI fail-closed。

### 改动范围

- `scripts/render_timing.py`
- `scripts/validate_annotations.py`
- `scripts/generate_annotation_previews.py`
- `scripts/serve_preview.py`
- `scripts/image_generation.py`
- `scripts/generate_images.py`
- `scripts/audio_normalization.py`
- `scripts/generate_voiceover.py`
- `scripts/render_stream_whiteboard.py`
- `scripts/media_validation.py`
- 新增 receipt/binding 共用模块

### 具体任务

1. 新增统一 `validation_receipts.py` 或等价模块：
   - canonical receipt schema；
   - receipt SHA；
   - current binding 检查；
   - expiry 和 validator contract 版本检查。
2. `serve_preview --ensure` 成功后可写入 FormalValidationContext receipt。
3. annotation preview batch 接受同 run receipt；receipt 失效时自动完整验证。
4. 图片 preview candidate 返回 PNG receipt，发布函数复用该 receipt。
5. 音频和 MP4 candidate 逐步迁移到同一 receipt 字段命名，但保持现有 manifest 兼容视图。
6. 明确 deep 与 binding：
   - 新字节、receipt 缺失、合同变更、显式 `--force-deep` → deep；
   - SHA/bytes/receipt 完全匹配 → binding；
   - binding 失败不得降级为 PASS。
7. 每个 receipt 记录 validator contract version，避免旧验证证据被静默复用。

### 预览发布优化

保留以下安全动作：

- candidate 生成后至少一次完整解码；
- 写入前原子临时文件；
- flush/fsync；
- `os.replace`；
- 发布后 SHA/bytes binding。

允许删除的重复动作必须由 receipt 覆盖，不能仅因为“同一进程刚验证过”就跳过证据。

### 验收

- 相同字节、相同合同、相同 receipt 的重复运行不再重复 full decode。
- receipt 缺失或任一绑定改变时仍然 fail closed。
- annotation preview、图片、音频、MP4 至少各有一组“deep 后 binding 复用”的测试。
- 修改一个 annotation、timing plan、render profile、voice binding 或 validator contract 时，相关 receipt 自动 stale。
- 正式 identity 与批准结果不因并发或 receipt 复用而错误保持不变。

### 回滚

receipt 是内部技术证据；迁移期间保留旧 validator receipt 的读取兼容，但不能将旧 receipt 当作新 contract 的 current 证据。

---

## Phase 3：正式渲染并发的真实性能优化

### 目标

在已经支持的正式多幕并发上，减少 Windows ProcessPool 的重复初始化和过载风险。

### 具体任务

1. 建立固定 benchmark fixture：
   - 8 幕；
   - 1920×1080；
   - 固定 annotation；
   - 固定 hand 素材；
   - 短视频和中等时长各一组。
2. 测量 `sceneRender=1/2/4/5`：
   - wall time；
   - peak worker；
   - peak RSS；
   - FFmpeg 子进程数；
   - candidate 磁盘占用；
   - 输出 SHA/identity；
   - 失败和重试行为。
3. 若 benchmark 证明有效，给 ProcessPool 增加 worker initializer：
   - worker-local hand 素材缓存；
   - immutable render config 缓存；
   - render profile 缓存。
4. 不把完整 numpy 图片通过 pickle 传给 worker；图片仍在 worker 侧从可信路径读取。
5. 研究按 CPU/内存或配置 profile 给出建议并发，而不是无条件鼓励 16。
6. `ProcessPoolExecutor` 仍必须有界；不允许提交无限任务或创建 agent × worker 乘法。
7. 继续保持 coordinator 顺序发布和单写 manifest。

### 可选优化：自适应串行阈值

只有 benchmark 证实短 scene 下 ProcessPool 启动成本高于收益时，才增加：

```text
scene 数过少或总预计帧数低于阈值 → 串行
否则 → 使用 effective sceneRender
```

该阈值必须：

- 记录在运行摘要；
- 不进入作品 identity；
- 可通过配置/contract version 追踪；
- 有串行/并发输出一致性测试。

### 验收

- 并发 1 的行为和输出与现有基线一致。
- 并发 2/4 的峰值不超过配置。
- 完成乱序时正式输出仍按 generation plan 顺序。
- 任一 worker 失败时旧 current 文件不被覆盖。
- benchmark 报告不使用脆弱的固定毫秒断言，只记录可比较数据。
- 在目标机器上确认推荐配置，不把未经测量的 5/8/16 写成默认。

### 回滚

保留 `sceneRender=1` 作为运行时安全降级；如果 initializer 或自适应阈值出现错误，可关闭优化但不得关闭现有 candidate/commit/recovery 合同。

---

## Phase 4：流程编排与命令启动成本优化

### 目标

减少主窗口工具往返和 Python 子进程启动次数，但不跨越人工 Gate。

### 具体任务

1. 新增一个可选 coordinator runner，例如：

```powershell
<ENV_PY> scripts/run_phase.py --project <project> --phase annotation-preview
```

2. runner 只串联确定性步骤：
   - technical validation；
   - receipt 复用；
   - candidate generation；
   - preview/contact sheet；
   - summary 输出。
3. runner 遇到人工 Gate 必须停止，输出：
   - artifact link；
   - identity；
   - 当前状态；
   - 需要用户明确回复的格式。
4. runner 不接受“自动批准”参数，不读取聊天历史推断批准。
5. 连续确定性链路可以在一次 Python 进程中复用已加载的 project/config/context，但每个阶段仍保留可独立调用的 CLI。
6. 不把外部 provider 请求放进 agent task 内部，不改变图片 `continue_independent` 和 TTS `stop_dispatch` 策略。
7. README 同时提供：
   - 逐步调试命令；
   - 正式 coordinator runner；
   - 中断后恢复命令。

### 建议的人工 Gate 停止点

```text
阶段 0：内容与制作方案联合确认
阶段 1：传统 SRT 模式与分镜确认
阶段 3：样音确认
阶段 4：完整旁白、真实时长和 review policy 确认
阶段 5：线稿确认
阶段 6：annotation/区域/reveal 联合确认
阶段 7：scene bundle 确认
阶段 10：最终成片确认
```

### 验收

- runner 与逐步 CLI 产生相同正式 identity、manifest、timeline、SRT 和 receipt。
- runner 在每个人工 Gate 停止，不能自动进入下一阶段。
- 失败后只重做受影响阶段，不重复已验证且 binding current 的阶段。
- 运行摘要明确列出跳过了哪些 deep validation、为什么可以 binding。
- 没有 provider 请求重复、unknown outcome 被自动重试或旧批准被复用。

### 回滚

runner 是可选入口；任何时候都可以回到逐步 CLI，不能让 runner 成为唯一不可诊断的黑盒。

---

## Phase 5：质量、可维护性和 CI 收口

### 具体任务

1. 修复测试中的 `ResourceWarning`，例如用 `with Image.open(...)` 管理文件句柄。
2. 测试统一使用项目 runtime：

```powershell
& 'D:\SRTWhiteboard\runtime\.venv\Scripts\python.exe' -m unittest discover -s tests -q
```

3. 增加文档合同检查：
   - 当前 `sceneRender` 并发能力与文档一致；
   - README 正式路径包含必需 approval 命令；
   - 不出现未标记的旧 Phase 8 状态。
4. 增加 prompt schema 检查：
   - content draft 使用 `imagePrompt`；
   - generation plan 使用 `prompt`；
   - storyboard candidate 不出现错误字段；
   - 跨请求指代检查继续有效。
5. 增加 receipt stale 矩阵测试：

| 改动 | 预期结果 |
|---|---|
| image bytes | image/visual downstream stale |
| annotation bytes | preview/annotation approval stale |
| timing plan | audio timing/annotation/render downstream stale |
| render profile | render/subtitle/final stale |
| voice/rate/text | sample/full/audio/timeline/downstream stale |
| subtitle preset | subtitle/captioned/final stale |
| concurrency only | 不改变作品 identity |
| validator contract | 旧 receipt 不能 binding PASS |

6. CI 输出区分：
   - 自动 fixture PASS；
   - 真实 provider SKIP/BLOCKED；
   - 人工 Gate 待确认。
7. 文档和测试中的路径、URL、端口、退出码统一使用真实值。

---

## 6. 性能基准方案

### 6.1 固定数据集

建立不提交真实用户内容的 fixture：

```text
fixture-small：2 scenes，短时长
fixture-medium：8 scenes，多 annotation elements
fixture-large：16 scenes，多个 TTS units
```

### 6.2 需要记录的指标

每次 benchmark 生成 JSON，不只输出人类日志：

```json
{
  "fixture": "fixture-medium",
  "stage": "sceneRender",
  "configured": 4,
  "effective": 4,
  "peak": 4,
  "taskCount": 8,
  "wallMs": 0,
  "peakRssBytes": null,
  "providerRequests": 0,
  "retryCount": 0,
  "unknownExternalOutcomeCount": 0,
  "outputIdentityStable": true,
  "failurePolicyStable": true
}
```

### 6.3 性能比较规则

- 不设置“必须低于 X ms”的脆弱门槛。
- 比较同一机器、同一 fixture、同一 provider/fixture adapter。
- 首次运行和 warm run 分开记录。
- 区分 Python 进程启动、图片解码、FFmpeg 编码、磁盘写入和 validator 时间。
- 外部 provider 性能只能报告请求数、并发峰值、失败/限流，不把网络波动误算成本地优化收益。
- 并发运行必须与串行运行比较输出 identity/SHA 和发布顺序。

---

## 7. 失败恢复和安全验收矩阵

任何优化 PR 都必须至少覆盖以下情况：

| 场景 | 必须发生的结果 |
|---|---|
| worker 正常完成但正式文件已变化 | candidate 不发布，batch 失败/stale |
| worker 生成 candidate 后 FFmpeg 失败 | 旧 current 保留，candidate 留证或清理符合合同 |
| 部分 scene 成功、部分失败 | `FAIL + partialSuccess:true`，不启动全量 review |
| receipt 缺失 | 重新 deep validation，不降级 PASS |
| receipt SHA 不匹配 | stale/fail closed |
| provider `unknown_external_outcome` | 停止新派发，等待用户决定，不自动重试 |
| review approval identity 不匹配 | 退出码 5，不写 merge candidate |
| user 保存 annotation | 只写 current technical，旧人工批准 stale |
| concurrency 改变 | 运行审计变化，作品 identity 不变 |
| timing/render/profile 改变 | 对应下游全部 stale |
| 文档与代码状态不一致 | CI/文档合同检查失败 |

---

## 8. 交付节奏与 Git 检查点

建议拆成 5 个独立 implementation cycles，每个 cycle 都可单独 review 和回滚：

### Cycle 1：合同同步

只改文档、示例配置、文档合同测试。不得改业务行为。

### Cycle 2：文档拆分与 prompt 收敛

只改 `SKILL.md`、references、role contract 模板和对应 schema 测试。

### Cycle 3：receipt/context 去重

先落地内部 receipt 和 annotation/preview binding，再迁移图片/音频/MP4。

### Cycle 4：渲染性能基准与 worker 优化

先提交 benchmark 数据，再决定 initializer、自适应阈值和建议并发。

### Cycle 5：可选 coordinator runner 与 CI 收口

runner 不替代现有 CLI；完成 warning、文档合同、stale 矩阵和完整回归。

每个 cycle 的提交信息使用中文，建议格式：

```text
计划：同步正式多幕并发合同
计划：收敛白板动画 Skill 上下文与提示词
计划：增加 candidate receipt 与验证证据复用
计划：建立正式渲染并发基准
计划：增加阶段编排入口与合同回归
```

---

## 9. 最终完成标准

以下条件全部满足，才可将本计划标记为完成：

- [ ] 当前正式多幕 `sceneRender` 并发在 SKILL、README、references、配置说明和测试中一致。
- [ ] 未加历史标记的文档不再声称 Phase 8 未实施或只能串行。
- [ ] README 正式路径包含所有必需人工批准和 Edge mux/final 命令。
- [ ] `SKILL.md` 已收敛为路由与核心不变量，详细合同按阶段引用。
- [ ] `imagePrompt` → formal `prompt` 字段映射唯一且有测试。
- [ ] 视觉拓扑规则单一来源，示例对象不会被误读为全局固定禁词。
- [ ] FormalValidationContext receipt 可在相同 run 内复用，绑定变化会失效。
- [ ] 图片/preview/WAV/MP4 candidate 至少有统一 receipt/binding 兼容层。
- [ ] 重复 full decode、重复大文件 SHA 和重复全局验证有可量化下降。
- [ ] `sceneRender` 已有 1/2/4 等配置的真实性能基准。
- [ ] 并发 1/2/4 的正式输出、发布顺序和失败恢复测试通过。
- [ ] 没有任何优化绕过人工 Gate、unknown outcome 授权或 coordinator 单写。
- [ ] 359 项现有测试保持全绿，并新增合同、receipt、benchmark 和 stale 矩阵测试。
- [ ] ResourceWarning 已清理，测试命令和 runtime 使用方式写入 README。
- [ ] 自动 fixture、真实 provider、真实媒体和人工验收边界继续分别报告。

---

## 10. 推荐的第一步

先实施 **Cycle 1 / Phase 0**，只做合同和文档同步：

1. 以“正式多幕并发已经启用”为当前能力基线。
2. 清理所有旧的 Phase 8 未实施说法。
3. 修正 README 正式生产路径。
4. 统一安全基线配置与性能示例。
5. 增加文档合同回归测试。

这一步风险最低，却能立刻消除当前最严重的误导。完成后再进入上下文拆分和 receipt 去重，避免在错误合同上继续优化实现。
