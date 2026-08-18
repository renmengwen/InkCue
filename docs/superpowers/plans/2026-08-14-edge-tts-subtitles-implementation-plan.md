# Edge TTS 旁白与烧录字幕综合实施计划

> 日期：2026-08-14  
> 状态：待实施  
> 适用 Skill：`srt-whiteboard-animation`  
> 运行产物根目录：`D:\SRTWhiteboard`  
> 设计依据：`docs/superpowers/specs/2026-08-14-edge-tts-voiceover-enhancement-design.md`  
> 首版目标：保留静音模式，同时新增可恢复、需人工批准的 Edge TTS 旁白；无论是否启用 TTS，正式 `output/final.mp4` 都必须带可见的烧录字幕。

## 1. 计划目的

当前 Skill 已能从 SRT 生成白板动画，但“README 示例有可见字幕”和“可复现的正式 MP4 没有字幕”之间存在交付缺口；同时现有渲染时钟仍以源 SRT 为主，不能直接承载真实 TTS 音频时长。

本计划一次性补齐两个相互依赖的能力：

1. Edge TTS 旁白：样音、voice/rate 人工批准、分段生成、断点恢复、canonical WAV、真实音频时间轴、完整试听和真实时长批准。
2. 正式字幕：按当前模式选择唯一权威 SRT，确定性编译为 ASS，使用 FFmpeg/libass 烧录，并将字幕输入、样式、字体和最终媒体身份写入 manifest。

实施完成后，“最终成片”不再表示一个只有白板画面的静音文件，而具有如下硬合同：

```text
voiceoverMode=disabled
  → H.264 视频 + 烧录字幕，无音频流

voiceoverMode=edge-tts
  → H.264 视频 + 烧录字幕 + AAC 旁白
```

缺少烧录字幕的 `output/final.mp4` 一律视为验收失败，而不是可接受的降级结果。

## 2. 首版范围与非目标

### 2.1 首版范围

- 显式支持 `voiceoverMode = disabled | edge-tts`。
- Edge TTS 是首版唯一 TTS provider，使用 Python `edge-tts`，依赖外网但不需要 API Key。
- 不调用、不导入、不依赖 `D:\code3\Yingshu` 运行时。
- 样音批准后才能生成完整旁白；完整旁白试听和真实时长批准后才能冻结最终动画时序。
- 完整旁白按朗读单元串行生成，成功单元可复用，失败单元可通过 `--retry-failed` 恢复。
- 所有正式 WAV 统一为 `pcm_s16le / mono / 24000Hz`，并使用 `ffprobe` 验证。
- Disabled 模式使用 `source/source.srt` 烧录字幕。
- Edge TTS 模式只使用 `audio/narration.srt` 烧录字幕。
- 最终媒体通过 `ffprobe`、帧数/时长合同和完整解码验证后原子发布。
- 同步更新 `SKILL.md`、`README.md`、reference、命令、自动测试和人工验收规则。

### 2.2 非目标

- 不接入第二个 TTS provider。
- 不做 voice 自动推荐、自动切换 provider 或失败后的隐式降级。
- 不做逐词卡拉 OK 字幕、软字幕轨、多语言字幕或字幕样式编辑器。
- 首版不增加 `subtitleMode=disabled`：正式 `final.mp4` 必须带烧录字幕；需要无字幕画面时使用明确保留的 `final-video-only.mp4`。
- 不在字幕阶段改写旁白文案；字幕阶段只做时间、换行、样式和安全转义，避免音频与字幕文本漂移。
- 不把 TTS/provider/批准逻辑塞进 `merge_scenes.py`。
- 不重新引入 Chrome、预览台或文件选择框作为字幕/TTS 流程的必要步骤。
- 不把 fake provider 自动测试描述成真实 Edge 服务验收。

## 3. 已冻结的核心决策

### 3.1 两种模式的权威时钟与字幕源

| 模式 | 最终时钟 | 权威字幕输入 | 正式 `final.mp4` |
|---|---|---|---|
| `disabled` | 原始 SRT 全局时间轴 | `source/source.srt` | 烧录字幕、无音频 |
| `edge-tts` | 批准后的 canonical audio timeline | `audio/narration.srt` | 烧录字幕、AAC 旁白 |

规则：

- Edge TTS 模式下，如果 `audio/narration.srt` 缺失、过期、hash 不匹配或未绑定 current timeline，必须失败；不得回退到 `source/source.srt`。
- Disabled 模式不得误用项目中遗留的 `audio/narration.srt`。
- `audio/narration.srt` 的文本和时间都从 canonical speech units/timeline 派生，不从源 SRT 复制旧时间戳。
- annotation 中的 `subtitle` 仍可用于元素语义对应，但不是正式烧录字幕的时间源。

### 3.2 固定的三层视频输出

```text
output/final-video-only.mp4
output/final-subtitled-video-only.mp4
output/final.mp4
```

- `final-video-only.mp4`：多幕合并后的干净静音母版；无音频、无烧录字幕。
- `final-subtitled-video-only.mp4`：以当前权威 SRT 烧录字幕后的静音诊断母版。
- `final.mp4`：正式交付文件，始终有烧录字幕。
  - Disabled：验证后的 `final-subtitled-video-only.mp4` 原子复制/发布，不再次编码。
  - Edge TTS：`final-subtitled-video-only.mp4` 以 `-c:v copy` 和 canonical WAV 封装 AAC 后发布。

现有 `merge_scenes.py` 改为默认产出 `output/final-video-only.mp4`，不再把无字幕文件命名为 `final.mp4`。

### 3.3 字幕先烧录，音频后封装

Edge TTS 模式固定执行：

```text
clean silent video
  → burn subtitles（只处理视频并明确 -an）
  → captioned silent video
  → mux canonical WAV（-c:v copy，仅编码 AAC）
  → final.mp4
```

字幕烧录必然重编码视频。先烧录、后封装可以让烧录阶段不触碰已批准音频，让音频封装阶段不再次编码视频，从而减少时长漂移和媒体身份不清。

### 3.4 不使用 `-shortest`

- 禁止通过 `-shortest` 截断较长的一侧来掩盖时间轴错误。
- 视频使用**累计全局帧边界**，不得对每幕 duration 分别 `ceil` 后再相加。冻结算法为：

  ```text
  globalEndFrame(scene) = ceil(scene.globalEndMs * fps / 1000)
  sceneStartFrame        = 上一幕 globalEndFrame（第一幕为 0）
  sceneEndFrameExclusive = globalEndFrame(scene)
  sceneFrameCount        = sceneEndFrameExclusive - sceneStartFrame
  globalFrameCount       = 最后一幕 sceneEndFrameExclusive
  ```

- `timeline.json` 和 source timeline 都必须持久化或确定性导出每幕的 `startFrame`、`endFrameExclusive`、`frameCount`；逐幕帧数之和必须天然等于全局目标帧数。
- 禁止使用 `sum(ceil(sceneDurationMs * fps / 1000))`，因为它会在多幕非整帧边界下累计多余帧。
- 音视频差值只有在 `max(1 个视频帧, 80ms)` 以内才允许确定性补零。
- 超出容差必须失败，回到 timeline、annotation 或场景帧数修复。

### 3.5 人工批准是持久化状态，不是聊天备注

