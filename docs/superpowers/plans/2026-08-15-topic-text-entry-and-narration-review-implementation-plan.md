# 主题/正文入口与配音字幕联合预审首版实施计划

> **已被 2026-08-17 当前合同部分取代：** 主题/正文入口继续有效；完整旁白后的 `narration-review.mp4`、review identity 与双 identity 批准已移除。当前流程只生成 WAV/timeline/narration SRT，用户完整试听 WAV 并确认真实时长，字幕视觉审查推迟到有真实画面的正式 contact sheet 与最终成片阶段。权威实现以 `SKILL.md`、`README.md` 和 `references/voiceover.md` 为准。

> 日期：2026-08-15  
> 状态：待实施  
> 适用 Skill：`srt-whiteboard-animation`  
> 工作目录：`C:\Users\MOVER\.codex\skills\srt-whiteboard-animation`  
> 正式产物根目录：`D:\SRTWhiteboard`  
> 基线说明：当前 Skill 目录不是 Git 仓库；实施必须保护现有文件，按文件所有权并发，不能依赖分支、commit 或 worktree 隔离。  
> 首版目标：让用户可以从“主题”或“一段正文”开始，由 Codex 先生成并确认旁白稿、cue 和分镜，再冻结内部 `source/source.srt`；Edge 模式在完整旁白生成时同步生成字幕和轻量联合预审视频，让用户边听配音边看字幕，并在进入生图/渲染前一次确认当前配音、字幕与真实时长。

## 1. 计划目的

当前 Skill 的外部起点和内部合同都是 SRT。SRT 在现有实现中同时承担：

1. 朗读文本与稳定 cue identity；
2. 语义分镜和初始 scene 范围；
3. `disabled` 模式的权威时钟与权威字幕；
4. `edge-tts` 模式在完整旁白获批前的 provisional source timeline。

用户希望把外部起点提升为“一个主题”或“一段话”，同时提出流程改进：完整旁白生成时能否把配音和字幕一起生成给用户确认，以减少最终阶段的返工。

本计划冻结的答案是：

- **可以把主题/正文作为用户入口。** 对外新增内容准备层，对内仍冻结严格 SRT，继续复用现有项目、TTS、字幕、渲染、identity、stale 和恢复合同。
- **可以且应该联合生成配音与字幕供用户预审。** 当前 Edge 完整旁白本来就同时产出 `narration.wav`、`timeline.json` 和 `narration.srt`；首版增加一个轻量 `previews/narration-review.mp4`，让用户完整播放时同时判断声音、字幕文本、字幕时序和真实时长。
- **联合预审不能替代最终字幕烧录。** 最终烧录依赖已确认的 `final-video-only.mp4`，在画面母版产生前无法得到最终画面上的字幕对比度、遮挡和安全区证据。联合预审节省的是后期修改旁白/字幕导致的整链返工，而不是省掉最终一次 FFmpeg 烧录。
- **不合并判断语义。** 用户可以在一个预审视频里完成观看，但批准记录仍要清楚绑定“完整旁白 + narration SRT + timeline + 真实时长 + 当前预审文件”，技术验证不能冒充人工确认。

## 2. 当前实现事实与改造边界

### 2.1 当前必须保留的事实

- `project.json.source.file` 当前必须是 `source/source.srt`，并校验 SHA-256。
- `create_project.py` 当前显式要求 `--srt`；该路径已经是稳定正式项目入口。
- `generate_voiceover.py` 从 source SRT 读取 cue 文本，并按 generation/timing scene 的 `sourceCueRange` 构造 speech units。
- Edge 完整旁白已经原子发布：
  - `audio/narration.wav`
  - `audio/timeline.json`
  - `audio/narration.srt`
- `approve-full` 已绑定 current WAV、timeline、narration SRT 和真实时长决定；本次只扩充“用户实际观看了哪一个联合预审文件”的证据，不另造一套平行批准系统。
- 正式字幕仍必须通过统一 ASS 编译和 `burn_subtitles.py` 烧录到 current clean master。
- 正式 Edge 成片仍是“先烧字幕、后以 `-c:v copy` 封装 AAC”，禁止 `-shortest`。

