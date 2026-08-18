# InkCue

> 字幕驱动的 AI 白板手绘视频工作流

把主题、正文或 SRT 制作为 1920x1080、60fps 的中文白板手绘视频。

项目以字幕和真实语音时间轴驱动画面：线稿按叙事顺序逐区域落墨，随后添加少量颜色，最终输出带烧录字幕的 H.264 MP4。它既是一套可安装到 Codex 的工作流 Skill，也包含可以单独调用的 Python CLI、项目状态、恢复机制和媒体校验工具。

本项目基于 [geeklee/srt-whiteboard-animation](https://github.com/geeklee/srt-whiteboard-animation) 二次开发，感谢原作者提供的创意与实现基础。

![白板动画效果：猴子山抢香蕉](examples/scene-01-monkey-mountain-stream.gif)

> 当前版本面向 Windows 本地工作流，不是浏览器应用，也不是无人值守的一键生成器。内容方案、声音、线稿、标注、场景和最终成片都保留明确的人工确认关卡。

## 特性

- 支持 `topic`、`text`、`srt` 三种输入。
- 支持静音成片和 Edge TTS 中文旁白。
- 严格解析 SRT，拒绝空文本、零时长、倒序和重叠 cue。
- 按全局累计帧边界渲染，保留首 cue 前空白和场景间空档。
- 统一暖米黄纸张、深灰线稿、少量强调色的白板风格。
- 图片生成接口兼容 OpenAI Images Generations 协议。
- 标注预览服务支持按场景检查和调整揭示区域。
- 使用 FFmpeg/libx264 单次编码正式场景，并进行 ffprobe、帧数和完整解码校验。
- 所有人工批准均绑定当前 artifact identity；上游变化后不会误用旧批准。
- 图片、语音、标注和渲染支持有界并发、断点恢复与失败候选保留。
- 326 个基于 `unittest` 的测试用例覆盖内容、时间轴、TTS、标注、渲染、字幕和最终交付。

## 支持范围

| 输入 | 内容策略 | 配音模式 |
|---|---|---|
| `srt` | 使用现有严格 SRT | `disabled` 或 `edge-tts` |
| `topic` | 生成完整旁白与分镜 | `edge-tts` |
| `text` | 保留原文或润色 | `edge-tts` |

两种正式输出模式都带可见的烧录字幕：

| 模式 | 权威时间轴 | 权威字幕 | `output/final.mp4` |
|---|---|---|---|
| `disabled` | 原始 SRT | `source/source.srt` | H.264，0 路音频 |
| `edge-tts` | 已批准的真实旁白时间轴 | `audio/narration.srt` | H.264 + AAC 旁白 |

`topic` 和 `text` 当前不支持静音模式；非 SRT 输入的目标时长必须在 15 到 600 秒之间。Edge TTS 不需要 API Key，但需要访问微软在线语音服务。

## 工作原理

```text
主题 / 正文 / SRT
        |
        v
内容与分镜方案 ---- 人工确认
        |
        v
严格 SRT + generation plan + timing plan
        |
        +---- Edge TTS 样音/完整旁白 ---- 人工确认
        |
        v
统一线稿 ---- 人工确认
        |
        v
区域标注 + 本地预览 ---- 人工确认
        |
        v
逐幕流式落墨渲染 ---- 场景联合确认
        |
        v
合并 + 字幕烧录 + 可选旁白封装 + 媒体验证
        |
        v
output/final.mp4 ---- 最终确认
```

CLI 的技术校验通过不等于人工批准。每次批准只对当时的文件、时间轴和配置 identity 有效；相关文件变化后，下游会被判定为 stale。

## 环境要求

- Windows 10/11。
- Python 3.11（推荐；环境脚本会创建独立虚拟环境）。
- `D:` 盘上的可写工作目录。当前实现要求 `workspaceRoot` 为 Windows 的 `D:` 盘绝对路径。
- FFmpeg 和 ffprobe 已加入 `PATH`，且 FFmpeg 包含 `libass`/`ass` filter 与 `libx264`。
- 系统字体 `C:\Windows\Fonts\msyh.ttc`（Microsoft YaHei）。
- 使用图片生成时，需要 OpenAI Images Generations 兼容服务及其 API Key。
- 使用 Edge TTS 时，需要可访问微软在线语音服务。
- 使用完整 Codex 编排时，需要支持本地文件、图片查看和子任务调度的 Codex 环境。

检查 FFmpeg：

```powershell
ffmpeg -version
ffprobe -version
ffmpeg -hide_banner -filters | Select-String " ass "
```

## 安装

将仓库克隆到 Codex Skills 目录：

```powershell
Set-Location "$env:USERPROFILE\.codex\skills"
git clone https://github.com/renmengwen/InkCue.git srt-whiteboard-animation
Set-Location .\srt-whiteboard-animation
```

准备本地配置。`*.local.json` 已在 `.gitignore` 中排除，请勿提交真实路径或 API Key：

```powershell
Copy-Item config\workspace.example.json config\workspace.local.json
Copy-Item config\image-providers.example.json config\image-providers.local.json
Copy-Item config\voice-providers.example.json config\voice-providers.local.json
```

至少修改 `config/workspace.local.json`：

```json
{
  "schemaVersion": 1,
  "workspaceRoot": "D:\\SRTWhiteboard",
  "execution": {
    "videoEncoding": { "subtitlePreset": "medium" },
    "agents": { "default": 1 },
    "concurrency": { "default": 1 }
  }
}
```

创建基础渲染环境：

```powershell
python scripts\prepare_env.py
```

成功时最后一行会输出实际解释器路径：

```text
ENV_PY=D:\SRTWhiteboard\runtime\.venv\Scripts\python.exe
```

后续命令使用这个解释器。需要 Edge TTS 时再安装对应 feature：

```powershell
$envPy = "D:\SRTWhiteboard\runtime\.venv\Scripts\python.exe"
& $envPy scripts\prepare_env.py --feature edge-tts
```

只检查、不安装：

```powershell
python scripts\prepare_env.py --check
& $envPy scripts\prepare_env.py --check --feature edge-tts
```

## 在 Codex 中使用

仓库位于 `$env:USERPROFILE\.codex\skills\srt-whiteboard-animation` 后，可以直接提出任务，例如：

```text
使用 $srt-whiteboard-animation，把 examples/一分钟理解习惯回路.srt
制作成无旁白白板动画，并在每个人工关卡等待我确认。
```

主题输入示例：

```text
使用 $srt-whiteboard-animation，以“为什么习惯很难改变”为主题，
制作约 60 秒、使用 Edge TTS 旁白的白板动画。
```

正文输入示例：

```text
使用 $srt-whiteboard-animation，将下面的正文适度润色后制作成白板动画，
目标 90 秒，使用 Edge TTS：<正文>
```

Codex 会读取 [SKILL.md](SKILL.md)，按照 artifact-first 工作流运行脚本，并在需要内容、声音或视觉判断时暂停等待确认。

## 最短 CLI 路径：已有 SRT、无旁白

CLI 可以独立完成确定性的项目、渲染和交付阶段，但不会替你生成分镜、图片内容或区域标注，也不会伪造人工批准。下面展示从已有 SRT 开始的主干命令。

1. 校验 SRT：

```powershell
& $envPy scripts\parse_srt.py examples\一分钟理解习惯回路.srt `
  --target-sec 60 --min-sec 45 --max-sec 75
```

2. 审阅并选用 generation plan。仓库提供了配套示例：

```text
examples/一分钟理解习惯回路-generation-plan.json
```

3. 创建静音项目：

```powershell
& $envPy scripts\create_project.py `
  --name "一分钟理解习惯回路" `
  --srt "examples\一分钟理解习惯回路.srt" `
  --plan "examples\一分钟理解习惯回路-generation-plan.json" `
  --voiceover-mode disabled
```

4. 配置真实图片 provider 后生成并验证线稿：

```powershell
& $envPy scripts\generate_images.py --project <项目根目录>
& $envPy scripts\validate_generated_images.py --project <项目根目录>
```

5. 在线稿获得确认后，准备并发布 annotation candidate。该阶段通常由 Codex 的 `annotationDrafting` 角色完成视觉判断；完整参数见：

```powershell
& $envPy scripts\validate_annotations.py prepare --help
& $envPy scripts\validate_annotations.py --help
```

6. 全量 annotation current 后启动预览并生成区域预览：

```powershell
& $envPy scripts\serve_preview.py --ensure --project <项目根目录>
& $envPy scripts\generate_annotation_previews.py --project <项目根目录> --all
```

7. 用户确认当前标注、区域、保护区和揭示时序后，写入批准：

```powershell
& $envPy scripts\approve_annotation_review.py --project <项目根目录> `
  --identity-hash <annotationReviewIdentitySha256>
```

8. 渲染全部场景并形成联合审阅 bundle：

```powershell
& $envPy scripts\render_stream_whiteboard.py --project <项目根目录> `
  --all --ink-path grid --color-fill contour-wipe

& $envPy scripts\scene_review.py --project <项目根目录>
```

9. 用户确认当前场景 bundle 后，写入批准并完成交付：

```powershell
& $envPy scripts\approve_scene_review.py --project <项目根目录> `
  --identity-hash <sceneReviewIdentityHash>

& $envPy scripts\merge_scenes.py --project <项目根目录> `
  --inputs <幕1.mp4> <幕2.mp4> <幕3.mp4>

& $envPy scripts\burn_subtitles.py --project <项目根目录>
& $envPy scripts\validate_final_media.py --project <项目根目录>
```

10. 完整观看 `output/final.mp4` 后再批准当前 final identity：

```powershell
& $envPy scripts\approve_final_media.py --project <项目根目录> `
  --identity-hash <finalIdentitySha256>
```

## Edge TTS 路径

Edge 模式在图片和标注阶段之前增加声音确认。创建项目时使用：

```powershell
& $envPy scripts\create_project.py `
  --name <项目名> --srt <字幕.srt> --plan <分镜.json> `
  --voiceover-mode edge-tts
```

生成样音、批准 voice/rate，再生成完整旁白：

```powershell
& $envPy scripts\generate_voiceover.py sample --project <项目根目录> `
  --voice zh-CN-YunjianNeural --rate 0

& $envPy scripts\generate_voiceover.py approve-sample --project <项目根目录> `
  --identity-hash <SAMPLE_IDENTITY>

& $envPy scripts\generate_voiceover.py full --project <项目根目录>
& $envPy scripts\validate_voiceover.py --project <项目根目录>
```

完整试听 `audio/narration.wav` 并检查真实时长后批准：

```powershell
& $envPy scripts\generate_voiceover.py approve-full --project <项目根目录> `
  --identity-hash <FULL_IDENTITY>
```

如果真实时长与目标时长偏差超过 10%，且用户明确接受实际时长：

```powershell
& $envPy scripts\generate_voiceover.py approve-full --project <项目根目录> `
  --identity-hash <FULL_IDENTITY> --duration-decision accept_actual
```

字幕烧录完成后，将已批准的 canonical WAV 封装进最终视频：

```powershell
& $envPy scripts\mux_voiceover.py --project <项目根目录>
& $envPy scripts\validate_final_media.py --project <项目根目录>
```

## 配置

### 工作区

[`config/workspace.example.json`](config/workspace.example.json) 包含完整示例。`execution.agents` 控制 Codex 子任务并发，`execution.concurrency` 控制本地 provider、验证和渲染 worker；两者是独立资源池，不相乘。所有并发值必须是 1 到 16 的整数。

首次运行建议从 `default: 1` 开始，确认 CPU、内存、网络和 provider 限额后再提高。`subtitlePreset` 可选 `medium`、`fast`、`veryfast`。

### 图片 provider

编辑 `config/image-providers.local.json`：

```json
{
  "schemaVersion": 1,
  "activeProvider": "primary",
  "providers": {
    "primary": {
      "protocol": "openai-images-generations",
      "baseUrl": "https://api.example.com/v1",
      "apiKey": "replace-with-real-key",
      "model": "your-image-model",
      "request": {
        "size": "1792x1024",
        "responseFormat": "b64_json",
        "timeoutSeconds": 180
      },
      "download": {
        "timeoutSeconds": 120,
        "maxBytes": 52428800
      },
      "extraBody": {}
    }
  }
}
```

请只把占位配置放进公开仓库。真实 Key 应保留在被忽略的 `*.local.json` 中。

### Edge TTS

[`config/voice-providers.example.json`](config/voice-providers.example.json) 固定了当前适配器合同和依赖版本。更换 voice、rate、pitch 或 volume 会改变语音 identity，并使相关批准与下游产物 stale。

## 项目产物

```text
D:\SRTWhiteboard\projects\<项目名>\
|-- project.json
|-- source\
|   `-- source.srt
|-- planning\
|   |-- generation-plan.json
|   |-- timing-plan.json
|   `-- voice-plan.json              # Edge 模式
|-- scenes\
|   |-- scene-01-<名称>.png
|   |-- scene-01-<名称>.annotation.json
|   `-- scene-01-<名称>-whiteboard.mp4
|-- audio\                            # Edge 模式
|   |-- narration.wav
|   |-- narration.srt
|   `-- timeline.json
|-- previews\
|-- subtitles\final.ass
|-- manifests\
|-- output\
|   |-- final-video-only.mp4
|   |-- final-subtitled-video-only.mp4
|   `-- final.mp4
`-- .work\
```

- `final-video-only.mp4`：无字幕、无音频的 clean master。
- `final-subtitled-video-only.mp4`：已烧录字幕、无音频的诊断母版。
- `final.mp4`：正式交付；始终带字幕，Edge 模式额外包含 AAC 旁白。

## 人工关卡

| 关卡 | 需要确认的内容 | 持久化 identity |
|---|---|---|
| 内容与制作方案 | 旁白、目标时长、cue 到 scene、分镜、图片提示词 | content draft |
| 样音 | voice 与 rate | sample |
| 完整旁白 | 完整 WAV 与真实时长 | full audio |
| 线稿 | 构图、风格、一致性、禁字 | 聊天确认 |
| 标注联合审阅 | 区域、`protectedRegions`、reveal 时序、预览 | annotation review |
| 场景联合审阅 | 全部 current 单幕视频及顺序 | scene review |
| 最终成片 | 完整画面、字幕和声音 | final media |

人工关卡是项目设计的一部分，不建议通过修改 manifest 绕过。

## 恢复与失败处理

图片和语音外部请求会按 attempt 记录 `prepared -> requesting -> candidate_ready -> publishing -> validated`。已经验证且 identity 未变化的结果可复用。

如果请求发出后无法确认 provider 是否完成，状态会变为 `unknown_external_outcome`。这类请求不会自动重发，以免造成重复计费；需要用户明确决定是否再次调用 provider。

常见退出码：

| 码 | 含义 |
|---:|---|
| 0 | 操作成功，技术校验通过 |
| 1 | 本地环境或批处理失败 |
| 2 | 参数、配置、项目、plan、SRT 或 timeline 无效 |
| 3 | Edge 外部请求失败或重试耗尽 |
| 4 | FFmpeg、ffprobe、字体、音频、字幕或媒体验证失败 |
| 5 | artifact stale、identity 不匹配或缺少人工批准 |

## 测试

测试使用 fake provider 和本地 fixture，不会调用真实图片服务或 Edge TTS：

```powershell
& $envPy -m unittest discover -s tests -p "test_*.py"
```

部分媒体测试依赖本机 FFmpeg、ffprobe、libass 和 Microsoft YaHei；依赖缺失时相关用例可能跳过或失败。自动测试通过不代表真实 provider、真实 Edge 服务或人工视觉/听觉验收通过。

## 仓库结构

```text
assets/       标注预览页与绘制手部素材
config/       可公开的配置模板；local 配置不入库
docs/         设计规格与实现计划
examples/     SRT、generation plan 和视觉示例
references/   内容、图片、语音、字幕和编排合同
scripts/      CLI 与核心实现
tests/        unittest 测试
SKILL.md      Codex 工作流入口和完整执行合同
```

## 已知限制

- 当前仅支持 Windows，并强制使用 `D:` 盘工作区。
- 字幕与预览固定依赖 Microsoft YaHei，不会自动回退到其他字体。
- 只实现软件编码；NVENC、QSV 和 AMF 尚未接入。
- topic/text 只支持 Edge TTS，不支持静音成片。
- 图片 provider 仅支持 OpenAI Images Generations 兼容协议。
- 视觉标注仍需要具备图片理解能力的 Codex/coordinator 或人工制作 candidate。
- 示例和自动测试不构成真实外部 provider 验收。

## 贡献

欢迎提交 Issue 和 Pull Request。建议在修改前先阅读 [SKILL.md](SKILL.md) 以及与改动相关的 `references/` 合同。

提交前请运行：

```powershell
& $envPy -m unittest discover -s tests -p "test_*.py"
```

贡献时请遵守以下边界：

- 不提交 `config/*.local.json`、API Key、真实用户内容或大体积成片。
- 不用技术 `validated` 状态代替人工批准。
- 不让失败 candidate 覆盖已经验证的正式 artifact。
- 修改 identity、stale 或恢复规则时，同时补充相应测试和文档。

## 交流

作者微信：`Remove147`

## 文档

- [内容输入与草案合同](references/content-input.md)
- [图片生成合同](references/image-generation.md)
- [Edge TTS 与真实时间轴](references/voiceover.md)
- [字幕与最终媒体合同](references/subtitles.md)
- [Subagent 编排合同](references/subagent-orchestration.md)
- [Annotation drafting role](references/annotation-drafting-role.md)

## 许可证

本项目使用 [MIT License](LICENSE)。二次发布或分发时，请同时遵守并保留上游项目的版权与许可声明。

Copyright (c) 2026 江哥是老登啊