样音、完整旁白/真实时长、最终成片的批准都必须绑定 current identity hash，并写入对应 manifest。CLI 只在主代理已经收到用户明确确认后执行，不得自行批准。

## 4. 统一工作流与人工确认关卡

### 阶段 1：SRT、模式和语义分镜确认

1. 读取并严格验证 SRT。
2. 显式选择 `voiceoverMode`。
3. 输出语义分镜策略、字幕来源和预期总时长。
4. 等待用户确认。

### 阶段 2：创建/升级项目

- 新项目写 schema v2，并冻结 `voiceoverMode`。
- v1 老项目默认按 Disabled 兼容读取，不在 loader 中静默改写。
- 老项目启用 Edge TTS 时执行显式、原子、可验证的 v1→v2 升级。

创建项目时同时写入独立的 `planning/timing-plan.json`。Disabled 模式完成后直接进入阶段 5；Edge TTS 模式继续阶段 3。

### 阶段 3：Edge TTS 样音和 voice/rate 批准

1. 从已确认文本选择代表性中文自然句。
2. 生成 `previews/voice-sample.wav`。
3. 通过 canonical WAV/ffprobe 技术校验。
4. 向用户播放样音，等待 voice/rate 明确确认。
5. 使用 current sample identity 写入批准状态。

未批准时，完整旁白命令必须以退出码 5 拒绝执行。

### 阶段 4：完整旁白、真实时间轴和完整试听批准

1. 按确定性 speech units 串行生成分段音频。
2. 每段立即规范化、验证、hash、原子发布并更新 checkpoint。
3. 合并为 `audio/narration.wav`。
4. 生成 `audio/timeline.json` 和 `audio/narration.srt`。
5. 输出源 SRT 时长、真实音频时长、差值和比例。
6. 用户完整试听，而不是只抽听首尾。
7. 用户批准完整旁白和真实时长。
8. 批准成功后原子更新 `planning/timing-plan.json`，不改写图片 generation plan。

真实时长相对源 SRT 偏差超过 10% 时，必须显式执行 `accept_actual`，或修改 rate/文本后重做；不得自动接受。

### 阶段 5：生成线稿并确认

- 生图提示词继续包含画布尺寸。
- 生成完成后使用本地图片查看能力检查，不启动 Chrome，不拉起文件选择框。
- 等待用户确认线稿。

### 阶段 6：标注、区域预览和最终时序确认

1. 创建 annotation。
2. Edge TTS 模式的 annotation 必须绑定 current audio/timeline SHA 和 scene 时间范围。
3. 生成区域预览图并等待确认。
4. 固化最终标注与时序并等待确认。

### 阶段 7：逐幕渲染和单幕确认

1. 按当前模式的权威时钟渲染每幕。
2. 验证每幕目标帧数、codec、尺寸、fps 和完整解码。
3. 逐幕提供成片检查，并等待用户确认；未确认的幕不得进入合并。

### 阶段 8：静音画面母版合并确认

1. 将全部已确认单幕合并为 `output/final-video-only.mp4`。
2. 验证总帧数恰好等于权威全局时间轴目标帧数。
3. 等待用户确认干净画面母版。

单幕项目可以跳过 concat 动作，但仍须生成并确认同名 `final-video-only.mp4`，保证最终交付输入一致。

### 阶段 9：字幕烧录

1. 按模式严格选择唯一权威 SRT。
2. 编译固定样式 ASS。
3. 烧录为 `output/final-subtitled-video-only.mp4`。
4. 生成 `previews/final-subtitle-contact-sheet.png`，至少包含首条、中间、末条字幕中段；仅当权威 SRT 确实存在空档时再包含一个无字幕空档中点。若不存在空档，manifest 记录 `gapEvidence: not_applicable_no_gap`，不得伪造空档帧。
5. 通过本地图片查看检查位置、换行、描边和安全边距。

该阶段不需要浏览器、预览台或文件选择框。

### 阶段 10：音频封装、最终验证和最终确认

- Disabled：验证 captioned video 后原子发布 `output/final.mp4`。
- Edge TTS：将 canonical WAV 编码为 AAC，并以 `-c:v copy` 与 captioned video 封装。
- 验证流、codec、尺寸、像素格式、fps、采样率、声道、帧数、时长、hash 和完整解码。
- 用户最终完整看片听音并确认；确认后必须通过独立的 `approve_final_media.py` 把 current final identity 写入 delivery manifest。技术验证脚本不得自动写入人工批准。

## 5. 项目 schema 与目录合同

### 5.1 schema v1/v2 兼容策略

当前 loader 接受 schema v1，并校验既有必需路径。直接把新字段变成 v1 必需字段会破坏已有项目，因此实施时采用：

- 新项目：写 `schemaVersion: 2`。
- loader：同时验证 v1 和 v2，不用内存默认值伪装持久化升级。
- v1：按现有扁平目录合同运行，读取时解释为 `voiceoverMode=disabled` 和固定兼容渲染档 `1920×1080/60fps`，但不改写文件。
- v1 启用 Edge：显式执行 `scripts/upgrade_project.py`，验证成功后用 `os.replace` 原子替换 `project.json`。
- 升级保留 `projectId`、source hash、generation plan、图片、annotation 和现有媒体；失败不留下半升级文件。

升级写入顺序固定为：先在项目 `.work/upgrade-<runId>/` 完整生成并验证 v2 `project.json` 与 timing plan 候选，再把 timing plan 原子发布到最终路径，最后以 `project.json` 作为提交点执行 `os.replace`。提交点之前项目仍按 v1 有效读取；若 timing plan 已发布但 project 提交失败，v1 loader 忽略该非 current 文件，重试时按 identity 覆盖，避免出现半升级不可读取状态。

建议 v2 核心字段：

```json
{
  "schemaVersion": 2,
  "projectId": "...",
  "voiceoverMode": "disabled",
  "renderProfile": {
    "contractVersion": "whiteboard-render-v2",
    "width": 1920,
    "height": 1080,
    "fps": 60,
    "pixelFormat": "yuv420p",
    "videoCodec": "h264",
    "frameRounding": "cumulative-ceil-v1"
  },
  "paths": {
    "planning": "planning",
    "scenes": "scenes",
    "previews": "previews",
    "manifests": "manifests",
    "output": "output",
    "work": ".work",
    "audio": "audio",
    "subtitles": "subtitles"
  }
}
```

`voiceoverMode` 只允许 `disabled` 或 `edge-tts`。`renderProfile` 是正式渲染、帧边界、annotation stale 和 delivery identity 的权威输入；正式渲染不得用未持久化的 `--fps` 或尺寸参数覆盖它。JSON 中继续只保存项目相对 POSIX 路径，不保存盘符绝对路径、`..`、秘密、Cookie、Token、临时 URL 或 PID。

### 5.2 项目目录

```text
<project>/
  project.json
  source/
    source.srt
  planning/
    generation-plan.json
    timing-plan.json
    voice-plan.json
  scenes/
    scene-01-<名称>.png
    scene-01-<名称>.annotation.json
    scene-01-<名称>-whiteboard.mp4
  audio/
    segments/
      unit-0001.wav
    narration.wav
    timeline.json
    narration.srt
  subtitles/
    final.ass
  previews/
    voice-sample.wav
    final-subtitle-contact-sheet.png
  manifests/
    generation-manifest.json
    voice-manifest.json
    delivery-manifest.json
  output/
    final-video-only.mp4
    final-subtitled-video-only.mp4
    final.mp4
  .work/
```