### 2.2 首版不推翻的边界

- 不删除内部 SRT。
- 不把 topic/body 直接塞进现有 `source` 字段代替 SRT。
- 不把 TTS 实际时长写回图片 `generation-plan.json`。
- 不在内容生成阶段自动批准旁白稿或分镜。
- 不因联合预审通过就跳过最终字幕 contact sheet 与最终成片完整看片听音。
- 不把预审视频当正式输出或最终交付证据。
- 不接入通用文本模型 provider；首版由当前 Codex 对话生成旁白稿和分镜，确定性脚本只负责校验、排时、持久化和派生文件。
- 不新增浏览器、Web UI、预览台或文件选择器作为必要流程。
- 不做逐词卡拉 OK、软字幕、多语言字幕、字幕编辑器、多角色或第二 TTS provider。
- 不做过度架构重写；先完成可以真实跑通的首版。

## 3. 首版产品合同

### 3.1 对外输入模式

```text
inputMode = srt | topic | text
```

| 模式 | 用户输入 | 内容策略 | 首版 voiceoverMode |
|---|---|---|---|
| `srt` | 现有 SRT 文件 | 保持当前严格校验 | `disabled | edge-tts` |
| `topic` | 一个主题 | Codex 生成完整旁白稿、cue、scene 与画面建议 | `edge-tts` |
| `text` | 一段或多段正文 | 按明确 rewritePolicy 保留或润色为旁白稿 | `edge-tts` |

首版有意限制：非 SRT 输入只支持 `edge-tts`。原因是 `disabled` 没有真实语音时钟，自动生成的阅读节奏会直接成为最终权威时钟，需要另一套专门的无音频字幕排时验收。该能力留到后续版本，不在首版中用估算时长冒充真实时长。

### 3.2 正文改写策略

```text
rewritePolicy = preserve | polish | generate
```

- `topic` 只允许 `generate`。
- `text + preserve`：保持原文、数字、人物、结论和段落意思，只允许规范化换行、拆 cue 和极少量不改变语义的口语标点处理。
- `text + polish`：允许改为更自然的旁白，但必须向用户展示完整新稿；确认前不得正式建项。
- `generate`：根据主题创作完整稿件；不确定事实不得伪装成已核验事实。

### 3.3 目标时长

- 非 SRT 输入必须有 `targetDurationSeconds`。
- 用户未提供时，Codex可以提出 60 秒建议值，但必须在内容关卡中明确展示并等待确认。
- 首版校验范围建议为 15–600 秒；不得接受布尔值、NaN、无限值或范围外数字。
- target 只用于内容预算和 provisional source SRT 排时；Edge 完整旁白获批后，真实 audio timeline 才是正式时钟。
- 实际音频相对 target/source provisional duration 偏差超过 10% 时，继续沿用 `accept_actual` 硬关卡。

## 4. 内容准备与持久化合同

### 4.1 新增内容草案格式

新增 `content-draft-v1` JSON。它由 Codex 在聊天中完成创作和分镜后写出；脚本不得自行调用文本模型。

建议结构：

```json
{
  "schemaVersion": 1,
  "contractVersion": "whiteboard-content-draft-v1",
  "inputMode": "topic",
  "topic": "为什么人会拖延",
  "body": null,
  "rewritePolicy": "generate",
  "targetDurationSeconds": 60,
  "voiceoverMode": "edge-tts",
  "narrationCues": [
    {
      "cueId": "cue-001",
      "sceneId": "scene-01",
      "text": "你有没有过这样的经历？明知道事情很重要，却总想再等一会儿。"
    }
  ],
  "scenes": [
    {
      "sceneId": "scene-01",
      "name": "拖延的表象",
      "coreIdea": "重要任务面前反复推迟",
      "visualSubject": "人物面对任务清单却转向轻松活动",
      "imagePrompt": "……"
    }
  ]
}
```

硬校验：

