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

## 2. 阶段流程与唯一人工 Gate

1. coordinator 冻结输入模式、rewritePolicy 和 target；旁白 provider 不询问用户，始终
   读取 skill 根目录 `config/voice-providers.local.json` 的 `activeProvider`，规范化后自动
   冻结为 `voiceoverMode=edge-tts` 或 `voiceoverMode=minimax`。review 只展示当前已采用的
   provider；只有用户明确要求静音时，传统 SRT 才允许显式使用 `disabled`。
2. 在允许真实派发时，由 `contentDrafting` child（或同合同 fallback）生成
   `whiteboard-content-draft-v1` candidate。child 不调用 provider、不写正式项目。
3. coordinator 校验 candidate，确定性渲染 review Markdown，只交付链接、identity、
   cue/scene 计数和短摘要，不把长正文回灌主上下文。
4. **停止等待用户明确确认 current `contentDraftIdentitySha256`。** 此次确认同时
   覆盖旁白内容、cue→scene、分镜策略和图片提示词；技术 PASS、打开文件或用户未反对
   都不是批准。
5. 修改意见冻结为绑定 current identity 的 revision request，创建新 attempt；旧
   candidate/review 保留为历史并 stale。只有同一冻结 attempt 的执行性缺漏才允许 followup。
6. 确认后才运行 `prepare_source.py`，确定性复核 provisional SRT/plan 与已确认方案一致，
   再创建正式项目；出现实质差异必须回到本 Gate。

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

阶段 0 不写正式 identity、manifest 或批准。命令细节、schema allowlist、revision request
和只读校验见 [`content-input.md`](content-input.md)；child 最小上下文和 dispatch 边界见
[`subagent-orchestration.md`](subagent-orchestration.md)。

## 6. 阶段 0 验收

自动测试应覆盖三类输入路由、非法组合、rewritePolicy 保真、cue/scene 连续性、确定性
candidate identity 和“未确认不建项”。fixture PASS 不等于用户批准、Edge 可用、真实声音
接受或线稿审美通过；这些状态必须分别报告为待确认、BLOCKED 或 SKIP。