场景目录继续沿用当前**扁平同名合同**，不在本次 TTS/字幕首版中迁移为每幕子目录；`foo.png` 对应 `foo.annotation.json`，以兼容现有 generation plan、生图验证、静态预览和可选预览台。`subtitles/final.ass` 是根据当前唯一权威 SRT 和 `subtitle-style-v1` 确定性编译的可审计产物，不是第二份字幕时间轴。Disabled 模式不要求安装 `edge-tts`，`audio/` 可按需为空；字幕烧录和 FFmpeg/字体 preflight 仍是正式交付所必需。

`generation-plan.json` 继续只负责图片语义、提示词、输出文件和图片 generation manifest identity；不得把批准后的 TTS 实际时长回写进去，否则 voice/rate 变化会无故使已确认图片的 generation plan hash 失效。所有 source/audio 权威时钟、scene 帧边界和 active timing identity 统一进入 `planning/timing-plan.json`。

## 6. 数据与身份合同

### 6.0 Timing plan

`planning/timing-plan.json` 是渲染时钟的唯一项目级快照，与图片 `generation-plan.json` 分离。首版 schema 固定为：

```json
{
  "schemaVersion": 1,
  "projectId": "...",
  "voiceoverMode": "disabled",
  "sourceSrtSha256": "...",
  "renderProfileSha256": "...",
  "activeTimeline": {
    "kind": "source-srt",
    "file": "source/source.srt",
    "sha256": "..."
  },
  "scenes": [
    {
      "sceneId": "scene-01",
      "sourceCueRange": [1, 10],
      "startMs": 0,
      "endMs": 28760,
      "sceneDurationMs": 28760,
      "startFrame": 0,
      "endFrameExclusive": 1726,
      "frameCount": 1726
    }
  ]
}
```

规则：

- Disabled 在创建项目时由 source SRT 和已确认语义 scene 边界生成 current timing plan。
- Edge 在完整旁白、真实时长获得批准后，根据 current `audio/timeline.json` 原子重写 timing plan 的 active timeline 与 scenes；不得修改图片 generation plan。
- `startMs/endMs` 是连续的全局时间，帧字段严格使用 3.4 的累计边界算法。
- timing plan hash 进入 annotation、场景 MP4、clean video 和 delivery identity；render profile、mode 或 active timeline 变化时 timing plan 必须重建并使下游 stale。
- v1 Disabled 老项目允许从 source SRT + generation plan 语义边界确定性构造兼容视图而不改写文件；显式升级到 v2 时才持久化 timing plan。

### 6.1 Voice plan

`planning/voice-plan.json` 至少冻结：

- schemaVersion、projectId、mode。
- provider ID、protocol、contractVersion。
- voice、language、rate、pitch、volume、output format。
- source SRT 相对路径和 SHA-256。
- speech-unit 分段合同。
- `timingPolicy.mode = audio-authoritative`。
- `durationReviewThresholdRatio = 0.10`。

全局 provider 配置只作为新 plan 默认值；配置变化不能自动覆盖已批准 voice plan。

`rate` 的 CLI 输入冻结为整数百分点，例如 `0`、`10`、`-10`；进入 provider 前规范化为 Edge 形式 `+0%`、`+10%`、`-10%`，进入 identity 的是规范化字符串。pitch/volume 采用同样的显式单位和规范化规则，不保存依赖调用点猜测的裸值。

### 6.2 Voice manifest

`manifests/voice-manifest.json` 只负责配音生产和批准，至少包含：

- voice plan file/`voicePlanAuditHash`、source SRT file/hash。
- sample identity、媒体字段、批准状态。
- runs 和每个 segment 的状态、`voiceSynthesisIdentityHash`、尝试次数、错误阶段、脱敏摘要。
- composite WAV 身份。
- timeline 和 narration SRT 身份。
- full audio approval identity、真实时长决定。

segment 状态固定为：

```text
pending | requesting | normalizing | validated | failed | cancelled
```

每个正式 segment 保存 codec、sampleRate、channels、bytes、durationMs、SHA-256，但不保存完整服务响应。

### 6.3 Canonical audio timeline

`audio/timeline.json` 必须满足：

- unit 0 从 0 开始。
- unit 连续、无空洞、无重叠。
- 最后一个 unit 收口到整轨 `ffprobe` 实测 duration。
- scene 继续保留已批准的语义 cue 边界。
- scene 连续覆盖全部 units，最后一幕收口到整轨时长。
- scene 同时记录全局 `startMs/endMs` 与按 3.4 累计算法计算的 `startFrame/endFrameExclusive/frameCount`。
- 包含 source SRT SHA、`voicePlanAuditHash`、audio SHA。
- timeline SHA 成为 annotation、narration SRT、字幕和最终媒体身份的一部分。

Speech unit 必须在已批准的 scene 边界强制断开，禁止一个音频 segment 横跨两个 scene，否则无法稳定计算逐幕音频时长和帧边界。

### 6.4 Annotation timingSource

Edge TTS 模式 annotation 新增：

```json
{
  "timingSource": {
    "kind": "edge-tts-audio-timeline",
    "timelineFile": "audio/timeline.json",
    "timelineSha256": "...",
    "audioSha256": "...",
    "sceneId": "scene-01",
    "sceneStartMs": 0,
    "sceneEndMs": 28760
  }
}
```

Disabled 模式使用 source timeline identity。annotation 还必须绑定 current timing plan SHA 和 render profile SHA。渲染前必须拒绝 stale、scene ID 不匹配、sceneDuration/frameCount 不匹配或元素越界的 annotation。

时间坐标合同必须固定为：

```text
timeline.scenes[*].startMs/endMs             全局时间
timingSource.sceneStartMs/sceneEndMs         全局时间
annotation.sceneDurationMs                   场景局部总时长
elements[*].reveal.startMs/durationMs        场景局部时间，从本幕 0 开始
narration.srt cue start/end                   全局时间
```

校验时使用 `sceneDurationMs = sceneEndMs - sceneStartMs`，并要求 `0 <= element.startMs`、`element.endMs <= sceneDurationMs - 500`。第二幕及以后不得把全局 `sceneStartMs` 直接写入元素的局部 `startMs`。

### 6.5 Delivery manifest

新增 `manifests/delivery-manifest.json`，同时服务 Disabled 和 Edge 两种模式，至少记录：

- `final-video-only.mp4` 的相对路径、SHA、bytes、duration、frameCount、fps。
- current timing plan 的相对路径、SHA、mode、active timeline identity 和累计帧边界合同。
- 字幕 `sourceKind`、相对路径、SHA、cue 数、首尾时间。
- `subtitle-style-v1` 合同 SHA、字体文件 SHA、compiled ASS SHA。
- `subtitles/final.ass` 的相对路径和 SHA；其内容必须能由权威 SRT、样式合同和字体 identity 重新生成。
- `final-subtitled-video-only.mp4` 的 SHA 和验证状态。
- Edge 模式下 audio/timeline/narration SRT/voice plan/full approval identity 和 AAC 参数。
- `final.mp4` 的 SHA、流合同和验证状态。
- 最终人工批准 identity。

最终 identity 至少覆盖：

