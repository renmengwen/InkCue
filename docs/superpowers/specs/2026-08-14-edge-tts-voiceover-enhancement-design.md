# SRT 白板动画：Edge TTS 配音与真实音频时间轴增强设计

日期：2026-08-14  
状态：对话评估完成，等待实现与书面复核  
适用 Skill：`srt-whiteboard-animation`  
参考项目：`D:\code3\Yingshu`  
首版配音范围：Edge TTS

## 1. 执行摘要

当前 `srt-whiteboard-animation` 已经具备完整的 SRT 解析、语义分镜、统一风格生图、像素级区域标注、流式笔迹渲染和多幕视频合并能力，但最终生产链仍然以静音画面为终点。为 Skill 增加 Edge TTS 后，可以把产品能力从“把字幕做成白板动画”提升为“把字幕做成带旁白的完整白板讲解视频”。

本设计的核心结论是：

> 配音必须作为可选但独立的生产阶段；启用配音时，以经过 `ffprobe` 实测并获得用户批准的 Edge TTS 音频时长作为最终时间轴，源 SRT 继续作为文本、语义分组和初始时间依据，但不再强行决定最终播放时长。

首版只支持 Edge TTS，不接入 MiniMax、MiMo、Windows System.Speech、本地 CosyVoice、Fish Speech 或 GPT-SoVITS。Skill 只借鉴 Yingshu 已验证的音频生产合同，不调用 Yingshu 后端、不读取 Yingshu 数据库或运行时配置，也不把 `D:\code3\Yingshu` 变成运行依赖。

正确实现不是“生成一段 MP3 后用 FFmpeg 塞进 `final.mp4`”，而是建立以下完整链路：

```text
源 SRT
  → 语义分镜策略
  → Edge TTS 样音
  → 用户确认音色和语速
  → 分段生成完整旁白
  → WAV 规范化与 ffprobe 实测
  → canonical 音频时间轴和派生 SRT
  → 用户完整试听并接受真实时长
  → 图像生成与标注
  → 按真实音频时长渲染白板画面
  → 合并静音视频
  → 封装 H.264/AAC 有声 MP4
  → ffprobe、完整解码和人工确认
```

## 2. 背景与口径澄清

### 2.1 当前对话确认的口径

最初讨论中曾把 Yingshu 的本地 ASR 和配音能力混为一谈。用户随后明确更正：

- 本地部署的是 ASR。
- 本次白板动画配音增强以 Edge TTS 为准。
- 不以本地 ASR 为配音参考。

因此，本设计中的“配音”只指 Edge TTS 文本转语音；FunASR、SenseVoice 或其他语音识别能力不在范围内。

### 2.2 Edge TTS 的真实边界

Edge TTS 可以由本地代码直接调用，不需要用户配置 API Key，但它不是离线本地模型。它仍然依赖外网和微软语音服务，并存在服务端规则、可用性、限流、音色列表和返回格式变化的风险。

首版必须把这些事实写进用户说明和错误信息：

- 无 API Key 不等于离线。
- 网络不可用时配音阶段会失败。
- 同样的文本、音色和语速在未来重新生成时，不保证文件字节完全一致。
- 已批准的音频应按文件哈希复用，不能为了“可重建”而无条件重新请求。
- Provider 失败时不得自动换音色或切换到其他 TTS。

## 3. 当前 Skill 基线

### 3.1 已有能力

当前 Skill 已经实现：

1. 使用 `scripts/parse_srt.py` 读取 SRT 并按 25～35 秒建议分幕。
2. 在用户确认配图策略后创建 D 盘项目。
3. 通过命名 `/images/generations` 供应商串行生成 1920×1080 线稿。
4. 使用 generation plan、manifest、SHA-256、图片尺寸和用户确认共同控制图片消费。
5. 使用 `.annotation.json` 描述 `sequence`、`startMs`、`durationMs`、`protectedRegions` 和字幕语义。
6. 使用 `render_stream_whiteboard.py` 生成连续笔迹白板动画。
7. 使用 `merge_scenes.py` 按场景顺序合并 MP4。
8. 将项目、运行环境、图片、视频和临时文件约束在 `D:\SRTWhiteboard`。

### 3.2 当前与配音相关的缺口

当前实现没有：

- 配音 provider 配置。
- 音色和语速选择。
- 代表性样音。
- 样音试听批准。
- 完整旁白音频。
- 分段配音检查点和失败恢复。
- 音频 manifest。
- 音频实际时长测量。
- 从真实音频派生的字幕时间轴。
- 完整音频试听和实际时长确认。
- 音频变化对标注和渲染的失效规则。
- 最终 MP4 的音频轨封装。
- H.264/AAC 双轨验证。
- 音频结尾未被截断的验证。

当前 `sceneDurationMs` 直接来自源 SRT 的起止时间。单幕渲染和多幕合并都是视频优先路径；现有 `merge_scenes.py` 即使处理含音频输入，也没有建立“最终音频身份必须与当前批准的配音一致”的业务合同。

## 4. Yingshu 可借鉴的能力边界

### 4.1 当前代码与配置事实

对 Yingshu 当前 `dev` 代码和非敏感本地配置的只读检查确认：

- 当前激活 TTS 为 `edge-tts/tts`。
- Provider ID 为 `edge-tts`。
- 实现模型为 `node-edge-tts`。
- 默认中文音色为 `zh-CN-YunjianNeural`。
- 当前语言为 `zh-CN`。
- 开启逐词时间边界。
- Edge TTS 不需要 API Key 和 Base URL。
- Project/Video 首版正式合同只允许当前 Edge TTS 配置。
- Yingshu 的音频生产会生成规范化 WAV、canonical cues、SRT 和 ASS。
- Yingshu 使用 `ffprobe` 校验真实音频字节、编码、声道、采样率和时长。
- 分段生成使用输入身份、检查点、文件哈希和恢复语义。
- 真实音频时长是视觉时间轴和最终渲染的依据。
- 完整听音和时长偏差处理是独立人工关卡。

