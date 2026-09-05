# 正式烧录字幕合同

本文说明首版正式字幕的权威输入、固定样式、ASS 安全边界、Windows FFmpeg 调用方式、产物身份和验收要求。字幕阶段不改写旁白文案，也不需要浏览器、预览台或文件选择框。旁白模式的 `full` 只生成 current narration SRT，不编码无画面的字幕预审视频；字幕视觉审查从本合同的正式烧录与 contact sheet 开始。

## 目录

- [权威字幕源](#权威字幕源)
- [旁白阶段与正式字幕的边界](#旁白阶段与正式字幕的边界)
- [字幕样式 recipe](#字幕样式-recipe)
- [确定性换行和 ASS 安全](#确定性换行和-ass-安全)
- [字幕编码 preset 与 identity](#字幕编码-preset-与-identity)
- [Windows 与 FFmpeg 边界](#windows-与-ffmpeg-边界)
- [三层输出和 manifest](#三层输出和-manifest)
- [Contact sheet 与 gap 证据](#contact-sheet-与-gap-证据)
- [命令与验收](#命令与验收)
- [退出码与失败处理](#退出码与失败处理)

## 权威字幕源

项目的 `voiceoverMode` 决定唯一合法输入：

| mode | 权威 SRT | 必须满足 |
|---|---|---|
| `disabled` | `source/source.srt` | 文件 SHA 同时匹配 `project.json.source.sha256`、`timing-plan.sourceSrtSha256` 和 `activeTimeline` |
| `edge-tts` / `minimax` / `doubao` | `audio/narration.srt` | `timing-plan.activeTimeline` 必须绑定 current `audio/timeline.json`；timeline 文件必须以 `narrationSrt.file/sha256` 绑定该 SRT |

Edge、MiniMax 与豆包的新 delivery evidence 统一写 `sourceKind=voiceover-narration-srt`；历史 `edge-tts-narration-srt` 仅作为旧 manifest 的只读兼容别名，新项目和重建产物不得继续写 provider 错配的旧值。

旁白模式的外层 timing plan 保存 timeline 文件 SHA；timeline 内不保存等于自身文件 SHA 的自引用字段。任一文件缺失、hash 不匹配或 binding stale 时立即失败，禁止回退到 `source/source.srt`。Disabled 模式即使残留 narration SRT，也只能使用 source SRT。annotation 的 `subtitle` 字段不是正式烧录时间轴。

所有 SRT 都由 `scripts/srt_timeline.py` 严格解析：UTF-8 BOM、CRLF 和多行 cue 可读取；空文本、零时长、倒序或重叠 cue 拒绝。`sourceOrdinal` 是稳定 cue 身份，原文件中的合法编号只记录为 `originalIndex`。

Disabled 的画面与字幕从全局 0 开始，以 source SRT 最后一条 cue 的 `endMs` 收口；第一条 cue 前的空白和跨幕 cue 空档不能因分幕而消失。旁白 narration SRT 从 canonical audio timeline 的词级真实区间派生：Edge TTS 使用本地 FunASR token；MiniMax 使用同次 T2A 响应的 provider-native word 时间戳；豆包使用同次 Seed Audio 响应的 `subtitle.sentences[].words[]` 毫秒时间戳。允许首字前、句间和末字后的真实无字幕空档，不要求 cue 从 0 连续铺满整轨。两种模式的 scene 仍使用 timing plan 中连续覆盖整轨的全局 `startMs/endMs`；annotation 元素仍使用从本幕 0 开始的局部时间。

## 旁白阶段与正式字幕的边界

Edge/MiniMax/豆包 `full` 生成 current `audio/narration.srt`，但此时尚未生图。人工模式只有用户完整试听后才绑定 `FULL_IDENTITY`；自主模式在阶段 0 current 内容与制作方案已批准，且 canonical WAV 完整解码、对应 provider 的 current 词级时间证据、原稿对齐、timeline/narration SRT/current identity 与时长偏差等严格技术证据通过后写“阶段 0 授权后的技术推进” basis，不声称 AI 完整试听。

Edge TTS 字幕文本/时序必须先通过 15 秒 VAD 分段 token/timestamp 重建、顶层逐项一致性和局部语速 QA。MiniMax 必须通过同次响应的 provider-native word 字幕文件 SHA、外部 model/endpoint、合成 identity 与 canonical audio SHA binding；豆包必须通过独立 `audio/doubao-subtitles.json` 的 SHA/bytes、`provider=doubao`、`model=seed-audio-1.0`、纯文本音色与 scene 时间窗口 prompt SHA、合成/full audio identity、canonical audio SHA、响应 duration/original duration 与 timeline binding。MiniMax 与豆包都不运行 FunASR 二次识别，也不应用本地 ASR 的局部语速经验阈值。全部路径仍须通过权威原稿覆盖、语义切句、断词、caption 阅读上限、scene 尾音边界和真实 gap QA，再由 current binding 保证；视觉判断统一留给下列有真实画面的步骤：

- `final-video-only.mp4` 技术验证通过后重新从 current 权威 SRT 编译正式 `subtitles/final.ass`；
- `burn_subtitles.py` 把正式 ASS 烧录到 current clean master；
- `previews/final-subtitle-contact-sheet.png` 的本地检查；
- `validate_final_media.py` 的完整技术验证；
- 当前批准主体完整播放 `output/final.mp4` 后执行的最终媒体批准。

正式 ASS 的文本或时序若与 current narration SRT/timeline binding 不兼容，必须按 stale/identity 规则失败，不能静默换稿。完整旁白批准也不替代线稿、一次性 annotation review bundle 批准、一次性 scene review bundle 批准、正式字幕画面检查或最终成片批准。clean master 只做技术验证，不设独立人工确认。

## 字幕样式 recipe

首版样式固定，不接受命令行临时覆盖：

- PlayRes：1920×1080。
- 字体：Microsoft YaHei，固定文件 `C:\Windows\Fonts\msyh.ttc`，48px，不加粗。
- 白色主色，黑色 3px 描边，无阴影、无底框。
- ASS `Alignment=2`，底部居中。
- 左右安全边距 96px，底部边距 54px。
- 最大文本宽度 1728px，最多两行。

每次运行都检查字体文件并计算 SHA-256。manifest 只保存 family、文件名和 hash，不保存 Windows 绝对路径，也不会静默切换字体。本机已知基线 SHA-256 为 `d79c55e68b1131eea0cc1c47be4f572d964f28c682e143db2ad09c1e4cb07a3f`；运行时仍以实际文件重新计算为准。

## 确定性换行和 ASS 安全

换行使用 Pillow 从固定 `msyh.ttc` 加载 48px 字体并做真实像素度量。显式且合法的一至两行会保留；需要自动换行时优先选择中文标点、英文标点或空格后的断点，再按视觉宽度平衡确定唯一结果。任一行不得超过 1728px。文本无法放入两行时失败并要求拆分 SRT cue，不随机缩小字号。

编译器先处理换行，再转义字幕中的反斜杠、`{`、`}` 和伪 ASS override。只有编译器生成的 `\N` 可作为两行分隔，源文本中的 `{\pos(...)}`、`\N` 等内容不能成为控制指令。ASS 起始时间向下取整到厘秒，结束时间向上取整到厘秒，避免编译精度降低导致 cue 被截短。

相同的权威 SRT、`algorithm=ass_subtitle_style, version=1` 参数和字体文件必须生成逐字节一致的 ASS 与 SHA-256。正式审计文件为 `subtitles/final.ass`。

## 字幕编码 preset 与 identity

跨阶段 identity/current binding、stale、retry 和失败码的权威规则见
[`recovery-and-identity.md`](recovery-and-identity.md)。本节只定义字幕 preset、ASS、
captioned/final 产物包含哪些阶段专属 binding。

正式字幕 preset 的唯一权威配置为：

```json
{
  "execution": {
    "videoEncoding": {
      "subtitlePreset": "medium"
    }
  }
}
```

`subtitlePreset` 只允许 `medium | fast | veryfast`。缺少整个 `videoEncoding` 或只缺少 `subtitlePreset` 时，loader 直接使用默认 `medium`，无需为了默认值改写 `workspace.local.json`；非字符串、大小写变体、其他 preset 或未知字段必须拒绝。`burn_subtitles.py` 只读取该 workspace 配置，不提供临时 `--preset` 覆盖。

同一对象还可配置 scene 软件编码：`scenePreset=medium|fast|veryfast` 与 `sceneEncoderThreads=0..16`（0 表示 x264 自动）。默认仍为 `medium/0` 以兼容旧配置；性能示例针对 6 核/12 线程基线使用 `sceneRender=3`、每 encoder 2 threads，避免多个 libx264 进程各自争抢全部 CPU。它不启用 GPU 编码，也不改变 FFmpeg/libx264、CRF18、yuv420p 与发布/验证边界。

preset 进入 `algorithm=subtitle_burn, version=2` encoding recipe，并随完整参数与 recipe SHA 写入 delivery manifest 和 media technical receipt。它还进入 subtitle identity、captioned binding 以及 Disabled/旁白 final identity，因此是正式视频字节、文件大小和 identity 的输入；这与只改变本机调度、排除在作品 identity 之外的 worker concurrency 不同。

相同 preset、current SHA/bytes 与 current receipt 全部匹配时，恢复路径只做 binding，复用 current burn，不重新编码、不重新截取 contact sheet，也不重复 deep decode。preset 或 encoding recipe 变化时必须重建 ASS、字幕烧录和 downstream final，使旧 subtitle/captioned/final stale，并清空 `finalApproval`。新 candidate 仍只 deep validation 一次，发布后复用 receipt 做 binding；失败 candidate 不覆盖旧正式输出，原子发布或 manifest 更新失败时保留诊断工作目录并报错。

## Windows 与 FFmpeg 边界

每次烧录创建 ASCII 工作目录：

```text
.work/subtitle-<runId>/
  burn.ass
  fonts/msyh.ttc
  captioned.tmp.mp4
```

项目路径可以含中文、空格或单引号。所有进程调用都使用 argv、`shell=False`、`cwd=runDir`；输入和候选视频使用独立的绝对路径 argv，filter 固定为相对路径：

```text
ass=burn.ass:fontsdir=fonts
```

烧录 recipe 固定为软件 `libx264 / preset <subtitlePreset> / crf 18 / yuv420p / fps_mode passthrough / +faststart`，显式 `-map 0:v:0 -an`；preset 默认 `medium`，只允许 `medium | fast | veryfast`。不使用 `-r`，也绝不使用 `-shortest`。输入 clean video 必须先符合项目持久化 `renderProfile` 与 timing plan 全局帧数。NVENC/QSV/AMF 未实施，属于 `SKIP`；不得自动探测后切换硬件编码器。

正式帧数使用累计全局边界，而不是逐幕 duration 各自向上取整：

```text
sceneStartFrame        = 上一幕 endFrameExclusive（第一幕为 0）
sceneEndFrameExclusive = ceil(scene.globalEndMs * fps / 1000)
sceneFrameCount        = sceneEndFrameExclusive - sceneStartFrame
```

烧录前后 `frameCount` 和 fps 必须完全保持；字幕阶段不能用补帧、`-r` 或 `-shortest` 修复上游时钟错误。

运行前 preflight 必须确认 `ffmpeg`、`ffprobe`、`ass/libass` filter 和固定字体可用。候选文件先通过共享 `media_validation.validate_video` 和完整 null-sink 解码，再使用同卷 `os.replace` 原子发布。失败候选不得覆盖旧正式文件，失败 run 保留用于诊断。

## 三层输出和 manifest

```text
output/final-video-only.mp4
output/final-subtitled-video-only.mp4
output/final.mp4
```

字幕 CLI 只读取技术验证通过的 clean `final-video-only.mp4`，发布 captioned 静音诊断母版。全部单幕按 `sceneRender` 有界并行生成和逐幕检查、由 coordinator 按 generation plan 顺序发布，并通过 current scene review bundle 联合批准后，clean 合并、字幕烧录和按需音频封装属于同一条连续成片链路，中间不等待 clean master 人工确认。Disabled 模式再将同一已验证字节原子发布为 `final.mp4`，不重复编码；Edge、MiniMax、豆包模式由后续 mux CLI 使用 `-c:v copy` 封装批准后的 canonical WAV。

`manifests/delivery-manifest.json` 顶层固定为：

```text
schemaVersion / projectId / voiceoverMode / timingPlan / cleanVideo /
subtitles / captionedVideo / final / finalApproval
```

字幕阶段更新 `subtitles`、`captionedVideo`，以及 Disabled 的 `final`。`subtitles.encoding` 必须记录 `subtitle_burn` algorithm/version、`subtitlePreset`、libx264、CRF18、yuv420p、完整 parameters 与 recipe SHA；captioned/final 的 media technical receipt 也必须记录同一 subtitle encoding evidence。字幕身份覆盖 mode、权威 SRT、权威 timeline、样式 recipe、字体、compiled ASS 和 encoding recipe；captioned identity 另绑定 clean video，Disabled/旁白 final identity 均绑定 preset，Disabled final 还绑定 render profile、timing plan、burn/copy recipe 和最终媒体 SHA。

普通重跑只有在相同 preset、current SHA/bytes 和 current receipt 完全匹配时才复用 current burn，并以 binding 模式验证，不重复 deep decode、编码或 contact sheet。preset 变化时旧 subtitle/captioned/final 与最终批准全部 stale；重建成功前保留旧正式文件，成功发布后 `finalApproval` 必须为 `null`。技术验证、receipt binding 与 fixture PASS 都不得写成人工批准。

## Contact sheet 与 gap 证据

每次烧录生成 `previews/final-subtitle-contact-sheet.png`，固定抽取首条、中间条和末条字幕中点。只有权威 SRT 存在真实前导、cue 间或尾部空档时，才额外抽取空档中点并记录范围；没有空档时 manifest 必须写：

```json
"gapEvidence": "not_applicable_no_gap"
```

不得为了满足检查伪造无字幕帧。contact sheet 需要本地查看字幕位置、两行换行、黑色描边和安全边距，但不能替代像素检测、ffprobe、完整解码或当前批准主体最终完整看片批准。

contact sheet 和正式字幕流程都不需要浏览器、预览台、文件选择框或电脑控制。应直接用本地图片查看能力打开 PNG；当前批准主体在最终批准前则完整播放 `output/final.mp4`。

## 命令与验收

```powershell
<ENV_PY> scripts/burn_subtitles.py --project <项目根目录>
<ENV_PY> scripts/validate_final_media.py --project <项目根目录>

# 仅在当前批准主体完整确认 current final 后：
<ENV_PY> scripts/approve_final_media.py `
  --project <项目根目录> `
  --identity-hash <刚完整看片听音的 FINAL_IDENTITY>
```

烧录命令没有 `--preset`：它只读取 workspace JSON 中的 `execution.videoEncoding.subtitlePreset`，缺失时为 `medium`。正式多幕候选当前已经支持 `execution.concurrency.sceneRender` 控制的有界并发；`sceneRender=1` 只是可用的安全基线和运行时降级值，不是能力限制。并行 worker 只生成并深验彼此独立的单幕 candidate，coordinator 仍按 generation plan 顺序发布；部分幕失败时不得形成可批准的完整 bundle。`agentApprovalEnabled` 缺失或为 `false` 时，用户必须对全部 current scene 的有序 review bundle 一次确认；为 `true` 时，coordinator AI 必须真实审阅同一 current bundle、决定通过或返工，并在通过时调用现有 scene review 批准动作。current scene review approval 通过后才能进入合并与字幕烧录，字幕 preset 不改变这一边界。

Disabled 验收要求 clean/captioned/final 都恰好 1 路视频、0 音频；captioned/final 为 H.264、1920×1080、yuv420p、项目 fps，烧录前后帧数和时长保持。所有旁白模式的字幕都先独立烧录为 0 音频的 captioned video，最终 AAC 封装由 `mux_voiceover.py` 完成。

最终技术验证和完整旁白批准通过后，人工模式仍只有用户完整看片听音并明确确认才允许执行 `approve_final_media.py`。自主模式重验 current full audio、字幕/ASS、AAC、流结构、完整解码、帧数/时长/尾部、实际 BGM 模式与 `FINAL_IDENTITY` 后调用同一脚本，并写区分技术推进的 `reviewBasis`；不得因 AI 无法听音而阻塞，也不得写成完整听审通过。Edge/MiniMax 启用 BGM 时必须验证内置 CC0 固定混音 receipt；豆包 prompt-only 启用 BGM 时必须验证 `provider_embedded`、`model=seed-audio-1.0`、完整 prompt SHA、voice synthesis/full audio identity 与 canonical audio SHA，且不得出现固定曲目/mix receipt。任何输入变化仍使旧批准 stale。

`burn_subtitles.py` 成功输出 captioned `OUTPUT` 和 `VOICEOVER_MODE`。Disabled 会同时原子发布相同已验证字节的 `final.mp4`；旁白模式只发布 captioned master，之后必须由 `mux_voiceover.py` 封装 current、approved WAV。`validate_final_media.py` 会独立重验三层输出并把 `technicalValidation` 证据写入 delivery manifest，但不会写人工批准；`approve_final_media.py` 成功输出 `FINAL_APPROVED=<identity>`。

Disabled 没有 mux 阶段，因此用于最终批准的 identity 从 `validate_final_media.py` JSON 的 `finalIdentitySha256` 取得；旁白模式既可使用 mux 输出的 `FINAL_IDENTITY`，也必须由该独立验证结果复核 current。不得从旧日志或旧 delivery manifest 复制 identity。

## 退出码与失败处理

| 码 | 含义 |
|---:|---|
| 0 | 字幕/媒体操作成功且对应技术验证通过 |
| 2 | 参数、项目、plan、manifest、SRT 或 timeline 无效 |
| 4 | FFmpeg、ffprobe、libass、字体、ASS 编译或媒体验证失败 |
| 5 | 字幕/媒体 stale、identity 不匹配或缺少批准 |

烧录和发布始终先在本次 ASCII `.work/subtitle-<runId>/` 生成候选。候选失败不得覆盖已有 `subtitles/final.ass`、captioned master 或 final；新候选经 ffprobe 和一次完整解码后才使用 `os.replace` 原子发布，正式路径只复用该 receipt 做 SHA/bytes binding。已发布但 manifest 更新失败时保留工作目录并报错。真实 provider 不可用与字幕技术验收是两个不同结果；未执行的真实 provider/媒体必须写为 SKIP、BLOCK 或待确认，自主技术推进也不得改写为真实声音验收 PASS。