```text
clean video SHA
+ audio SHA（Disabled 为空）
+ timeline SHA
+ authoritative subtitle SHA
+ subtitle style contract SHA
+ font SHA
+ render profile SHA
+ timing plan SHA
+ burn/mux contract version
```

其中 `timeline SHA` 表示当前模式的权威时间轴 identity：Disabled 为 source timeline SHA，Edge 为 canonical audio timeline SHA；不得在 Disabled 中写入空值后绕过时间轴 stale 检查。

## 7. SRT 与全局时间轴修复

当前按“本幕最后 cue.end - 第一 cue.start”计算场景时长会丢失第一条字幕前的前导空白和跨幕字幕间空档。Disabled 模式直接烧录原始 SRT 时，这会导致后半段字幕逐幕漂移。

实施时必须修复为：

- Disabled 的画面从全局 0 开始，最终目标时长为源 SRT 最大 `endMs`。
- scene 区间连续覆盖 `[0, sourceLastEndMs]`。
- 跨幕 cue 空档确定性归入前一幕完整画面停留，或显式记录在 generation plan；不得从总时长中消失。
- 不改写 `source/source.srt` 的原始时间戳。
- 烧录前验证源 SRT 最后 cue 结束时间与 clean video 时长在一帧容差内。
- Edge TTS 的 `audio/narration.srt` 从 0 连续派生，以真实 audio timeline 为准。
- 首版 Disabled 不在最后 cue 之后隐式追加项目级尾时长；元素必须在本幕结束前至少 0.5 秒完成，完整画面停留包含在 `[0, sourceLastEndMs]` 内。未来若需要额外尾时长，必须新增持久化配置和 identity，不能靠渲染器自行延长。

测试必须覆盖首 cue 非 0、跨幕 2 秒空档、末幕元素预留 0.5 秒完整画面、非整帧场景边界和多幕累计帧数，而不能只用“从 0 开始且 cue 连续”的 60 秒样例。

## 8. Edge TTS 生成与恢复合同

### 8.1 Speech unit planner

- 复用一套 SRT parser，不复制两套解析口径。
- 保留稳定的 `sourceOrdinal`，并在输入确有合法原始编号时同时保留 `originalIndex`；不得因 parser 重新编号而破坏 cue、speech unit、scene 和字幕 identity 的映射。
- 断句优先级：`。！？!?；;……`，次级为 `，,、：:`。
- 合并过短相邻 cue，拆分超长句，纯标点并入相邻单元。
- 字符长度按 Unicode code point 计，不按 UTF-16 单元。
- 每个 unit 保存 index、sourceCueRange、sourceOrdinal 范围、原始文本、朗读文本、source text hash、speech text hash。
- cue range 必须连续、无遗漏、无重复；相同输入必须生成相同 unit 和 hash。
- unit 必须在已确认 scene 边界断开，不能跨 scene 合并。

为避免“源 SRT 只改时间也迫使相同音频重新请求”，身份拆分为：

```text
sourceTextIdentityHash
sourceTimingIdentityHash
voiceSynthesisIdentityHash
```

- `voiceSynthesisIdentityHash` 只覆盖规范化朗读文本、稳定 ordinal/range、voice、rate、language、分段合同和 provider 合同。
- 原始 source SRT SHA 继续记录用于审计。
- 纯时间变化不重新请求相同 Edge 语音段，但必须重算源/真实时长偏差；Disabled 字幕时间轴和受影响的画面产物 stale。
- 文本、voice、rate、scene/分段边界或 provider 合同变化才使相应音频 segment stale。

### 8.2 Provider adapter

- Python `edge-tts`，默认串行请求。
- 请求超时、队列间隔、最大重试在短/长样本校准后冻结版本和数值，不在计划阶段猜版本。
- 仅 DNS、连接、timeout、429、502、503、504 做有限重试。
- voice 无效、配置错误、协议错误和用户取消不自动重试。
- 不自动更换 voice/rate/provider。
- 临时原始媒体只写本次 `.work/voice-generate-<runId>/`。

C1/C2 并行前冻结最小 adapter protocol：

```text
SynthesisRequest
  text, voice, normalizedRate, normalizedPitch, normalizedVolume,
  providerContractVersion, timeoutSeconds, cancellationToken

RawAudioResult
  bytes, declaredFormat, providerRequestId?（只存脱敏值）

EdgeTtsAdapter.synthesize(request) -> RawAudioResult

异常：RetryableProviderError | PermanentProviderError | CancelledError
```

adapter 不写项目正式文件、不更新 manifest、不做批准；它只返回原始媒体或分类异常。`voiceover.py` 负责队列、checkpoint 和编排，`audio_normalization.py` 负责 FFmpeg 规范化/ffprobe/原子段发布。fake adapter 使用完全相同的 protocol。

### 8.3 Canonical WAV

```text
Edge 临时输出
  → FFmpeg 规范化为 WAV pcm_s16le/mono/24000Hz
  → ffprobe
  → SHA-256/bytes/duration
  → 原子发布到 audio/segments/
```

严格验证：普通文件、非空、RIFF/WAVE magic、恰好 1 个音频流/0 视频流、codec/sample rate/channels 正确、duration > 0、ffprobe size 与磁盘 bytes 一致。

### 8.4 断点恢复

- 严格按 unit index 串行。
- 每段落盘后立即规范化、验证、hash、原子发布、更新 manifest。
- `--retry-failed` 只处理 failed 和未完成 unit。
- current validated unit 不重请求、不覆盖。
- 正式文件 hash 与 manifest 不一致时失败，不假装恢复。
- `voicePlanAuditHash` 变化后样音/完整音频批准、偏差决定、timeline、narration SRT 和下游全部重新判定 current/stale；但旧 segment 是否复用只由该 segment 的 `voiceSynthesisIdentityHash`、正式 WAV SHA 和媒体合同决定。
- 只有 `voiceSynthesisIdentityHash` 变化的 segment 才重新请求；纯 source timing 变化不得强制重请求合成身份不变的 validated segment。scene/分段边界变化属于 synthesis identity 变化，不跨边界复用整段。
- 合并失败保留全部 validated segments。
- 只清理/恢复本次 run，不扫描或删除其他 `.work` 目录。

## 9. 字幕样式与安全合同

### 9.1 `subtitle-style-v1`

README 示例可确认的是“底部居中白色中文无衬线字、黑色描边、无明显底框”。精确字号和描边宽度未由上游提交，因此首版按短样例视觉校准后冻结。计划默认起始值为：

```text
画布 / PlayRes：1920×1080
字体：Microsoft YaHei
字号：48
粗体：否
主色：白色
描边：黑色 3px
阴影：0
底框：无
对齐：底部居中（ASS Alignment=2）
左右安全边距：96px
底部边距：54px
最大行数：2
最大文本宽度：1728px
```

对应 ASS 核心字段：

```text
PrimaryColour=&H00FFFFFF
OutlineColour=&H00000000
BorderStyle=1
Outline=3
Shadow=0
Alignment=2
MarginL=96
MarginR=96
MarginV=54
```

### 9.2 换行与转义

- 使用实际微软雅黑字体度量做确定性换行。
- 优先在中文标点和空格处断行。
- 最多两行，每行不得超过 1728px。
- 放不进两行时失败并提示拆分 cue，不随机缩字号。
- 转义 `{`、`}`、反斜杠、换行和 ASS override 内容，防止字幕文本注入控制指令。
- 相同 SRT、样式和字体必须生成相同 ASS/hash。
- 最终 MP4 不保留独立字幕轨，因为字幕已经成为视频像素。