本次检查没有调用真实 Edge TTS；Yingshu 后端 `127.0.0.1:3102` 当时也没有在短超时内响应。因此这些结论用于设计参考，不应写成“已完成本 Skill 的在线配音验收”。

### 4.2 应复用的合同

Skill 应复用这些设计原则：

- 冻结不含秘密的 provider、voice、rate、文本和源文件身份。
- 分段生成并保留已成功段。
- 每段生成后立即规范化和测量。
- 使用 SHA-256、字节数、相对路径和实际时长验证文件身份。
- 生成连续覆盖整轨音频的 canonical 时间轴。
- 生成与 canonical 时间轴一致的派生 SRT。
- 样音试听和完整试听分别批准。
- Voice、rate 或文本变化后旧试听批准自动失效。
- 真实音频时长与源 SRT 偏差过大时不得静默接受。
- 最终视频必须使用 H.264/AAC 并执行 `ffprobe` 和完整解码验证。

### 4.3 不应复用的耦合

Skill 不得：

- 调用 Yingshu 的 `3102` API。
- 读取 `D:\code3\Yingshu\apps\server\data\config\models.json`。
- 读取或写入 Yingshu SQLite。
- 从 Yingshu 的 `node_modules` 动态加载 `node-edge-tts`。
- 把 Yingshu Job/Worker 数据库原样复制到轻量 Skill。
- 引入 Project/Video、图片候选、审核工作台等不属于本 Skill 的业务模型。
- 复制或输出 Yingshu 的任何密钥、绝对媒体路径或运行时数据。

## 5. 目标

第一版必须达到：

1. 给 Skill 增加可选的 Edge TTS 配音模式。
2. 默认提供 `zh-CN-YunjianNeural`，允许显式选择其他 Edge voice。
3. 支持明确的 rate 配置，并冻结实际采用值。
4. 先生成短样音，必须经过用户试听确认后才能生成完整旁白。
5. 完整旁白按可恢复单元串行生成。
6. 已成功单元在后续重试中不重复请求。
7. 所有音频统一为规范化的单声道 PCM WAV。
8. 使用系统 `ffprobe` 验证真实时长、编码、声道、采样率、字节数和文件类型。
9. 生成完整旁白、canonical 时间轴和派生 SRT。
10. 完整音频必须经过用户实际试听和真实时长确认。
11. 启用配音时，动画使用批准音频的真实时间轴。
12. 输出保留静音视频，并额外生成包含音频轨的最终 MP4。
13. 最终 MP4 使用 H.264/AAC、`yuv420p` 和 `+faststart`。
14. 最终 MP4 经过 `ffprobe`、时长校验和完整 null-sink 解码。
15. 所有音频、manifest、派生字幕和临时文件继续位于 D 盘项目内。
16. Edge TTS 不可用时，保留成功段并给出可恢复错误，不自动降级。
17. 不破坏现有“只生成静音白板动画”的使用方式。

## 6. 非目标

第一版明确不实现：

- 本地 ASR。
- Windows System.Speech 配音。
- MiniMax、MiMo、OpenAI Audio、CosyVoice、Fish Speech、GPT-SoVITS。
- 多 Provider 自动故障转移。
- 多角色、多音色对话。
- 情绪、风格、SSML、克隆音色或角色声线绑定。
- 背景音乐、音效和自动 ducking。
- 音频波形编辑器。
- 浏览器试听 UI。
- 自动替用户批准样音或完整音频。
- 自动把任意配音强行压缩到源 SRT 总时长。
- 自动覆盖源 `source/source.srt`。
- 从 Edge TTS 在线音色列表自动选择“最好”的声音。
- 在测试中默认调用真实外部 Edge TTS。

后续如增加其他 TTS，只新增 provider adapter，并复用本设计的 voice plan、manifest、音频时间轴、试听关卡、失效规则和最终封装合同。

## 7. 核心设计原则

### 7.1 配音是可选阶段

项目必须显式选择：

```text
voiceoverMode = disabled | edge-tts
```

- `disabled`：保持现有静音工作流，不安装或调用 Edge TTS。
- `edge-tts`：执行本文定义的样音、完整配音、真实时间轴和音画封装流程。

不得因为运行环境安装了 `edge-tts` 就自动启用配音。

### 7.2 音频是最终时钟

启用配音后：

- 源 SRT 时间只用于初始分镜和相对节奏建议。
- 语义场景边界继续由已批准分镜决定。
- 完整旁白完成后，为每个场景计算真实 `sceneDurationMs`。
- annotation 和渲染必须消费真实音频时间轴。
- 最终视频时长必须覆盖完整音频，不能用 `-shortest` 静默截断。

### 7.3 原始输入不可变

以下文件不得被覆盖：

```text
source/source.srt
```

派生时间轴写到独立文件：

```text
audio/narration.srt
audio/timeline.json
```

### 7.4 技术验证不能替代人工听音

以下状态含义必须分开：

- `validated`：音频文件技术完整、编码正确、哈希一致、时间轴连续。
- `sampleApproved`：用户实际试听并确认音色和语速。
- `fullAudioApproved`：用户完整试听并确认整轨音频和实际时长。

技术状态不能自动写入人工批准状态。

### 7.5 不自动换音色

Edge TTS 请求失败时：

- 不改用其他 voice。
- 不改用系统语音。
- 不改 rate。
- 不重新切分已经成功的单元。
- 不删除已成功文件。
- 只报告失败单元并允许重试。

## 8. 新工作流与确认关卡

现有七步工作流扩展为九个阶段。

### 第 1 阶段：读取 SRT、输出语义分镜策略

- 解析 SRT 文本和源时间轴。
- 建议 25～35 秒场景，但该时长只是初始参考。
- 输出每幕核心表达、主体、字幕编号范围、源起止时间和源时长。
- 不创建项目、不生成图片、不生成音频。
- 等待用户确认语义分镜策略。

