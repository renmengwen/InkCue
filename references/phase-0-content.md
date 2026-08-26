# Phase 0：主题 / 正文内容与旁白写作

合同版本：`whiteboard-phase-0-content-v1`

本文件是阶段 0 的流程、自然中文旁白和内容草案审阅规则的唯一规范来源。
`references/content-input.md` 保留脚本/schema 细节，但旁白写法、两遍回读、内容与
制作方案联合 Gate 以本文为准；视觉拓扑统一见
[`prompt-writing.md`](prompt-writing.md)。

## 1. 输入路由

外部输入只能是 `inputMode=srt | topic | text`：

| 输入 | rewritePolicy | voiceoverMode 来源 | 阶段 0 行为 |
| --- | --- | --- | --- |
| `srt` | 不适用 | 默认读取 `activeProvider`；明确静音时为 `disabled` | 不改写用户字幕；走传统 SRT 模式/语义分镜确认 |
| `topic` | 仅 `generate` | 自动读取 `activeProvider` | child 生成完整旁白、cue、scene 与 `imagePrompt` candidate |
| `text` | `preserve\|polish` | 自动读取 `activeProvider` | 按策略保真或局部润色，再生成 cue、scene 与 `imagePrompt` candidate |

拒绝 `topic+preserve`、`topic+polish`、`text+generate` 和非 SRT 的
`voiceoverMode=disabled`。`targetDurationSeconds` 只用于内容预算和 provisional SRT；
topic/text 的正式时钟由获批真实音频 timeline 接管。

## 2. 阶段 0 预项目、样音与一次联合 Gate

1. coordinator 冻结输入模式、rewritePolicy、target；旁白 provider 不询问用户，始终
   读取 skill 根目录 `config/voice-providers.local.json` 的 `activeProvider`，规范化后自动
   冻结为 `voiceoverMode=edge-tts`、`voiceoverMode=minimax` 或 `voiceoverMode=doubao`。review 只展示当前已采用的
   provider；只有用户明确要求静音时，传统 SRT 才允许显式使用 `disabled`。BGM 只允许“加入/不加入”，加入时固定使用内置 CC0 曲目与固定混音参数；后续模式只允许“逐阶段由我确认/后续由 AI 自主推进至成片”。它们不在创建 pending 预项目时预写，而是在用户选择完整句后由联合动作原子冻结。只有当前登录态 `image_gen` 与已配置图片供应商同时真实可用时，才把两种生图方式并入 8 个通过句；只有一种可用或已固定时不把生图方式作为选择轴。不可用组合不得展示。
2. 在允许真实派发时，由 `contentDrafting` child（或同合同 fallback）生成
   `whiteboard-content-draft-v1` candidate。child 不调用 provider、不写正式项目。
3. coordinator 校验 candidate，确定性渲染 review Markdown，确定性派生 source package，并创建 `project.json.initialApproval.status=pending` 的 `pending_initial_approval` 预项目。确认前创建的是受限预项目，不是可执行下游的正式项目。
4. 预项目中只允许阶段 0 审阅、样音生成/技术验证、草案或 voice/rate 修订与联合批准。coordinator 生成绑定 current content identity、voice plan 与项目的真实样音，向用户交付 review 与不可变样音副本。完整旁白、正式生图、annotation、render、merge、burn、mux、final 必须在入口调用统一 pending guard，不能只依赖 coordinator 记忆。
5. coordinator 调用 `build_initial_approval_options()` 按当前真实能力生成完整自然语言句子。固定生图方式时必须逐字列出四个旁白通过句；仅当登录态 `image_gen` 与已配置图片供应商同时可用时，才把生图方式并入每条通过句并枚举 BGM × 后续模式 × 生图方式共 8 项。不可用组合不展示，active voice provider 只显示“当前已采用”。另列三条草案/样音定向返工句。传统 `disabled` SRT 不生成样音，使用“字幕与分镜方案通过……”语义。
6. **停止等待用户的一次合法联合回复。** 用户可复制完整句或回复编号；解析结果必须结构化绑定 current content identity 与 current `SAMPLE_IDENTITY`，不得从近义自由文本猜测。项目层重验 identity、pending、选项和能力条件后原子写入 `initialApproval.status=approved`、BGM、`agentApprovalEnabled`、`imageGenerationMode` 和 sample approval。审计值为 `initialApproval.approvalBasis=user_joint_content_and_sample`、`sampleApproval.approvalBasis=user_joint_initial_approval`；静音 SRT 使用 `initialApproval.approvalBasis=user_joint_silent_plan` 且没有 sample approval。任一校验失败不留下半批准状态。
7. 修改意见冻结为绑定 current identity 的 revision request，创建新 attempt；旧
   candidate/review 保留为历史并 stale。新 attempt 是版本边界，不要求更换执行者：上一
   `contentDrafting` child 仍存在、idle、上一结果 completed 且 role contract 兼容时，
   coordinator 优先 followup 原 child，并只交付新 task/base/revision 的路径与 SHA。原 child
   不可用、失败、role 改变、修订升级为全面独立重写或用户明确要求换执行者时才 spawn
   新 child；同一冻结 attempt 的执行性缺漏也 followup 原 child。
8. 修改内容、voice/rate 或 provider synthesis contract 时按 recovery 合同使受影响 content/sample/full/downstream identity stale；旧样音批准不得静默复用。用户明确要求新任务时总是新建预项目，不 resume 同名旧项目。