### 9.3 字体与 libass preflight

当前环境已确认存在 `C:\Windows\Fonts\msyh.ttc`，FFmpeg 已启用 libass/freetype/fontconfig；实现仍须每次运行前检查：

- `ffmpeg`、`ffprobe` 可执行。
- `ass` filter 和 libass 可用。
- 指定字体存在且 SHA 可计算。
- 缺失时明确失败，不静默换成不可复现字体。

manifest 中只记录字体 family、文件名和 hash，不保存 Windows 字体绝对路径。

## 10. Windows 路径与 FFmpeg 发布边界

不得把包含盘符、空格、中文或单引号的项目绝对路径直接拼进 `-vf subtitles=...`。每次烧录创建 ASCII 工作目录：

```text
.work/subtitle-<runId>/
  burn.ass
  fonts/
    msyh.ttc
  captioned.tmp.mp4
```

执行规则：

- `subprocess.run(argv, shell=False, cwd=runDir)`。
- 输入/输出绝对路径作为独立 argv 参数。
- filter 固定使用相对路径 `ass=burn.ass:fontsdir=fonts`。
- 不构造 PowerShell/cmd 命令字符串。
- 不用 `-r` 强制改帧率，输入视频必须先通过项目 fps 校验。
- `burn.ass` 与候选通过验证后，将完全相同的 ASS 内容原子发布为 `subtitles/final.ass`；失败时不得覆盖旧的 current ASS。

字幕烧录参数合同：

```text
ffmpeg -y -loglevel error
  -i <absolute final-video-only.mp4>
  -vf ass=burn.ass:fontsdir=fonts
  -map 0:v:0 -an
  -c:v libx264 -preset medium -crf 18
  -pix_fmt yuv420p -fps_mode passthrough
  -movflags +faststart
  <same-volume candidate>
```

Edge 音频封装参数合同：

```text
ffmpeg -y -loglevel error
  -i <final-subtitled-video-only.mp4>
  -i <audio/narration.wav>
  -map 0:v:0 -map 1:a:0
  -c:v copy
  -c:a aac -b:a 192k -ar 24000 -ac 1
  -movflags +faststart
  <same-volume final candidate>
```

发布顺序：

1. 只写本次 `.work/<runId>` 或 output 同盘候选文件。
2. 候选通过 `ffprobe` 和完整 null-sink 解码。
3. 使用 `os.replace` 原子替换正式文件。
4. 失败时保留原有正式输出，并报告失败工作目录。
5. 成功后仅清理本次临时 ASS、字体副本和 mux 候选。
6. `final-subtitled-video-only.mp4` 长期保留为诊断产物。

## 11. CLI 合同

### 11.1 环境

```powershell
<ENV_PY> scripts/prepare_env.py --check
<ENV_PY> scripts/prepare_env.py --feature edge-tts
<ENV_PY> scripts/prepare_env.py --check --feature edge-tts
```

- 基础/字幕模式不因未安装 `edge-tts` 失败。
- `--feature edge-tts` 才安装并检查校准后冻结的版本。
- 正式字幕交付始终要求 FFmpeg/ffprobe/libass/字体 preflight 通过。

### 11.2 项目

```powershell
<ENV_PY> scripts/create_project.py ... --voiceover-mode disabled
<ENV_PY> scripts/create_project.py ... --voiceover-mode edge-tts

<ENV_PY> scripts/upgrade_project.py `
  --project <项目根目录> `
  --to-schema 2 `
  --voiceover-mode edge-tts
```

### 11.3 Voiceover

```powershell
<ENV_PY> scripts/generate_voiceover.py sample `
  --project <项目根目录> `
  --voice zh-CN-YunjianNeural `
  --rate 0

<ENV_PY> scripts/generate_voiceover.py approve-sample `
  --project <项目根目录> `
  --identity-hash <刚完整试听的样音身份>

<ENV_PY> scripts/generate_voiceover.py full --project <项目根目录>
<ENV_PY> scripts/generate_voiceover.py full --project <项目根目录> --retry-failed

<ENV_PY> scripts/generate_voiceover.py approve-full `
  --project <项目根目录> `
  --identity-hash <刚完整试听的旁白身份> `
  --duration-decision accept_actual

<ENV_PY> scripts/generate_voiceover.py status --project <项目根目录>
<ENV_PY> scripts/validate_voiceover.py --project <项目根目录>
```

`approve-sample` 验证 identity 仍是 current sample；`approve-full` 验证 WAV、timeline 和 narration SRT 都 current，并在批准成功后原子发布新的 current timing plan。它不得改写图片 generation plan。identity 不匹配时返回退出码 5，且不修改批准状态或 timing plan。

`--duration-decision accept_actual` 仅在偏差超过 10% 时必需；阈值内省略该参数并由 manifest 记录 `within_threshold`，不得把阈值内批准伪装成超阈值人工接受。

### 11.4 字幕与最终媒体

```powershell
<ENV_PY> scripts/burn_subtitles.py --project <项目根目录>
<ENV_PY> scripts/mux_voiceover.py --project <项目根目录>
<ENV_PY> scripts/validate_final_media.py --project <项目根目录>
<ENV_PY> scripts/approve_final_media.py `
  --project <项目根目录> `
  --identity-hash <刚完整看片听音的 final identity>
```

- `burn_subtitles.py` 自动按 mode 选择唯一权威 SRT，但不处理 provider。
- `mux_voiceover.py` 只允许 `edge-tts` 模式，并校验所有批准/identity/current 前置条件。
- Disabled 模式由 `burn_subtitles.py` 在验证 captioned 候选后原子发布 `final.mp4`。
- `validate_final_media.py` 按 mode 验证不同的音频流合同，同时要求字幕像素证据和 delivery identity current。
- `approve_final_media.py` 同时支持 Disabled/Edge，只允许批准已技术验证且仍 current 的 final identity；identity 不匹配返回 5，且不得修改旧批准。任何 clean video/audio/timeline/subtitle/style/font/render profile/burn-mux contract/final SHA 变化都使该批准 stale。

### 11.5 退出码

| 退出码 | 含义 |
|---:|---|
| 0 | 成功且技术验证通过 |
| 1 | 批处理结束但存在失败 unit |
| 2 | 参数、项目、config、plan、manifest、SRT 或 timeline 无效 |
| 3 | Edge 外部请求或限流重试耗尽 |
| 4 | FFmpeg、ffprobe、字体、字幕或媒体验证失败 |
| 5 | stale、identity 不匹配或缺少人工批准 |

## 12. 新增和修改的文件

### 12.1 新增核心文件

```text
config/voice-providers.example.json
references/voiceover.md
references/subtitles.md

scripts/srt_timeline.py
scripts/voiceover.py
scripts/edge_tts_adapter.py
scripts/audio_normalization.py
scripts/subtitle_delivery.py
scripts/media_validation.py

scripts/upgrade_project.py
scripts/generate_voiceover.py
scripts/validate_voiceover.py
scripts/burn_subtitles.py
scripts/mux_voiceover.py
scripts/validate_final_media.py
scripts/approve_final_media.py