### 第 2 阶段：创建项目并生成代表性样音

仅在第 1 阶段确认后：

1. 创建 D 盘项目。
2. 复制原始 SRT。
3. 写入 provisional generation plan。
4. 写入 voice plan。
5. 从已批准分镜中选择一段具有代表性的中文文本。
6. 使用确定的 voice 和 rate 生成样音。
7. 规范化 WAV 并执行 `ffprobe`。
8. 展示或提供样音路径。
9. 停止并等待用户明确批准音色和语速。

用户要求更换 voice 或 rate 时，只重做样音，不生成完整旁白。

### 第 3 阶段：生成完整旁白和真实时间轴

仅在样音批准后：

1. 按朗读单元串行调用 Edge TTS。
2. 每段落盘后规范化、探测并计算哈希。
3. 写入分段检查点。
4. 合并完整 WAV。
5. 生成 canonical timeline。
6. 生成派生 SRT。
7. 计算场景实际时长。
8. 冻结更新后的 generation plan。
9. 校验完整音频和时间轴。
10. 输出源时长、实际时长和偏差比例。
11. 停止并等待用户完整试听和确认。

如果整体实际时长相对源 SRT 偏差超过 10%，用户必须明确选择：

- `accept_actual`：接受真实音频时长。
- 修改 rate 后重新生成。
- 修改文本或分镜后重新开始受影响阶段。

不能把用户未回复、此前对分镜的确认或“没有反对”视为时长接受。

### 第 4 阶段：生成线稿

仅在完整音频获得批准后：

- 按冻结后的 generation plan 生图。
- 保留现有 generation manifest、技术验证和线稿人工确认。
- 音频时长变化不影响已生成图片的像素内容，但会使 annotation 时序和渲染失效。

### 第 5 阶段：查看图片并创建 annotation

- 继续要求同时阅读字幕和实际查看图片。
- annotation 中的 `sceneDurationMs` 必须来自批准的音频场景时长。
- 元素 `subtitle` 可引用派生朗读单元或源字幕文本，但必须保存来源映射。
- `startMs` 和 `durationMs` 以场景内音频时间轴为依据。

### 第 6 阶段：生成区域预览并确认

保持现有编号、区域、保护区和叙事顺序检查。

### 第 7 阶段：保存最终标注与真实时序

- 检查 annotation 的音频身份。
- 检查所有元素时序不越过该幕真实音频时长。
- 确保最后一个绘制动作后仍有至少 0.5 秒完整画面；若音频不允许，应提示调整绘制速度或元素分配，不能悄悄延长音频。
- 用户确认最终标注与时序。

### 第 8 阶段：渲染和合并静音视频

- 各幕按真实 `sceneDurationMs` 渲染。
- 抽查开场、中段和结尾。
- 多幕合并为 `output/final-video-only.mp4`。
- 静音视频保留为可诊断中间产物。

### 第 9 阶段：封装音频并验证最终成片

- 把 `audio/narration.wav` 与 `output/final-video-only.mp4` 封装为 `output/final.mp4`。
- 不使用 `-shortest` 掩盖时长错误。
- 验证 H.264/AAC、分辨率、帧率、像素格式、音频轨、时长、哈希和完整解码。
- 停止并等待用户确认最终有声视频。

## 9. 时间轴模型

### 9.1 两类时间轴

系统必须同时保留：

```text
source timeline     源 SRT 提供的时间
audio timeline      Edge TTS 实测产生的时间
```

源时间轴负责：

- 初始分镜建议。
- 用户输入证据。
- 对比实际时长偏差。
- 无配音模式下继续作为最终时钟。

音频时间轴负责：

- 启用配音时的场景真实时长。
- annotation 元素时序。
- 静音视频渲染时长。
- 最终 MP4 时长。
- 派生 SRT。

### 9.2 不采用默认强制保时长

首版不把 Edge TTS 语音强制压入源 SRT 时间槽。原因包括：

- 不同字幕段需要不同变速比例。
- 音色听感会忽快忽慢。
- 句间停顿容易被压缩。
- 长段可能截尾。
- 多幕累计漂移。
- 绘制动作被迫追赶语音。

未来可以增加 `preserve-source-timing` 模式，但必须：

- 明确由用户选择。
- 限制允许变速范围，例如 `0.95x～1.05x`。
- 超出范围就失败并要求重选 rate 或接受真实时长。
- 生成独立的变速 manifest。
- 变速后重新试听批准。

### 9.3 场景边界保持语义稳定

完整音频生成后不应仅因时长变化自动重分所有场景。已批准场景继续绑定原字幕范围；系统只重新计算每幕实际时长。

若某一场景实测明显过短或过长，必须报告，例如：

- 少于 15 秒。
- 超过 45 秒。
- 相对源场景偏差超过 20%。

这只是复核提示，不自动改变已批准的语义分镜。

### 9.4 帧边界

当前 stream 渲染默认 60 fps。视频可表达的时长是帧数的整数倍，因此：

```text
videoFrameCount = ceil(audioDurationMs * fps / 1000)
videoDurationMs = videoFrameCount * 1000 / fps
```

最终音频不得比视频短缺或长出一个不可解释的明显区间。允许的容差应同时考虑一个视频帧和 AAC/容器取整，建议验收上限为：

```text
max(1 个视频帧, 80ms)
```

容差只能处理编码取整，不得掩盖内容截断。

## 10. 朗读单元切分

### 10.1 为什么不能机械按 SRT 条生成

很多 SRT 以屏幕可读性而不是自然语句为单位。一条可能只有数个字。如果每条字幕独立请求 TTS，会产生：

- 句子被不自然地打断。
- 每条都出现重新起音。
- 语调不能跨字幕延续。
- 请求数量过多。
- Edge TTS 限速和失败概率增加。

### 10.2 切分规则

首版应将源字幕归并为朗读单元：