传统 SRT 复用同一 pending 预项目与联合动作；旁白 SRT 仍须 current 样音，`disabled` 静音 SRT 不要求样音且通过句使用字幕/分镜语义。旧项目缺少 `initialApproval` 时兼容为已批准，缺少 `imageGenerationMode` 时按 `provider`，缺少 `agentApprovalEnabled` 时按 `false`。

`agentApprovalEnabled=true` 的声音主观依据是用户已试听并批准的 current 样音。后续 full/final 仍执行全部严格技术校验，但不再因宿主不能听音而 `BLOCKED`，也不得声称 AI 完整听过；批准记录以最小 `approvalBasis/reviewBasis` 区分真实听审与“用户样音授权后的技术推进”。超过 10% 的真实时长按授权采用真实音频时钟。视觉 Gate 仍要求实际查看 current artifact；人工模式的 full/final 真实试听/看片听音 Gate 不变。`reviewPolicy` 在自主模式确定性为 `agent_first`。

以下事项仍须单独询问用户：`unknown_external_outcome` 后可能重复的新外部请求，阶段 0 冻结计划
之外的新费用、凭据或服务授权，版权授权，以及必须实质改变已冻结用户意图的修订。冻结计划内的
正常有界调用与常规返工不打断用户。该选择不引入新 identity、manifest、状态机或专用恢复协议。

## 3. 自然中文旁白（`natural-spoken-zh-v1`）

规则只约束 `narrationCues[].text`，不改 schema、`coreIdea`、`visualSubject` 或
`imagePrompt`。旁白应像熟悉主题的人直接解释：现代、简洁、可朗读，优先主语、动词和
可见对象；每个 cue 只推进一个新信息并与所属 scene 的核心命题一致。

### 3.1 策略边界

- `text + preserve`：只做 Unicode/换行规范化、拆 cue 和不改语义的标点调整；不换声口、
  增删信息、补例子或静默口语化。
- `text + polish`：可以局部改成自然可听的现代中文，但必须保持人物、数字、结论、
  因果方向、责任主体、完成态、证据强度和不确定性。
- `topic + generate`：围绕已确认主题创作具体、完整、可朗读的旁白；未知/推断/示例
  不能包装成已核验事实。
- `inputMode=srt` 不自动套用上述改写；用户若要求重写，先展示完整改稿并重新确认。

### 3.2 写作约束

- 直接进入具体问题、场景、动作或判断，避免连续抽象名词和空泛总结。
- 不为“像文案”而堆破折号、冒号、排比、路线词或固定句壳；真实操作顺序可以保留。
- 少用“下面我们来”“值得注意的是”“总的来说”等助手路线词；除非主题确实在做文本
  解读，否则不把“原文认为/材料说明”当作口播主体。
- 不添加无目的互动、口头禅、emoji、虚构数据、个人经历或多余情绪；例子必须明确是例子。
- 句长和节奏可自然变化；不通过连续同义词制造“人味”，也不强改已经自然的原稿。

## 4. 两遍回读

`contentDrafting` 提交 candidate 前必须完成：

1. **保真回读**：逐项核对原始 topic/body、人物、事实、数字、时间、结论、限定、不确定
   性、因果强度和责任主体；确认 cue 连续、每条有新信息且总量适合 target。`preserve`
   在此后停止风格处理。
2. **口播回读**：完整朗读，局部修正拗口句、抽象名词堆叠、重复解释、助手路线词、无目的
   收尾、同型句壳过密和节奏过匀；不能借机添加新事实。

回读不替代用户确认。草案批准后，后续 SRT/TTS/字幕只消费已确认文本；任何旁白文字变化
必须回到阶段 0 并重新走 Gate。

## 5. 草案、映射与下游边界

`candidate.content-draft.json` 是阶段 0 唯一机器权威源，Markdown 是 coordinator 的
确定性审阅视图，不得被下游反向解析为 source、plan、identity 或批准。topic/text
草案的 `scenes[].imagePrompt` 只能在用户确认后由 coordinator 按
[`prompt-writing.md`](prompt-writing.md) 的唯一映射转换为正式 plan `scenes[].prompt`；
child 不得改名、改写或直接调用 provider。

阶段 0 复用现有 project/source/voice/sample identity 与批准链，只新增一个 pending marker、统一下游 guard 和薄的原子联合批准动作；不建立第二套 sample manifest、preview identity、状态机或恢复协议。生图方式只路由现有图片阶段。命令细节、schema allowlist、revision request
和只读校验见 [`content-input.md`](content-input.md)；child 最小上下文和 dispatch 边界见
[`subagent-orchestration.md`](subagent-orchestration.md)。

## 6. 阶段 0 验收

自动测试应覆盖三类输入路由、非法组合、rewritePolicy 保真、cue/scene 连续性、确定性
candidate identity、pending 下游拒绝、联合动作原子性、4/8 个完整句、静音无 sample、缺失 `initialApproval` 时兼容为 approved、缺失 `agentApprovalEnabled` 时等价于 `false`，以及缺失 `imageGenerationMode` 时等价于 `provider`。fixture
PASS 不等于用户亲自批准、AI 代理批准、Edge 可用、真实声音接受或线稿审美通过；这些状态必须
分别报告为待确认、BLOCKED 或 SKIP。