- 顶层只接受明确 allowlist 字段；未知敏感字段拒绝或至少不持久化。
- `topic` 与 `body` 分开保存，不能用其中一个覆盖另一个。
- 统一 NFKC、CRLF→LF、去除首尾空白，但不得静默改写正文语义。
- `topic` 建议上限 200 Unicode 字符；`body` 建议上限 128 KiB UTF-8。
- narration cue 文本不能为空；cue ID 连续、唯一；scene ID 必须存在。
- 每个 cue 只能属于一个 scene；scene cue 必须连续，不能跨越后再返回旧 scene。
- scene ID 必须是 `scene-01` 起连续编号。
- `topic` 必须有内容；`text` 必须有 body。
- `topic + preserve`、`topic + polish`、`text + generate` 等不合法组合按合同拒绝。
- JSON 中不保存 API Key、Cookie、Token、临时 URL、PID、本机绝对路径或完整模型内部响应。

### 4.2 新增确定性准备脚本

新增：

```text
scripts/content_source.py
scripts/prepare_source.py
```

职责划分：

- `content_source.py`
  - 定义/校验 `content-draft-v1`；
  - 规范化文本；
  - 计算 canonical JSON hash；
  - 将 narration cues 确定性排成 provisional SRT；
  - 从 scene/cue 映射生成满足现有校验器的 generation plan；
  - 不做网络请求，不批准草案，不创建正式项目。
- `prepare_source.py`
  - CLI 外壳；
  - 读取已由用户确认的 draft JSON；
  - 原子输出准备包；
  - 输出 `CONTENT_DRAFT_IDENTITY`、SRT 路径、generation plan 路径和摘要；
  - 失败时不覆盖上一次有效准备包。

建议命令：

```powershell
<ENV_PY> scripts/prepare_source.py `
  --draft <已获用户确认的-content-draft.json> `
  --output-dir <D:\SRTWhiteboard\drafts\项目名>
```

输出：

```text
<draft-dir>/
  input.json
  source.srt
  generation-plan.json
  manifest.json
```

其中 `manifest.json` 至少绑定：

- contract version；
- normalized input SHA；
- narration cue identity；
- source SRT SHA；
- generation plan SHA；
- target duration；
- inputMode/rewritePolicy；
- 不含秘密的创建时间和工具版本。

### 4.3 provisional SRT 排时算法

首版算法必须确定性，不依赖模型二次输出：

1. 以确认后的 narration cue 为最小单位，不在脚本里重新改写或重新分句。
2. 对每条 cue 计算有效中文/字母/数字字符权重；标点不计主要朗读权重，但句末停顿加入固定小权重。
3. 在每条 cue 的最短可读时长约束下，按权重分配 `targetDurationSeconds`。
4. 使用整数毫秒和“累计边界”分配，最后 cue 的 endMs 必须精确等于 target 毫秒，不能逐 cue 四舍五入后累加漂移。
5. cue 从 0 连续排列，首版不人为加入字幕空档。
6. 输出后必须立即通过共享 `parse_srt()` 严格 round-trip 校验。
7. generation plan 的 scene `subtitleRange` 和 `sceneDurationMs` 必须从生成后的 cue 边界派生，不从 draft 中信任重复的毫秒字段。

该 SRT 在 Edge 完整旁白批准前是 provisional source timeline；它不是最终真实语音时钟。

### 4.4 正式项目创建兼容

扩展 `create_project.py`，保留现有 `--srt` 用法，并增加可选：

```powershell
<ENV_PY> scripts/create_project.py --name <项目名> `
  --srt <draft-dir\source.srt> `
  --plan <draft-dir\generation-plan.json> `
  --source-input <draft-dir\input.json> `
  --source-manifest <draft-dir\manifest.json> `
  --voiceover-mode edge-tts