tests/test_srt_timeline.py
tests/test_voiceover.py
tests/test_voiceover_cli.py
tests/test_subtitles.py
tests/test_final_media.py
tests/test_voiceover_timing.py
tests/test_render_timing.py
tests/fixtures/voiceover/
tests/fixtures/subtitles/
```

### 12.2 修改现有文件

```text
SKILL.md
README.md

scripts/prepare_env.py
scripts/project_workspace.py
scripts/create_project.py
scripts/parse_srt.py
scripts/render_stream_whiteboard.py
scripts/stream_render.py
scripts/merge_scenes.py

tests/test_project_workspace.py
```

职责边界：

- `srt_timeline.py`：唯一 SRT 解析/写出/严格时间校验、稳定 source ordinal、共享 cue 模型和 source timing plan 构建。
- `voiceover.py`：speech units、voice plan/manifest、canonical WAV 编排、timeline、派生 SRT 和 adapter protocol；由 C1 独占修改。
- `edge_tts_adapter.py`：真实 Edge 请求、异常分类、有限重试和原始媒体返回；由 C2 独占修改，通过 C1 冻结的 adapter protocol 接入。
- `audio_normalization.py`：原始媒体到 canonical WAV 的 FFmpeg 规范化、ffprobe、SHA 和原子发布；由 C2 独占修改。
- `subtitle_delivery.py`：权威字幕选择、SRT→ASS、样式、字体度量、转义、字幕 identity。
- `media_validation.py`：统一 ffprobe、媒体流、帧数/时长、完整解码和 SHA 校验。
- `merge_scenes.py`：只合并静音场景视频。
- `burn_subtitles.py`：只烧录 current 字幕并发布 captioned video；Disabled 同时发布 final。
- `mux_voiceover.py`：只在 Edge 模式封装 current、approved canonical audio。
- `approve_final_media.py`：只在用户明确确认后持久化 current final identity，不承担技术验证或媒体生成。

## 13. 实施批次、依赖与子代理编排

### 13.1 强制编排规则

- 新实现任务的主窗口只负责编排、拆分、接口冻结、冲突裁决、进度跟踪和每批验收；**不得亲自编写业务实现**。
- 所有具体编码、测试补齐、文档同步和真实验收准备均交给子代理；子代理不得各自修改已冻结的共享合同，发现冲突先报告主窗口。
- 依赖关系允许时必须并行派发，不得把可并行的 schema/SRT、字幕/媒体验证、planner/adapter、文档/fixture 串行执行。
- 以尽快做出可运行首版为优先，不做与本计划无关的重构、风格统一或过度 review。主窗口只做接口级 review、定向测试和批次 E2E，不重复进行无证据的全量审查。
- 每个子代理任务必须有明确文件边界、输入/输出合同、定向测试和完成条件；共享文件需要先指定唯一 owner。
- 任何子代理不得自动跨越 Skill 的用户确认关卡；实现代码可以并行，真实样音批准、完整听音批准和最终成片批准仍必须等待用户明确确认。

### 13.2 批次与依赖

主窗口负责冻结 schema、manifest、退出码、文件命名、帧边界算法和跨包接口后再派发；具体编码全部交给子代理。

### 批次 A：基线、schema 与共享 SRT（先行）

#### A1：环境与项目 schema 子代理

负责：

- `prepare_env.py` feature 分层和 preflight。
- schema v1/v2 loader、create/upgrade CLI、安全相对路径。
- v2 timing plan 创建、v1 Disabled 兼容视图与显式升级持久化。
- 项目兼容测试。

完成边界：Disabled 老项目无回归；新项目写 v2；显式升级原子且可恢复。

#### A2：共享 SRT 与源全局时间轴子代理

负责：

- `srt_timeline.py`。
- `parse_srt.py` 适配。
- 前导空白、跨幕空档、末尾收口和确定性 scene 区间。
- 累计全局帧边界与 scene `startFrame/endFrameExclusive/frameCount`。

完成边界：SRT parser/时间轴测试全部通过；不再丢失全局空档。

A1 和 A2 在主代理冻结 v2 字段后可并行。

### 批次 B：先关闭 Disabled 字幕缺口

#### B1：字幕编译与 FFmpeg 烧录子代理

负责：

- `subtitle_delivery.py`、`burn_subtitles.py`、`references/subtitles.md`。
- ASS 样式、字体度量、两行换行、ASS 转义、Windows ASCII run dir。
- 字幕 contact sheet。

#### B2：媒体验证与静音合并命名子代理

负责：

- `media_validation.py`、`validate_final_media.py`。
- `merge_scenes.py` 默认输出改为 `final-video-only.mp4`。
- 原子发布和 Disabled 三层输出验证。

B1/B2 在 A2 的 SRT 接口冻结后可并行，最终由主代理联合跑 Disabled E2E。

批次 B 完成边界：现有 60 秒 smoke 项目生成 README 风格可见字幕的 `output/final.mp4`；视频 1 路 H.264、0 音频，像素检测、ffprobe 和完整解码通过。

### 批次 C：TTS 核心与恢复

#### C1：Speech units 与 voice plan 子代理

负责：

- `voiceover.py` 中的 planner、voice plan/manifest schema、hash 和 adapter protocol。
- fake provider 接口。
- planner/manifest 单测。

#### C2：Edge adapter 与 canonical WAV 子代理

负责：

- 独立 `edge_tts_adapter.py`、`audio_normalization.py`、超时/有限重试和 provider 异常分类。
- 按已冻结 protocol 返回原始媒体；canonical 规范化、ffprobe 和原子段发布通过独立 normalization 模块提供，`voiceover.py` 只负责编排，避免 C1/C2 并行修改同一核心文件。
- fake/固定 WAV 测试，自动测试不调用网络。

C1/C2 在主代理冻结接口后并行。

#### C3：样音、完整生成、批准和恢复 CLI 子代理

依赖 C1+C2，负责：

- `generate_voiceover.py`、`validate_voiceover.py`。
- sample/full/approve/status、`--retry-failed`。
- narration.wav/timeline.json/narration.srt。
- >10% duration decision 和 stale。

批次 C 完成边界：fixture 可获得 current、validated、approved 的完整旁白与真实时间轴；真实 Edge 网络验收尚未计为通过。

### 批次 D：音频时钟接入渲染与 Edge 最终交付

#### D1：annotation 与帧精确渲染子代理

负责：

- 独立 timing plan 的 audio-authoritative scene 时间；不得改写图片 generation plan。
- annotation `timingSource` 和 stale 校验。
- 消费 timeline 已冻结的累计帧边界，不得对每幕 duration 独立 `ceil`。
- 修复渲染器末尾停留静默突破总时长的问题。
- 正式渲染严格消费 project `renderProfile`，输出 1920×1080/60fps，不接受未持久化覆盖。

渲染前必须拒绝：

```text
lastElementEnd > sceneDurationMs - 500
```

不得再通过 `gaze_until = max(total_ms, cur_ms + 500)` 延长画面掩盖 annotation 错误。

#### D2：Edge 字幕/音频封装子代理

负责：

- `mux_voiceover.py`。
- delivery manifest 的 Edge 字段。
- captioned video + WAV 的 H.264/AAC 封装、时长容差和完整解码。
- `approve_final_media.py` 和最终批准 stale/current 合同。

D1 与字幕核心对 Edge timeline 的适配可并行；D2 正式联调必须等待 D1 clean video 和批次 C timeline 稳定。

批次 D 完成边界：fake provider E2E 的 `output/final.mp4` 同时有 AAC 旁白和烧录字幕，字幕来自 narration SRT，音视频无截尾。

### 批次 E：文档、fixture E2E 与真实 Edge 验收

可并行派发：

- 文档子代理：同步 `SKILL.md`、`README.md`、voiceover/subtitles reference 和所有命令。
- 测试子代理：补齐 fixture、恢复/stale、Windows path、Disabled/Edge E2E。
- 验收子代理：只读检查验收输出、ffprobe、完整解码、contact sheet 和 identity；不做过度 review。

主代理最终整合并报告每项为 `PASS`、`SKIP` 或 `BLOCKED`。

## 14. 自动测试矩阵

### 14.1 SRT/字幕单测

- UTF-8 BOM、CRLF、多行 cue。
- 保留稳定 `sourceOrdinal`，合法原编号写入 `originalIndex`；parser 重新编号不改变 cue identity。
- 中文、英文、数字和混合标点。
- 空文本、倒序时间、零时长失败。
- 重叠 cue 按首版合同失败。
- `{}`、反斜杠和伪 ASS 指令被转义。
- 中文按实际字体像素宽度换成一至两行。
- 超过两行容量失败，不静默缩字。
- ASS/SRT 毫秒格式正确。
- 相同输入、样式、字体生成确定性 ASS/hash。
- Disabled 严格选择 source SRT。
- Edge 严格选择 narration SRT；缺失时不回退。

### 14.2 Source 时间轴边界测试

- 第一 cue 从 0 开始。
- 第一 cue 非 0 开始。
- 场景边界存在 2 秒字幕空档。
- 末幕元素在权威总时长内保留至少 0.5 秒完整画面，不隐式延长总时长。
- 两幕均为非整帧毫秒边界时，逐幕帧数之和严格等于全局累计帧数。
- 多幕总帧数与源 SRT 全局时间一致。
- 分幕不删除全局空档。

### 14.3 TTS 单元/恢复测试

- 中文句末断句、逗号次级断点、短 cue 合并、长句拆分、纯标点、emoji/code point。
- cue range 连续、无遗漏、确定性 hash。
- fake provider 成功、timeout、可重试/不可重试、重试上限、queue interval、取消。
- MP3→canonical WAV 和严格 ffprobe。
- 临时文件清理、原子发布失败不破坏旧文件。
- validated segment 不重请求。
- 中断后 `--retry-failed` 只处理失败/未完成单元。
- voice/rate/source text/segmentation/provider synthesis contract 变化使受影响 checkpoint 和批准 stale；source 只改 timing 走下述复用合同。
- 源 SRT 只改时间但朗读文本、scene 边界和 synthesis identity 不变时，不重新请求 current Edge segment；只重算偏差和受影响的 source timing 身份。
- timing plan 改变不得改变 generation plan hash 或把已验证图片自动降级；图片是否需要语义复核由 source text/scene 语义变化单独决定。
- composite WAV、timeline、narration SRT 一致。
- >10% 偏差要求显式 `accept_actual`。
- sample/full approval identity 不匹配返回 5。
- final approval identity 不匹配、技术验证未通过或 final 已 stale 时返回 5，且不修改旧批准。
- voice plan audit hash 因纯 timing 改变时，segment 按 synthesis identity 复用，但批准/timeline/下游重新判定。
- timing plan 更新不改变 generation plan/image manifest identity，已验证图片可按语义复核规则保留。

### 14.4 Windows/FFmpeg 测试

- 项目路径含中文、空格和单引号。
- 所有调用使用 argv、`shell=False`。
- filter 中不出现绝对 Windows 路径。
- libass/字体缺失明确失败。
- FFmpeg 失败不破坏已有正式输出。
- 候选验证成功后才原子替换。
- 命令及实现中不存在 `-shortest`。
- 正式渲染拒绝未持久化的 fps/尺寸覆盖，render profile 变化使场景视频和 final stale。

### 14.5 Disabled 集成测试

```text
source SRT fixture
→ scene MP4 fixtures
→ final-video-only
→ ASS
→ final-subtitled-video-only
→ final.mp4
→ ffprobe
→ full decode
```

断言：

- 三个文件各为 1 video / 0 audio。
- captioned/final 为 H.264、1920×1080、yuv420p、项目 fps。
- burn 前后帧数、fps 和时长保持。
- cue 活跃时间底部区域相对 clean video 有像素差异。
- 对专用 gap fixture，cue 空档没有字幕残留；对当前连续 60 秒 smoke，记录 `gapEvidence: not_applicable_no_gap`。
- 最后一条字幕完整显示且不越过视频尾部。

### 14.6 Edge TTS fixture 集成测试

```text
SRT
→ speech units
→ fake Edge segments
→ narration.wav
→ timeline.json
→ narration.srt
→ annotation fixture
→ scene render
→ final-video-only
→ subtitle burn
→ audio mux
→ final.mp4
→ ffprobe/full decode
```

断言：

- 烧录字幕文本和时间严格来自 `audio/narration.srt`。
- `final.mp4` 恰好 1 路 H.264 + 1 路 AAC。
- 音频 24000Hz、mono。
- 视频时长与 canonical timeline 在 `max(1帧, 80ms)` 内。
- 音频尾部不截断。
- 字幕像素存在。
- 修改 voice/rate 后 narration、timeline、批准、annotation、场景视频、captioned video 和 final 全部 stale。
- 至少两幕使用非整帧 scene 边界，合并帧数仍等于全局累计目标。

## 15. 测试命令与执行层级

实施子代理应先运行其负责文件的定向测试，再由主代理运行整组：

```powershell
<ENV_PY> -m unittest tests.test_project_workspace -v
<ENV_PY> -m unittest tests.test_srt_timeline -v
<ENV_PY> -m unittest tests.test_subtitles -v
<ENV_PY> -m unittest tests.test_voiceover -v
<ENV_PY> -m unittest tests.test_voiceover_cli -v
<ENV_PY> -m unittest tests.test_voiceover_timing -v
<ENV_PY> -m unittest tests.test_render_timing -v
<ENV_PY> -m unittest tests.test_final_media -v