1. 保留每条源字幕的编号、文本和源时间。
2. 优先在 `。！？!?；;……` 后断开。
3. `，,、：:` 作为次级断点。
4. 过短相邻字幕合并。
5. 过长句子按次级标点切分。
6. 不含可朗读汉字、字母或数字的标点单元合并到相邻单元。
7. 每个朗读单元保存 `sourceCueRange`。
8. 原始文本和规范化朗读文本分别保存哈希。

建议首版目标长度为 12～36 个 Unicode code point；这只是初始工程参数，必须通过真实听感验证后再冻结。

### 10.3 暂停

优先利用朗读文本中的标点让 Edge TTS 自己产生自然停顿。首版不在所有单元之间统一插入长静音。

如必须补静音，只允许确定性的短暂停顿配置，并将其写入 voice plan 和时间轴身份。不得在合并阶段临时猜测暂停长度。

## 11. 总体架构

```text
source/source.srt
        ↓
SrtParser + SpeechUnitPlanner
        ↓
planning/voice-plan.json
        ↓
EdgeTtsProviderAdapter
        ↓
SegmentCheckpointStore
        ↓
audio/segments/*.wav
        ↓
AudioNormalizer + FfprobeValidator
        ↓
VoiceManifestStore
        ↓
CanonicalAudioTimelineBuilder
        ↓
audio/narration.wav
audio/narration.srt
audio/timeline.json
        ↓
用户完整试听与 accept_actual
        ↓
generation plan / annotation sceneDurationMs
        ↓
现有白板场景渲染
        ↓
output/final-video-only.mp4
        ↓
VoiceoverMuxer
        ↓
output/final.mp4
        ↓
ffprobe + full decode + 用户确认
```

职责边界：

- `SpeechUnitPlanner` 只负责文本切分和源字幕映射。
- `EdgeTtsProviderAdapter` 只负责 Edge TTS 请求和原始响应。
- `AudioNormalizer` 负责格式转换，不解释业务语义。
- `VoiceManifestStore` 负责输入、输出、哈希、重试和状态。
- `CanonicalAudioTimelineBuilder` 负责连续时间轴和派生 SRT。
- annotation 和 renderer 只消费已批准时间轴，不调用 TTS。
- `VoiceoverMuxer` 只接受已经验证并获批的静音视频和音频身份。

## 12. D 盘项目目录扩展

建议结构：

```text
project.json
source/
  source.srt
planning/
  generation-plan.json
  voice-plan.json
manifests/
  generation-manifest.json
  voice-manifest.json
audio/
  segments/
    segment-0001-<inputHash>.wav
    segment-0002-<inputHash>.wav
  narration.wav
  narration.srt
  timeline.json
scenes/
  scene-01-<名称>.png
  scene-01-<名称>.annotation.json
  scene-01-<名称>-whiteboard.mp4
previews/
  voice-sample.wav
  scene-01-<名称>-annotation-preview.png
  scene-01-<名称>-preview.mp4
output/
  final-video-only.mp4
  final.mp4
.work/
  voice-sample-<运行ID>/
  voice-generate-<运行ID>/
  voice-mux-<运行ID>/
```

约束：

- 所有音频、派生字幕、manifest 和临时文件在项目根目录内。
- 路径在 JSON 中一律使用项目相对路径。
- 禁止绝对路径、`..`、空路径、反斜杠混入规范相对路径。
- 正式文件必须是普通文件，不能是符号链接或目录联接。
- 临时文件只进入当前运行 ID 的 `.work` 子目录。
- 只清理本次运行创建的临时文件，不扫描其他运行。

## 13. Edge TTS 配置

建议新增：

```text
config/voice-providers.example.json
config/voice-providers.local.json
```

首版结构：

```json
{
  "schemaVersion": 1,
  "activeProvider": "edge-tts",
  "providers": {
    "edge-tts": {
      "protocol": "edge-tts",
      "voice": "zh-CN-YunjianNeural",
      "language": "zh-CN",
      "rate": 0,
      "pitch": "default",
      "volume": "default",
      "outputFormat": "audio-24khz-48kbitrate-mono-mp3",
      "requestTimeoutSeconds": 60,
      "queueIntervalMs": 1800,
      "maxRetries": 2
    }
  }
}
```

规则：

- `protocol` 首版只能是 `edge-tts`。
- 不保存 API Key。
- `voice`、`language` 不能为空。
- `rate` 使用稳定的整数档位，建议 `-10～10`，每档映射 10%。
- `pitch` 和 `volume` 首版可以冻结为 `default`，不一定暴露给用户。
- `queueIntervalMs` 不能为负数。
- `maxRetries` 必须有小上限，不能无限重试。
- 一次完整生成只使用一个 voice/rate 身份。
- 配置变化不会自动替换已有批准音频。

如果首版不需要多 provider 配置文件，也可以把 Edge TTS 参数直接放入 voice plan；但 provider adapter 仍应保持独立接口，避免未来增加第二个 provider 时重写音频管线。

## 14. Voice Plan

位置：

```text
planning/voice-plan.json
```

建议结构：

```json
{
  "schemaVersion": 1,
  "projectId": "与 project.json 一致",
  "mode": "edge-tts",
  "provider": {
    "id": "edge-tts",
    "protocol": "edge-tts",
    "contractVersion": "srt-whiteboard-edge-tts-v1"
  },
  "selection": {
    "voice": "zh-CN-YunjianNeural",
    "language": "zh-CN",
    "rate": 0,
    "pitch": "default",
    "volume": "default"
  },
  "source": {
    "file": "source/source.srt",
    "sha256": "64位小写SHA-256"
  },
  "segmentation": {
    "contractVersion": "speech-unit-v1",
    "minCodePoints": 12,
    "targetCodePoints": 24,
    "maxCodePoints": 36
  },
  "timingPolicy": {
    "mode": "audio-authoritative",
    "durationDeviationReviewThreshold": 0.1
  }
}
```

Voice plan 不保存：