```

规则：

- `--source-input` 与 `--source-manifest` 必须同时出现；只允许新建项目，续接仍读取项目内冻结证据。
- 创建时重新校验 input/manifest/SRT/plan 的全部 hash 与绑定关系，不能只复制文件。
- 项目内新增：

  ```text
  source/input.json
  source/source-manifest.json
  source/source.srt
  ```

- `project.json` 新增可选 `contentSource` 绑定 input/manifest hash；旧 SRT 项目无该字段时保持完全兼容。
- 不为这项可选来源证据强制升级旧 v2 项目；loader 在字段存在时严格校验，不存在时继续按传统 SRT 项目读取。
- `project.json` 仍以 `source/source.srt` 为实际下游 source，不改变当前字幕/TTS接口。
- 原子创建失败必须回滚本次唯一新项目目录；draft 准备包保留，方便修复后重试。

## 5. 配音 + 字幕联合预审合同

### 5.1 新增联合预审产物

Edge 完整旁白生成成功后，在请求用户批准前同步生成：

```text
previews/narration-review.mp4
```

该文件固定为轻量诊断视频：

- 1920×1080；
- 暖米黄纯色/极简纸张背景；
- 使用 current `audio/narration.wav`；
- 使用 current `audio/narration.srt` 经现有 ASS 编译器和固定 `subtitle-style-v1` 烧录；
- H.264 + AAC，允许使用低成本/快速编码预设；
- 总时长必须服从 current audio timeline；
- 必须有 1 路视频和 1 路音频；
- 必须通过 `ffprobe` 和完整 null-sink 解码；
- 它不是 `output/final.mp4`，不得进入最终交付三层输出。

优先复用现有字幕编译、字体 discovery、FFmpeg 调用、媒体验证和原子发布工具，避免再实现一套字幕转义或字体规则。

建议新增：

```text
scripts/narration_review.py
```

也可将纯函数放入 `subtitle_delivery.py` 或现有媒体模块，但 CLI/编排应由 `generate_voiceover.py full` 在完整音频、timeline、SRT 均 current 后显式调用。

### 5.2 联合预审 identity

定义：

```text
NARRATION_REVIEW_IDENTITY = sha256(canonical JSON {
  fullIdentitySha256,
  narrationWavSha256,
  timelineSha256,
  narrationSrtSha256,
  reviewAssSha256,
  reviewVideoSha256,
  reviewProfileVersion,
  fontIdentity,
  subtitleStyleIdentity
})
```

voice manifest 新增 current review artifact/binding，不保存绝对路径或秘密。任何以下变化都使 review stale：

- narration WAV、timeline、narration SRT；
- review ASS、字体 identity、字幕样式；
- review profile/version；
- review MP4 文件 SHA。

预审视频编码变化只使 review artifact/批准证据 stale，不应反向使已经验证的 canonical WAV、timeline 或 narration SRT stale；内容源变化仍按现有规则使音频和全部下游 stale。

### 5.3 生成与验证命令输出

`generate_voiceover.py full` 成功后增加输出：

```text
FULL_IDENTITY=<...>
NARRATION_REVIEW=<project-relative-path>
NARRATION_REVIEW_IDENTITY=<...>
```

`validate_voiceover.py` 增加只读验证：

- review artifact 存在且 SHA 匹配；
- review 绑定 current FULL_IDENTITY；
- SRT/ASS/字体/style identity current；
- 媒体流、codec、尺寸、时长、完整解码通过；
- 报告 target/source provisional duration、真实音频 duration、偏差与是否超过 10%。

技术验证只能输出 current identity，不得写人工批准。

### 5.4 用户联合确认关卡

用户必须完整播放 `previews/narration-review.mp4`，一次观看中完成三项明确判断：

1. 配音内容、音色、语速和完整听感是否接受；
2. 字幕文字、切换时机、两行换行和可读性是否接受；
3. 是否接受真实音频总时长；偏差超过 10% 时必须单独给出 `accept_actual`。

用户确认后执行：

```powershell
<ENV_PY> scripts/generate_voiceover.py approve-full --project <项目根目录> `
  --identity-hash <FULL_IDENTITY> `
  --review-identity-hash <NARRATION_REVIEW_IDENTITY>
```

超过 10% 且用户明确接受时：

```powershell
<ENV_PY> scripts/generate_voiceover.py approve-full --project <项目根目录> `
  --identity-hash <FULL_IDENTITY> `
  --review-identity-hash <NARRATION_REVIEW_IDENTITY> `
  --duration-decision accept_actual
```