<ENV_PY> -m unittest discover -s tests -p "test_*.py" -v
```

如果项目当前采用其他已存在的 test runner，实施时沿用现有 runner，但必须保留以上测试边界和可单独执行能力。

## 16. 真实端到端验收

### 16.1 Disabled 模式

1. 使用当前 60 秒 smoke SRT 和已确认场景资产。
2. 生成 `final-video-only.mp4`。
3. 烧录 `source/source.srt`。
4. 生成 contact sheet，本地检查 README 风格。
   当前 smoke 的字幕连续，因此记录 `gapEvidence: not_applicable_no_gap`；另用自动 gap fixture 验证无字幕空档不残留。
5. 验证 `final.mp4` 无音频流但字幕可见。
6. `ffprobe`、完整解码、字幕像素检测全部 PASS。
7. 用户确认后执行 `approve_final_media.py`，验证 Disabled final approval 已持久化且仍 current。

### 16.2 真实 Edge TTS

仅在所有 fixture 测试通过后执行：

1. 以 `zh-CN-YunjianNeural`、默认 rate 生成短中文样音。
2. 用户完整试听并批准样音。
3. 使用短 SRT 生成完整旁白、timeline 和 narration SRT。
4. 用户完整试听并批准真实时长。
5. 按真实时钟完成 annotation、场景渲染和 clean video。
6. 烧录 narration SRT，封装 AAC。
7. 验证 `final.mp4` 为 H.264/AAC、1920×1080、yuv420p、项目 fps、24kHz mono AAC。
8. 生成 contact sheet并完整看片听音。
9. 用户确认后执行 `approve_final_media.py`，验证最终批准 identity 已持久化且仍 current。

如果外网或 Edge 服务不可用，结果记录为：

```text
自动 fixture：PASS
真实 Edge 外部验收：BLOCKED（说明网络/服务原因）
```

不得把它写成“Edge TTS 已验收通过”。

## 17. stale 与恢复矩阵

| 变化 | 必须失效 | 可以保留 |
|---|---|---|
| voice/rate/朗读文本/分段边界/provider synthesis contract 改变 | sample/full 批准、受影响 segments、WAV、timeline、narration SRT、annotation 时序、场景视频、字幕烧录、final、最终批准 | 已确认图片需语义复核；合成身份未变化的其他 segment 可保留 |
| voice plan audit hash 仅因 source timing/audit 字段改变 | sample/full 批准、偏差决定、timeline、narration SRT、annotation 时序、场景视频、字幕烧录、final、最终批准 | synthesis identity 和正式媒体均一致的 validated segment/WAV 可复用 |
| narration WAV 改变 | full 批准、timeline、annotation、场景视频、字幕烧录、final、最终批准 | 图片 |
| timeline/timing plan 改变 | narration SRT、annotation 时序、场景视频、字幕烧录、final、最终批准 | 音频仅在 identity 异常调查前保留；图片 generation plan/manifest 不因纯时序变化失效 |
| subtitle style/font hash 改变 | captioned video、final、最终批准 | clean video、音频、annotation |
| Disabled source SRT 文本/时间改变 | 字幕烧录、final、最终批准；按语义/时间影响判断更早产物 | 未受影响图片需人工判断 |
| narration SRT 单独改变 | captioned video、final、最终批准，并判为 identity 异常 | 不自动接受为 current |
| clean video hash 改变 | captioned video、final、最终批准 | 音频 |
| render profile 改变 | annotation 帧时序、所有场景视频、clean/captioned/final、最终批准 | 图片和音频可保留但需按新时钟复核 |
| voiceoverMode 改变 | timing strategy、annotation 时序、所有场景视频、字幕来源、delivery、final、最终批准 | 图片需语义复核 |
| 只改 AAC bitrate | final、最终批准 | clean/captioned video、WAV、字幕 |
| source SRT 只改时间、朗读文本不变 | Disabled 时间轴/场景视频/字幕/final/最终批准 stale；Edge 重算偏差和相关 source identity，并失效下游及最终批准 | current Edge segment/WAV 可在 synthesis identity 一致时保留 |

恢复原则：

- 保留仍满足 inputHash 和媒体合同的 validated segments。
- 不自动跨 `voiceSynthesisIdentityHash` 或媒体合同复用；voice plan audit hash 变化不等同于所有 segment synthesis identity 变化。
- 不因下游 stale 删除上游有效证据。
- 失败候选不覆盖已验证正式文件。

## 18. 文档同步要求

### `SKILL.md`

- 将十阶段确认关卡写成权威运行流程，并保留“单幕确认后才能合并”的现有门禁。
- 明确 `voiceoverMode` 选择和两种权威时钟。
- 明确样音/完整试听/真实时长/最终成片批准。
- 明确最终字幕烧录不需要浏览器或文件选择框。
- 更新所有命令、输出目录、恢复和 stale 规则。
- 明确 `output/final.mp4` 默认始终带烧录字幕。

### `README.md`

- 说明示例 GIF 的可见字幕现在由可复现脚本生成。
- 展示 Disabled 和 Edge TTS 两种流程。
- 说明三层输出文件的用途。
- 说明 Edge TTS 无 Key 但依赖外网。
- 说明 Edge 模式字幕来自 narration SRT，Disabled 来自 source SRT。
- 不再把 annotation 的 subtitle 字段描述为“仅供后续用途”。

### references

- `references/voiceover.md`：provider、配置、批准、恢复、时长、timeline、外部验收。
- `references/subtitles.md`：权威源选择、style-v1、字体、换行、ASS 安全、Windows FFmpeg、contact sheet、验收。

## 19. 完成定义

只有以下全部满足，综合实现才算完成：

- [ ] v1 Disabled 项目无回归，新 v2 项目可显式选择 mode。
- [ ] Disabled `final.mp4` 有可见烧录字幕、无音频流。
- [ ] Edge `final.mp4` 有可见烧录字幕和 AAC 旁白。
- [ ] Edge 字幕唯一来自 current `audio/narration.srt`。
- [ ] 样音、完整旁白/时长和最终成片批准均绑定 identity。
- [ ] narration WAV、timeline、narration SRT、annotation 和 final 身份可追溯。
- [ ] timing plan 与图片 generation plan 分离，纯 TTS 时序变化不破坏已验证图片 manifest。
- [ ] 分段生成可中断恢复，validated unit 不重复请求。
- [ ] 纯 source timing 变化时按 synthesis identity 复用 segment，同时正确失效批准、timeline 和下游。
- [ ] 全局 SRT 空档和首 cue 前导时长不再丢失。
- [ ] 渲染帧数按累计全局帧边界和持久化 render profile 确定，逐幕之和严格等于全局目标，不靠末尾 gaze 或 `-shortest` 掩盖错误。
- [ ] 字幕样式、字体、ASS 和最终媒体 hash 写入 delivery manifest。
- [ ] Disabled 与 fake Edge E2E 自动测试全部通过。
- [ ] Disabled 真实 smoke 的字幕像素、ffprobe 和完整解码通过。
- [ ] 真实 Edge 验收有明确 PASS 或外部 BLOCKED 记录，不能以 SKIP 冒充 PASS。
- [ ] 最终成片批准通过独立 CLI 绑定 current final identity，技术验证不自动批准。
- [ ] `SKILL.md`、`README.md`、references、命令、测试和验收规则全部同步。

## 20. 首版实施顺序摘要

```text
冻结 schema / manifest / CLI / 文件命名 / timing plan
  ↓
A1 环境与项目兼容 ─┐
A2 共享 SRT/全局时钟 ┘（可并行）
  ↓
B1 字幕编译烧录 ───┐
B2 媒体验证/合并命名 ┘（可并行）
  ↓
Disabled 字幕 E2E（先关闭当前缺口）
  ↓
C1 speech units/manifest ─┐
C2 Edge adapter/WAV ──────┘（可并行）
  ↓
C3 样音/完整音频/批准/恢复/timeline
  ↓
D1 audio-authoritative 渲染 ─┐
D2 Edge 最终封装准备 ────────┘（部分并行）
  ↓
fake Edge E2E
  ↓
文档同步 + Disabled 真实 smoke
  ↓
真实 Edge 样音和短项目验收
```

这个顺序先用批次 B 快速补齐已经确认的“最终成片无字幕”缺口，再接入完整 TTS 时钟，避免必须等全部 TTS 能力完成后才交付字幕修复；同时通过统一 SRT、媒体验证和 delivery manifest 让两条模式最终汇合到同一正式交付合同。