- API Key。
- Cookie 或 Edge 服务令牌。
- 绝对路径。
- 用户主目录。
- 临时请求 URL。
- 进程 ID。

## 15. Voice Manifest

位置：

```text
manifests/voice-manifest.json
```

### 15.1 顶层字段

- `schemaVersion`
- `projectId`
- voice plan 相对路径与 SHA-256
- source SRT 相对路径与 SHA-256
- `sample`
- `runs`
- `segments`
- `composite`
- `timeline`
- `approval`
- `createdAt`
- `updatedAt`

### 15.2 分段字段

每段至少记录：

- `index`
- `sourceCueRange`
- `speechTextHash`
- `inputHash`
- `status`
- `relativePath`
- `audioMime`
- `audioCodec`
- `sampleRate`
- `channels`
- `bytes`
- `durationMs`
- `sha256`
- `attempts`
- `createdAt`
- `errorStage`
- 已脱敏 `errorSummary`

状态只允许：

```text
pending
requesting
normalizing
validated
failed
cancelled
```

### 15.3 完整音频字段

`composite` 至少记录：

- `relativePath`
- `audioMime`
- `audioCodec`
- `sampleRate`
- `channels`
- `bytes`
- `durationMs`
- `sha256`
- `segmentsHash`
- `validatedAt`

### 15.4 批准字段

```json
{
  "sample": {
    "approved": true,
    "identityHash": "...",
    "approvedAt": "..."
  },
  "fullAudio": {
    "approved": true,
    "identityHash": "...",
    "durationDecision": "accept_actual",
    "approvedAt": "..."
  }
}
```

实现可以不保存用户账号身份，因为 Skill 当前没有账户系统，但必须保存批准所绑定的不可变音频身份。音频或配置变化后，旧批准不得继续有效。

## 16. Canonical Audio Timeline

位置：

```text
audio/timeline.json
```

建议结构：

```json
{
  "schemaVersion": 1,
  "projectId": "...",
  "sourceSrtSha256": "...",
  "voicePlanSha256": "...",
  "audioSha256": "...",
  "durationMs": 91732,
  "units": [
    {
      "index": 0,
      "sourceCueRange": [1, 2],
      "text": "这是第一段实际朗读内容。",
      "startMs": 0,
      "endMs": 3840,
      "segmentSha256": "..."
    }
  ],
  "scenes": [
    {
      "sceneId": "scene-01",
      "sourceCueRange": [1, 10],
      "unitRange": [0, 6],
      "startMs": 0,
      "endMs": 28760,
      "sceneDurationMs": 28760
    }
  ]
}
```

必须满足：

- 第一单元从 `0` 开始。
- 每个单元 `endMs > startMs`。
- 后一单元 `startMs` 等于前一单元 `endMs`。
- 最后一单元 `endMs` 等于完整 WAV 实测时长，允许仅用于探测取整的极小收口修正。
- 场景连续覆盖全部单元。
- 场景之间无重叠、无空洞。
- 最后场景 `endMs` 等于完整音频时长。
- 每个场景的 `sceneDurationMs = endMs - startMs`。
- 时间轴哈希进入 annotation 和最终视频身份。

## 17. Edge TTS Provider Adapter

### 17.1 实现选择

当前 Skill 使用 Python，并已经有 D 盘 Python 虚拟环境。首版建议使用 Python `edge-tts` 包，而不是新增 Node.js/npm 运行时。

原因：

- 不让 Skill 依赖 Yingshu 的 npm 安装。
- 继续使用现有 `prepare_env.py` 管理依赖。
- 便于与 Python 的项目、manifest 和文件工具集成。
- 不需要额外守护进程。

如验证发现 Python `edge-tts` 无法满足稳定字幕边界或取消需求，再单独评估自带固定 Node runtime 的 adapter；不能临时借用 Yingshu 的 `node_modules`。

### 17.2 输入身份

每次请求的 `inputHash` 至少包含：

- provider contract version。
- source SRT SHA-256。
- voice plan SHA-256。
- 朗读单元 index。
- source cue range。
- 规范化朗读文本。
- voice。
- language。
- rate。
- pitch。
- volume。
- output format。

### 17.3 请求策略

- 默认串行。
- 每次请求之间保留 `queueIntervalMs`。
- 单次超时明确受限。
- 只对连接失败、超时、HTTP 429、502、503、504 做有限重试。
- 鉴权或协议错误不反复重试。
- 每次失败保留简短中文错误摘要，不保存完整服务响应。
- 取消时删除未发布临时文件，保留已经 validated 的正式段。

### 17.4 原子发布

每段流程：

```text
Edge TTS 临时输出
  → 验证非空
  → FFmpeg 转 canonical WAV
  → ffprobe
  → 计算 SHA-256
  → 原子 rename 到 audio/segments
  → 更新 manifest
```

正式路径已存在且身份一致时复用；正式路径存在但哈希不符时必须失败，不能覆盖并伪装为恢复。

## 18. 音频规范化与验证

### 18.1 Canonical WAV

建议首版统一为：

```text
容器：WAV
编码：pcm_s16le
声道：1
采样率：24000 Hz
```

Edge TTS 默认输出 24 kHz 单声道 MP3，使用 24 kHz PCM 可以避免无意义地先降到 22.05 kHz。若实现阶段为了兼容现有测试或音频工具决定使用 22.05 kHz，必须全链路统一并写入合同，不能同一项目混用。

### 18.2 `ffprobe` 验证

每段和完整 WAV 都验证：

- 文件存在。
- 普通文件且非符号链接。
- 仅一个音频流。
- 没有视频流。
- codec 为 `pcm_s16le`。
- channels 为 `1`。
- sample rate 等于合同值。
- duration 为正数。
- reported size 等于实际字节数。
- 文件字节数大于 WAV header。
- RIFF/WAVE magic bytes 正确。

### 18.3 完整音频合并