批准记录必须同时绑定 current FULL_IDENTITY 和 current NARRATION_REVIEW_IDENTITY。任一不匹配、缺失、stale 或未验证时以退出码 5 拒绝。

### 5.5 用户反馈后的最小重做边界

- 音色/语速不满意：回到样音；样音和完整旁白批准失效，按现有 voice/rate stale 规则处理。
- 朗读文字错误：修改 content draft，重新确认内容，重新派生 source SRT/plan；受影响 TTS units 和全部下游按 identity 重做。
- narration SRT 与实际音频文本不一致：视为技术失败，不允许只改字幕掩盖音频错误。
- 仅字幕换行/显示样式不满意：允许修正确定性字幕编译规则，重新生成 review ASS/MP4；canonical WAV 可复用，但 review 批准重做。
- 实际时长不接受：修改文本或 voice/rate 后重做；不得在后面用 `-shortest` 截断。

## 6. 更新后的权威工作流

### 阶段 0：输入、旁白稿、cue 和分镜确认（新增）

1. 接受 `srt | topic | text`。
2. SRT 直接进入原阶段 1。
3. topic/text 生成完整 `content-draft-v1`，向用户展示：
   - 原始主题/正文；
   - rewritePolicy；
   - 完整旁白稿；
   - target duration；
   - cue 与 scene 映射；
   - 每幕核心表达、画面主体和图片提示词；
   - 对原文的实质改动说明。
4. 停止，等待用户明确确认内容草案；确认前不得运行 `prepare_source.py`，不得创建正式项目。
5. 确认后生成并验证 draft 准备包。

### 阶段 1：严格 SRT、模式和语义分镜确认（扩展）

- 对生成的 source SRT 使用与传统 SRT 完全相同的严格 parser。
- 展示 provisional 总时长、cue、scene 和 generation plan。
- 向用户说明：当前为 Edge provisional source timeline，最终由获批真实音频 timeline 接管。
- 再次等待策略确认，随后才创建正式项目。

### 阶段 2–3：沿用当前创建项目与样音批准

- topic/text 只允许 edge-tts。
- 样音仍须独立完整试听并确认 voice/rate，不能被“已确认旁白稿”替代。

### 阶段 4：完整旁白 + 字幕 + 联合预审批准（替换当前阶段 4）

1. 生成并验证 canonical speech segments/WAV。
2. 生成 current timeline 与 narration SRT。
3. 立即生成 review ASS 和 `narration-review.mp4`。
4. `validate_voiceover.py` 完成技术验证。
5. 用户完整播放 review MP4，同时确认配音、字幕和真实时长。
6. `approve-full` 绑定 full identity 与 review identity。
7. 成功后原子更新 timing plan，使 audio timeline 成为正式时钟。

### 阶段 5–8：沿用线稿、标注、逐幕渲染、clean master 确认

- 联合预审已经通过不代表线稿、annotation、单幕或 clean master 自动获批。
- 旁白/字幕 identity stale 时，annotation 和场景媒体必须按现有规则失效。

### 阶段 9：最终字幕烧录仍然保留

- 使用 current approved `audio/narration.srt` 编译正式 ASS。
- 烧录到 current approved `final-video-only.mp4`。
- 生成最终字幕 contact sheet，检查真实画面上的位置、描边、对比度、两行换行和安全边距。
- 若正式 ASS 的文本、时序、字体或样式与联合预审批准内容不兼容，必须失败；不能静默换稿。

### 阶段 10：最终封装与批准保持不变

- Edge 继续以 `-c:v copy` 封装 approved canonical audio。
- 完整技术验证后用户仍要完整看片听音。
- 最终批准只绑定 current final identity；联合预审批准不能代替最终媒体批准。

## 7. stale、恢复和失败语义

在现有规则上增加：