所有段已 validated 后才能合并。合并输入按 segment index 排序，并在合并前再次核对路径、字节数和 SHA-256。

可以直接拼接相同 PCM 参数的 WAV payload，也可以用 FFmpeg concat；无论采用哪种方法，最终完整 WAV 都必须重新 `ffprobe`，不能把分段时长简单求和当作最终时长。

## 19. Annotation 与音频绑定

建议 annotation 增加：

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

校验要求：

- annotation 的 `sceneDurationMs` 等于当前 scene audio duration。
- `timingSource.timelineSha256` 等于已批准时间轴。
- `timingSource.audioSha256` 等于已批准完整音频。
- `sequence` 连续。
- 元素 `startMs` 不重叠并位于场景范围内。
- 最后元素结束后至少保留 0.5 秒完整画面。
- 音频变化后 annotation 标为 stale，不能直接渲染。

## 20. 最终音画封装

### 20.1 输出分层

```text
output/final-video-only.mp4   已验证的静音白板视频
output/final.mp4              最终有声视频
```

保留静音中间产物便于判断问题来自画面渲染、场景合并还是音频封装。

### 20.2 封装前条件

- 完整音频 validated。
- 完整音频获得用户批准。
- 音频和时间轴身份仍为 current。
- 所有 annotation 绑定相同 current timeline。
- 所有场景 MP4 已验证。
- 静音合并视频总时长与目标视频帧时长一致。

### 20.3 禁止使用 `-shortest` 掩盖错误

`-shortest` 会在任一输入先结束时停止输出，可能静默截断旁白末尾或最后几帧。首版不得把它作为正常时长对齐机制。

正确方式：

1. 先按音频时长和 fps 计算目标视频帧数。
2. 渲染或补齐静音视频到目标帧数。
3. 如 WAV 比目标视频仅短一个编码容差，可确定性补零到目标时长。
4. 音频比视频明显更长时失败。
5. 映射 `0:v:0` 和 `1:a:0`。
6. 视频尽量 `copy`，音频编码为 AAC；若输入视频不满足最终合同则明确重编码。
7. 写入 `+faststart`。

### 20.4 最终媒体合同

最终 `output/final.mp4` 至少满足：

```text
视频流数量：1
音频流数量：1
视频编码：H.264
音频编码：AAC
像素格式：yuv420p
画面尺寸：1920x1080
帧率：与白板渲染配置一致，默认 60 fps
时长：与 current audio timeline 在容差内一致
```

验收还必须执行一次完整解码到 null sink，防止只有 metadata 可读而码流中途损坏。

## 21. 失效与复用规则

| 变化 | 必须失效 | 可以保留 |
|---|---|---|
| 源 SRT 文本变化 | 样音、完整配音、音频时间轴、annotation 时序、场景渲染、最终视频 | 已生成图片需按场景语义重新判断 |
| 源 SRT 只有时间变化且文本不变 | 分镜源时长、时长偏差判断、annotation 时序、最终视频 | 已批准音频和图片可保留，但需重新绑定时间策略 |
| Voice 变化 | 样音批准、完整配音、完整听音批准、时间轴、annotation、全部渲染 | 图片 |
| Rate 变化 | 样音批准、完整配音、完整听音批准、时间轴、annotation、全部渲染 | 图片 |
| Speech-unit 切分合同变化 | 完整配音、时间轴、派生 SRT、annotation、全部渲染 | 样音仅在文本和 voice/rate 相同时可人工决定是否重用；默认失效更安全 |
| 某一图片变化 | 对应 annotation、对应场景渲染、最终视频 | 配音、其他图片 |
| Annotation 区域或时序变化 | 对应场景渲染、最终视频 | 配音、图片 |
| 完整音频文件哈希变化 | 完整听音批准、时间轴、annotation、全部渲染 | 图片 |
| 最终合并参数变化 | 最终视频 | 源音频、图片、annotation、单幕视频视身份决定 |

任何 stale 产物可以作为历史文件保留，但不能作为 current 产物进入下一阶段。

## 22. 错误、重试与恢复

### 22.1 可自动重试

- DNS 或连接失败。
- 明确的请求超时。
- Edge 服务短暂不可用。
- HTTP 429。
- HTTP 502、503、504。

### 22.2 不自动重试

- Voice 不存在。
- 配置无效。
- 源 SRT 或 voice plan 身份变化。
- 输出路径越界。
- WAV 无法验证。
- 已完成检查点损坏。
- 正式文件与登记哈希不一致。
- FFmpeg 或 ffprobe 不存在。
- 用户取消。

### 22.3 批量行为

- 严格按朗读单元顺序串行。
- 单元失败后可停止本轮完整生成，避免在明显配置错误时继续大量请求。
- 已 validated 的单元永久保留。
- `--retry-failed` 只处理失败或未完成单元。
- 已 validated 单元不会被重请求或覆盖。
- Voice plan 哈希变化后不得沿用旧请求检查点。
- 合并阶段失败不删除已验证分段。
- 最终 mux 失败不删除静音视频或完整 WAV。

### 22.4 退出码建议

- `0`：目标操作成功并通过技术验证。
- `1`：批量完成但存在失败单元。
- `2`：参数、项目、配置、voice plan、manifest 或时间轴无效。
- `3`：外部 Edge TTS 请求失败或限流重试耗尽。
- `4`：FFmpeg/ffprobe 或媒体验证失败。
- `5`：当前产物 stale 或缺少人工批准。

## 23. CLI 草案

### 23.1 样音

```powershell
<ENV_PY> scripts/generate_voiceover.py sample `
  --project <项目根目录> `
  --voice zh-CN-YunjianNeural `
  --rate 0
```

成功输出：

```text
SAMPLE_AUDIO=<项目根目录>\previews\voice-sample.wav
SAMPLE_IDENTITY=<sha256>
```

### 23.2 完整配音

```powershell
<ENV_PY> scripts/generate_voiceover.py full --project <项目根目录>
```

只重试失败单元：

```powershell
<ENV_PY> scripts/generate_voiceover.py full --project <项目根目录> --retry-failed
```

### 23.3 配音验证

```powershell
<ENV_PY> scripts/validate_voiceover.py --project <项目根目录>
```

### 23.4 最终封装

```powershell
<ENV_PY> scripts/mux_voiceover.py `
  --project <项目根目录> `
  --video <项目根目录>\output\final-video-only.mp4 `
  --audio <项目根目录>\audio\narration.wav `
  --output <项目根目录>\output\final.mp4
```

CLI 不能使用“存在文件即代表已批准”的隐式规则。样音批准和完整音频批准必须由明确的工作流状态或人工确认后写入 manifest。

## 24. 预计新增与修改文件

### 24.1 新增

```text
config/voice-providers.example.json
references/voiceover.md
scripts/voiceover.py
scripts/generate_voiceover.py
scripts/validate_voiceover.py
scripts/mux_voiceover.py
tests/test_voiceover.py
tests/test_voiceover_cli.py
```

### 24.2 修改

```text
SKILL.md
README.md
scripts/prepare_env.py
scripts/project_workspace.py
scripts/create_project.py
scripts/parse_srt.py
scripts/render_stream_whiteboard.py
scripts/merge_scenes.py
tests/test_project_workspace.py
tests/test_image_generation_cli.py
```

具体实现时，如果 `merge_scenes.py` 保持只负责静音视频，而所有音频逻辑进入 `mux_voiceover.py`，应避免在 `merge_scenes.py` 中混入 provider 和 approval 逻辑。

## 25. 测试设计

### 25.1 文本切分

- 中文句末标点正确断句。
- 逗号作为次级断点。
- 短字幕合并。
- 长句按最大长度拆分。
- 纯标点合并到相邻单元。
- Unicode code point 长度正确，不按 UTF-16 字节误算。
- 每个朗读单元保留连续源字幕范围。
- 相同输入产生确定性单元和哈希。

### 25.2 Voice plan

- 默认 Edge TTS 配置。
- 未知 protocol 失败。
- voice、language 为空失败。
- rate 越界失败。
- source SRT SHA-256 不一致失败。
- voice plan 不含绝对路径或秘密字段。

### 25.3 Provider adapter

自动测试默认使用本地 fake provider 或固定 WAV fixture，不调用真实 Edge TTS：

- 成功生成临时音频。
- 请求超时。
- 可重试错误。
- 不可重试错误。
- 重试次数上限。
- 队列间隔。
- 取消。
- 临时文件清理。
- 已成功段不重复调用。
- Voice/rate 变化产生不同 input hash。

### 25.4 音频规范化

- MP3 转 canonical WAV。
- WAV magic bytes。
- 单声道、采样率、PCM 编码。
- 多音轨失败。
- 含视频流失败。
- 空文件失败。
- 损坏 WAV 失败。
- 字节数、时长和 SHA-256 验证。
- 原子落盘失败不破坏已有有效段。

### 25.5 Manifest 和恢复

- 所有段成功。
- 中间一段失败。
- `--retry-failed` 只重试失败段。
- 运行中断后的非终态恢复。
- 检查点存在但文件缺失时失败。
- 文件被修改后哈希校验失败。
- Voice plan 改变后旧检查点拒绝复用。
- 完整 WAV 合并失败后分段保留。
- manifest 和日志不含秘密或绝对路径。

### 25.6 时间轴

- 第一单元从 0 开始。
- 单元连续无空洞。
- 末单元收口到完整 WAV 实测时长。
- 场景覆盖所有单元。
- 场景之间无重叠。
- 派生 SRT 与 canonical units 一致。
- 源 SRT 不被覆盖。
- 实际时长偏差超过 10% 时要求明确决策。

### 25.7 Annotation 与 stale

- Annotation 绑定 timeline/audio hash。
- Scene duration 与音频场景时长一致。
- Voice/rate 变化使 annotation stale。
- 图片变化只使对应场景 annotation stale。
- Stale annotation 不能渲染。
- 元素时序不能越界。
- 结尾至少 0.5 秒完整画面。

### 25.8 最终媒体

- 静音视频保留。
- 最终视频包含一个 H.264 视频流和一个 AAC 音频流。
- 分辨率 1920×1080。
- 像素格式 yuv420p。
- 帧率与项目配置一致。
- 最终时长与音频时间轴在容差内。
- 不使用 `-shortest` 截断音频。
- 完整 null-sink 解码成功。
- 最终文件 SHA-256 和媒体信息写入 manifest。

### 25.9 现有链路回归

至少覆盖：

```text
SRT fixture
→ 语义分镜
→ fake Edge TTS WAV
→ canonical timeline
→ 本地模拟生图
→ annotation fixture
→ 白板场景短视频
→ 静音合并
→ 有声 MP4
→ ffprobe
→ full decode
```

同时验证 `voiceoverMode=disabled` 时原有静音工作流保持不变。

### 25.10 真实 Edge TTS 验收

自动测试通过后，单独执行一次真实 Edge TTS 手工验收：

1. 使用一个短中文样音。
2. 确认 `zh-CN-YunjianNeural` 可用。
3. 确认 WAV 规范化和 `ffprobe`。
4. 用户试听样音。
5. 使用短 SRT 完成整链视频。

若网络或 Edge 服务不可用，真实验收标记为“未执行/阻塞”，不得把 fixture 通过写成外部 Edge TTS 已验收。

## 26. 分阶段实施计划

### Phase 1：配音合同与核心基础

目标：建立独立、可测试、不依赖 Yingshu 的 Edge TTS 基础。

内容：

- Voice plan schema。
- Voice manifest schema。
- Speech-unit planner。
- Edge TTS adapter 接口。
- Fake provider。
- Canonical WAV 规范化。
- `ffprobe` 验证。
- 单元测试。

完成边界：能从固定文本生成或模拟生成一段可验证 WAV，但还不改动画时间轴。

### Phase 2：样音、完整配音与恢复

目标：完成真实的音频生产阶段和人工门禁。

内容：

- 样音 CLI。
- 样音批准身份。
- 分段完整生成。
- 串行队列、超时、有限重试和取消。
- 检查点和 `--retry-failed`。
- 完整 WAV。
- Canonical timeline。
- 派生 SRT。
- 完整听音批准。
- 时长偏差决策。

完成边界：获得 current、validated、approved 的完整旁白和真实时间轴。

### Phase 3：真实时间轴接入白板渲染

目标：让 annotation 和场景视频跟随 current 音频。

内容：

- Project schema 扩展。
- Generation plan 实际时长冻结。
- Annotation timing source。
- Stale 规则。
- 场景帧数与音频时长对齐。
- 静音场景和合并视频验证。

完成边界：`final-video-only.mp4` 与批准音频时间轴一致。

### Phase 4：最终封装和端到端验收

目标：交付有声白板 MP4。

内容：

- `mux_voiceover.py`。
- H.264/AAC 封装。
- 时长容差。
- `ffprobe`。
- Full decode。
- 最终 manifest。
- 无配音模式回归。
- Fixture 端到端验收。
- 可选真实 Edge TTS 验收。
- 更新 `SKILL.md` 和使用说明。

完成边界：获得 current、validated、可完整播放且经过用户确认的 `output/final.mp4`。

## 27. 风险与缓解

### 27.1 Edge TTS 服务变化

风险：非正式 API 行为变化或音色不可用。

缓解：provider adapter 隔离、固定依赖版本、有限重试、明确外部验收、禁止自动 fallback。

### 27.2 请求数量过多

风险：SRT 过碎导致请求多、耗时长、容易限流。

缓解：朗读单元归并、串行队列、检查点、成功段复用、合理长度上限。

### 27.3 分段语气不连贯

风险：每段独立 TTS 导致重新起音。

缓解：按自然句归并，避免机械逐字幕生成；真实样音和完整试听作为人工门禁。

### 27.4 时长变化引发返工

风险：先做完图片和标注，再发现音频时长不同。

缓解：完整配音和时长批准放在生图之前。

### 27.5 最后一帧或音频尾部被截断

风险：`-shortest`、帧取整或 AAC 时长差异。

缓解：音频权威、向上取整帧数、禁止默认 `-shortest`、容差校验和完整解码。

### 27.6 重新生成不完全可重复

风险：Edge 服务侧更新导致相同输入产生不同字节。

缓解：把实际音频文件、字节数和 SHA-256 作为 current 身份；批准后优先复用，不追求跨时间字节重建。

### 27.7 Skill 复杂度膨胀

风险：为了参考 Yingshu 引入数据库、服务和前端工作台。

缓解：只复用合同，不复制产品架构；使用 JSON manifest、项目文件和 CLI 保持轻量。

## 28. 完成标准

只有全部满足以下条件，Edge TTS 第一版才算完成：

1. 配音是显式可选模式，静音模式无回归。
2. Edge TTS 是第一版唯一 provider。
3. 样音生成后必须等待用户确认 voice/rate。
4. 未批准样音不能生成完整旁白。
5. 完整旁白按可恢复单元生成。
6. 已成功段不因其他段失败而删除或重复请求。
7. 每段和完整 WAV 都经过真实 `ffprobe`。
8. Voice manifest 不含秘密和绝对路径。
9. 源 SRT 原样保留。
10. 派生 SRT 与 canonical audio timeline 一致。
11. 完整音频必须完整试听并批准。
12. 超过 10% 的时长偏差要求明确 `accept_actual` 或重新处理。
13. Annotation 绑定 current audio/timeline hash。
14. Voice/rate/audio 变化后下游正确 stale。
15. 场景和最终静音视频按真实音频时间轴渲染。
16. 最终视频包含 H.264 视频轨和 AAC 音频轨。
17. 不使用 `-shortest` 掩盖时长错误。
18. 最终视频通过 `ffprobe`、时长检查和完整解码。
19. Fixture 端到端链路通过。
20. 真实 Edge TTS 验收通过，或因外部条件明确标记未执行，不虚报。
21. `SKILL.md` 的确认关卡、目录约定、命令和质量检查与实现一致。
22. Skill 快速校验和自动测试通过。

## 29. 已确定决策与待实现校准项

### 29.1 已确定

- 第一版以 Edge TTS 为准。
- 本地 ASR 不属于配音范围。
- 配音是可选能力。
- 启用配音后音频时长是最终时钟。
- 源 SRT 不覆盖。
- 先样音批准，再生成完整旁白。
- 完整音频必须试听批准。
- 不依赖 Yingshu 运行时。
- 不自动切换 provider 或 voice。
- 不默认强制保持源 SRT 总时长。
- 静音中间视频和最终有声视频分别保留。

### 29.2 实现时需要通过样例校准

- Python `edge-tts` 的固定版本。
- 朗读单元最小、目标和最大长度。
- 是否使用逐词 boundary 辅助生成更细粒度字幕。
- Canonical WAV 采用 24 kHz 还是 22.05 kHz。
- Edge TTS 的 rate 映射范围。
- 请求间隔和超时默认值。
- 场景时长异常提示阈值。
- 最终媒体容差的精确毫秒数。

这些参数不得凭感觉一次性永久硬编码。应先用至少一个短中文 SRT 和一个 3～5 分钟中文 SRT 测量请求稳定性、自然停顿、总耗时、时长偏差和最终听感，再冻结为合同。

## 30. 版本管理说明

当前目录 `C:\Users\MOVER\.codex\skills\srt-whiteboard-animation` 不是 Git 仓库。本设计文档可以写入并继续复核，但当前目录无法产生 Git commit。实现阶段不得擅自执行 `git init`。

本设计只定义后续实现基线，不代表 Edge TTS 已接入、外部语音已调用、音画合成已完成或真实成片已经验收。