- topic/body/rewritePolicy/target/narration cues/scene mapping 改变：content draft、source SRT、generation/timing plan、voice plan/segments/audio/timeline/narration SRT、review、annotation、场景视频、字幕、final 与最终批准全部重新判定。
- 只有 imagePrompt 改变且 narration cues/scene boundary 不变：音频与 review 可复用；generation plan/images 及视觉下游 stale。
- provisional SRT 排时算法版本改变：source identity/timing 重新判定；已经 approved 且 synthesis identity 未变的 Edge 音频 segments 可按现有规则复用，但 duration decision、timing plan、review 与下游重新验证。
- review style/font/profile 改变：review artifact 与 review approval stale；canonical audio/timeline/SRT 保留；正式字幕是否 stale 按正式 `subtitle-style-v1` identity 判断。
- review 生成中断：保留 current 已验证 audio/timeline/SRT；可只重试 review，不重复请求 Edge。
- 完整音频只有部分 units 失败：继续沿用 `--retry-failed`，不得覆盖 validated/current units。
- 所有候选和 manifest 通过 `.work/<run-id>` 原子发布；失败候选不能覆盖 current。

退出码继续沿用：

| 码 | 本功能含义 |
|---:|---|
| 0 | 准备/生成/验证成功 |
| 1 | 批处理局部失败或独立环境操作失败 |
| 2 | content draft、参数、SRT、plan、manifest 或绑定无效 |
| 3 | Edge 外部请求失败或重试耗尽 |
| 4 | FFmpeg、ffprobe、字体、ASS、WAV 或 review 媒体验证失败 |
| 5 | stale、identity 不匹配、review 未验证或缺少人工批准 |

## 8. 代码与文档落点

### 8.1 主要新增文件

```text
scripts/content_source.py
scripts/prepare_source.py
scripts/narration_review.py
tests/test_content_source.py
tests/test_prepare_source_cli.py
tests/test_narration_review.py
examples/topic-habit-loop-content-draft.json
examples/text-habit-loop-content-draft.json
references/content-input.md
```

### 8.2 主要修改文件

```text
SKILL.md
README.md
scripts/create_project.py
scripts/project_workspace.py
scripts/generate_voiceover.py
scripts/validate_voiceover.py
scripts/voiceover.py
scripts/subtitle_delivery.py          # 仅在需要提取可复用 ASS 编译/identity 时
scripts/media_validation.py           # 仅增加通用 review 验证 helper 时
references/voiceover.md
references/subtitles.md
tests/test_project_workspace.py
tests/test_voiceover.py
tests/test_voiceover_cli.py
tests/test_voiceover_timing.py
tests/test_subtitles.py
tests/test_edge_delivery_e2e.py
```

避免让多个并行代理同时编辑 `SKILL.md`、`README.md`、`generate_voiceover.py` 或同一测试文件。总窗口必须先分配文件所有权。

## 9. 自动测试与验收

### 9.1 内容准备单元测试

- topic/text 合法组合通过，非法 rewritePolicy 组合拒绝。
- topic/body 分开保存，NFKC/newline 规范化确定性。
- 空输入、超长 topic/body、无效时长、重复 cue/scene、跨 scene 回跳拒绝。
- 相同 draft 产生完全相同的 canonical hash、SRT、plan 和 manifest。
- provisional SRT 从 0 开始、无重叠、最后 endMs 精确等于 target。
- 极短 cue、多 cue、全标点、中文/英文/数字混合均有稳定行为。
- SRT serialize 后由共享 parser round-trip 一致。
- generation plan 满足现有 `validate_generation_plan_data`。
- manifest 或任一文件被篡改时，create_project 拒绝。
- JSON/manifest 不含秘密键和绝对路径。

### 9.2 兼容测试

- 现有 `create_project.py --srt --plan` 用法不变。
- 传统 SRT 的 disabled/edge 项目照常创建、续接和加载。
- 旧 v1/v2 项目没有 `contentSource` 时不被静默改写。
- topic/text 项目缺任一 source evidence 或 hash stale 时失败。
- source input 改变后不能复用旧项目 current identity。

### 9.3 联合预审测试

- fake provider/fixture 生成 WAV、timeline、narration SRT 后可生成 review MP4。
- review ASS 使用 current narration SRT，不回退 source SRT。
- review 视频为 1920×1080 H.264 + AAC、时长匹配、完整解码。
- review manifest/identity 覆盖 WAV、timeline、SRT、ASS、font/style/profile 和 MP4 SHA。
- 修改 WAV/SRT/ASS/MP4 任一字节会被检测为 stale 或 hash mismatch。
- review 失败后 canonical audio/timeline/SRT 保持可恢复，重试不再次调用 fake/real Edge。
- `approve-full` 缺 review identity、identity 不匹配或 review 未验证时退出 5。
- 通过 current FULL_IDENTITY + REVIEW_IDENTITY 后批准记录正确持久化。
- 超过 10% 偏差仍要求 `accept_actual`，联合确认不能绕过。
- review 通过后 timing plan 使用 approved audio timeline；generation plan 不变。

### 9.4 最终交付回归测试

- 正式 burn 仍使用 current approved narration SRT。
- review 批准不能替代 final contact sheet 或 final media approval。
- Edge final 仍为 H.264 + 24kHz mono AAC，字幕可见，完整解码。
- Disabled 现有 final 行为完全不变。
- 不使用 `-shortest`。
- topic fixture 从 content draft → source package → project → fake Edge → review → approval → render fixture → final 的端到端测试通过。
- 自动测试只使用 fake provider/固定 WAV，不调用真实 Edge 或图片 provider。

### 9.5 建议命令

```powershell
python scripts/prepare_env.py --check
<ENV_PY> -m unittest tests.test_content_source -v
<ENV_PY> -m unittest tests.test_prepare_source_cli -v
<ENV_PY> -m unittest tests.test_narration_review -v
<ENV_PY> -m unittest tests.test_project_workspace -v
<ENV_PY> -m unittest tests.test_voiceover tests.test_voiceover_cli tests.test_voiceover_timing -v
<ENV_PY> -m unittest tests.test_subtitles tests.test_final_media tests.test_edge_delivery_e2e -v
<ENV_PY> -m unittest discover -s tests -v
```

若系统 `ffmpeg`/`ffprobe` 不可用，应明确报 `BLOCKED` 或对应测试 skip/fail，不能把未执行媒体测试写成 PASS。

### 9.6 最小人工验收

1. 主题输入：“为什么人会拖延”，60 秒，生成旁白稿/分镜，用户确认后生成准备包。
2. 正文输入分别验证 `preserve` 与 `polish`，确认原文保真和修改说明。
3. 传统 SRT 创建路径做一次无回归 smoke。
4. 使用真实 Edge 时：
   - 样音完整试听并批准；
   - 完整播放 `narration-review.mp4`；
   - 分别确认配音、字幕、真实时长；
   - 超过 10% 时明确 `accept_actual` 或退回修改。
5. 继续至少一个短 fixture 项目到正式 burn/mux/final validation，确认联合预审没有跳过最终关卡。

真实 Edge、真实图片 provider 和最终人工审美结论必须与 fixture 自动测试分开报告。

## 10. 多代理实施编排（新窗口硬约束）

### 10.1 总窗口角色

新 Codex 窗口只负责：

- 阅读本计划和当前合同；
- 冻结接口、拆任务、分配文件所有权；
- 创建/调度子代理；
- 汇总子代理的改动、测试和阻塞；
- 在代理完成后安排一个子代理做集成修复和全量测试；
- 向用户报告已落地/部分/待验收边界。

**总窗口不得亲自实现业务代码或文档改动。** 所有具体编辑、测试修复和集成修复都交给子代理。总窗口可以做只读检查、更新调度计划和汇总证据。

### 10.2 并发原则

- 当前最多并发槽应尽量使用；总窗口占一个槽时，首波同时启动三个子代理。
- 能并行的任务必须并行，禁止无原因串行等待。
- 当前目录不是 Git 仓库，所有代理共享文件系统；必须按文件所有权拆分，禁止两名代理同时编辑同一文件。
- 子代理完成后立即释放槽位；下一波集成/修复代理及时接替。
- 不创建单独的“过度 review”代理。首版只做一次轻量集成核对和必要测试修复；没有具体失败证据时不开展全面重构或风格 review。

### 10.3 Wave 1：三个并发子代理

#### 子代理 A：内容入口与 source 准备包

文件所有权：

```text
scripts/content_source.py
scripts/prepare_source.py
scripts/create_project.py
scripts/project_workspace.py
tests/test_content_source.py
tests/test_prepare_source_cli.py
tests/test_project_workspace.py
examples/*-content-draft.json
```

任务：实现 `content-draft-v1`、确定性 provisional SRT、generation plan 派生、source package manifest、正式项目可选 provenance 绑定和传统 SRT 回归。

#### 子代理 B：配音字幕联合预审

文件所有权：

```text
scripts/narration_review.py
scripts/generate_voiceover.py
scripts/validate_voiceover.py
scripts/voiceover.py
scripts/subtitle_delivery.py       # 如确需改动
scripts/media_validation.py        # 如确需改动
tests/test_narration_review.py
tests/test_voiceover.py
tests/test_voiceover_cli.py
tests/test_voiceover_timing.py
```

任务：实现 review MP4、review identity/manifest、只读验证、`approve-full` 双 identity 绑定、可恢复重试和 fixture 测试。

#### 子代理 C：Skill/README/reference 合同与用户流程

文件所有权：

```text
SKILL.md
README.md
references/content-input.md
references/voiceover.md
references/subtitles.md
```

任务：把阶段 0、非 SRT 首版限制、联合预审、最终 burn 保留、命令、stale、退出码和人工关卡写入权威文档。不得提前宣称尚未实现或尚未测试的能力已 PASS；应与 A/B 冻结的 CLI 名称保持一致。

### 10.4 Wave 2：集成与端到端子代理

Wave 1 核心接口落地后，由总窗口选择已空闲槽位，派一个子代理负责：

```text
tests/test_subtitles.py
tests/test_final_media.py
tests/test_edge_delivery_e2e.py
必要的跨模块集成修复
```

任务：

- 汇总 A/B 结果；
- 补 topic fixture 的端到端路径；
- 跑相关测试和全量 unittest；
- 只修复真实失败，不做无关重构；
- 明确真实 Edge/真实图片 provider 的 SKIP/BLOCK 边界。

如果集成修复必须修改 A/B/C 所有权文件，总窗口应先确认原子代理已结束，再把具体文件显式转交给集成代理，避免并发覆盖。

### 10.5 完成定义

首版只有同时满足以下条件才能报告“实现完成”：

- topic/text → approved content draft → source package → formal project 路径可运行；
- 传统 SRT 路径无回归；
- fake Edge 完整旁白能同步生成并验证 narration review；
- `approve-full` 必须绑定 current full + review identities；
- timing plan、generation plan、正式字幕与最终媒体合同保持正确；
- 新增/相关/全量自动测试结果有具体命令与数字；
- `SKILL.md`、README、references 与实际 CLI 一致；
- 未执行的真实 provider 和人工关卡明确写为 SKIP/BLOCK/待用户确认，不能冒充 PASS。

## 11. 实施顺序摘要

```text
Wave 1（并发）
  A 内容入口/source package
  B 配音字幕联合预审
  C Skill/README/reference
        ↓
Wave 2
  集成代理：E2E + 全量测试 + 只修真实失败
        ↓
总窗口
  汇总证据、报告首版完成边界，不亲自写业务实现
```

## 12. 后续版本候选（本次不做）

- 非 SRT 输入的 `voiceoverMode=disabled` 确定性阅读节奏与独立时序确认。
- 通用文本模型 provider 和一条命令 topic→draft 自动化。
- 主题事实检索、来源冻结和引用展示。
- 字幕文本/换行的交互式编辑器。
- 多语言、逐词时间戳、卡拉 OK 字幕。
- 在真实最终画面上提前模拟字幕安全区的低分辨率代理预览。

这些候选不得扩大本次首版范围。
