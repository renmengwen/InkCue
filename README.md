# InkCue

> 字幕驱动的 AI 白板手绘视频工作流

InkCue（墨序）可以把主题、正文或 SRT 制作为 1920x1080、60fps 的中文白板手绘视频。画面跟随字幕和真实语音时间轴逐区域落墨，最终输出带烧录字幕和可选旁白的 H.264 MP4。

本项目基于 [geeklee/srt-whiteboard-animation](https://github.com/geeklee/srt-whiteboard-animation) 二次开发，感谢原作者提供的创意与实现基础。

[在 Bilibili 观看 InkCue 白板动画演示](https://www.bilibili.com/video/BV1ik836ME8B/)

> 当前版本面向 Windows 本地工作流。内容、声音、线稿、标注、场景和最终成片都保留明确的人工确认关卡，不是无人值守的一键生成器。

## 核心能力

- 支持 `topic`、`text`、`srt` 三种输入。
- 支持静音成片和 Edge TTS 中文旁白。
- 按字幕事件和全局帧边界依次落墨，而不是一次性显示整张图片。
- 支持 OpenAI Images Generations 兼容的图片服务。
- 提供标注预览、局部重做、断点恢复和 stale 检查。
- 使用 FFmpeg/libx264 编码，并进行 ffprobe、帧数和完整解码验证。
- 人工批准绑定 artifact identity，上游变化后不会误用旧结果。
- 326 个 `unittest` 测试覆盖主要工作流。

## 输入与输出

| 输入 | 内容策略 | 配音模式 |
|---|---|---|
| `srt` | 使用现有严格 SRT | `disabled` 或 `edge-tts` |
| `topic` | 生成完整旁白与分镜 | `edge-tts` |
| `text` | 保留原文或润色 | `edge-tts` |

最终的 `output/final.mp4` 始终带烧录字幕：

| 模式 | 时间轴与字幕 | 最终媒体 |
|---|---|---|
| `disabled` | 原始 `source/source.srt` | H.264，静音 |
| `edge-tts` | 已批准的真实音频时间轴和 `audio/narration.srt` | H.264 + AAC 旁白 |

非 SRT 输入的目标时长必须在 15 到 600 秒之间。Edge TTS 不需要 API Key，但需要访问微软在线语音服务。

## 工作流程

```text
主题 / 正文 / SRT
        |
        v
内容与分镜确认
        |
        v
严格 SRT + generation plan + timing plan
        |
        +---- Edge TTS 样音与完整旁白确认
        |
        v
线稿确认 -> 区域标注与预览确认 -> 场景联合确认
        |
        v
合并 -> 字幕烧录 -> 可选旁白封装 -> 媒体验证
        |
        v
output/final.mp4 最终确认
```

技术校验通过不等于人工批准。每次批准只对当时的文件、时间轴和配置 identity 有效。

## 环境要求

- Windows 10/11。
- Python 3.11（推荐）。
- `D:` 盘可写工作目录；当前版本要求 `workspaceRoot` 为 `D:` 盘绝对路径。
- FFmpeg、ffprobe、libass/ass filter 和 libx264。
- `C:\Windows\Fonts\msyh.ttc`（Microsoft YaHei）。
- 图片生成需要兼容服务和 API Key；Edge TTS 需要网络。

## 快速开始

### 1. 安装

```powershell
Set-Location "$env:USERPROFILE\.codex\skills"
git clone https://github.com/renmengwen/InkCue.git srt-whiteboard-animation
Set-Location .\srt-whiteboard-animation
```

### 2. 准备配置

```powershell
Copy-Item config\workspace.example.json config\workspace.local.json
Copy-Item config\image-providers.example.json config\image-providers.local.json
Copy-Item config\voice-providers.example.json config\voice-providers.local.json
```

编辑 `config/workspace.local.json`，至少设置工作区：

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

真实 API Key 只能保存在被 `.gitignore` 排除的 `*.local.json` 中。

### 3. 准备运行环境

```powershell
python scripts\prepare_env.py
```

命令最后会输出 `ENV_PY=<解释器路径>`。需要 Edge TTS 时：

```powershell
$envPy = "D:\SRTWhiteboard\runtime\.venv\Scripts\python.exe"
& $envPy scripts\prepare_env.py --feature edge-tts
```

### 4. 在 Codex 中使用

```text
使用 $srt-whiteboard-animation，把 examples/一分钟理解习惯回路.srt
制作成无旁白白板动画，并在每个人工关卡等待我确认。
```

也可以从主题开始：

```text
使用 $srt-whiteboard-animation，以“为什么习惯很难改变”为主题，
制作约 60 秒、使用 Edge TTS 旁白的白板动画。
```

Codex 会读取 [SKILL.md](SKILL.md)，运行对应脚本，并在需要内容、声音或视觉判断时等待确认。

## 常用 CLI

CLI 适合调试和确定性阶段；完整生产工作流建议交给 Codex 编排。

```powershell
# 校验 SRT
& $envPy scripts\parse_srt.py <字幕.srt>

# 创建项目
& $envPy scripts\create_project.py --name <项目名> `
  --srt <字幕.srt> --plan <分镜.json> --voiceover-mode disabled

# 图片
& $envPy scripts\generate_images.py --project <项目根目录>
& $envPy scripts\validate_generated_images.py --project <项目根目录> `
  --review-policy user_first
# 技术 PASS 后自动生成 identity 绑定的 reviews/line-art-review-*.md；
# 主窗口只交付文件链接、identity 和异常摘要，不逐图嵌入聊天
# 改为 agent_first 时，技术验证后只准备 global visualReview 宿主 spawn package

# 标注预览
& $envPy scripts\serve_preview.py --ensure --project <项目根目录>
& $envPy scripts\generate_annotation_previews.py --project <项目根目录> --all `
  --review-policy user_first
# agent_first 只在 preview bundle 完成后增加一次预审；annotationDrafting 仍须查看原图

# 场景渲染与联合审阅
& $envPy scripts\render_stream_whiteboard.py --project <项目根目录> --all
& $envPy scripts\scene_review.py --project <项目根目录> `
  --review-policy user_first
# agent_first 只准备一次全量 bundle 预审，每幕仅抽少量关键帧

# 合并、字幕与验证
& $envPy scripts\merge_scenes.py --project <项目根目录> --inputs <场景视频...>
& $envPy scripts\burn_subtitles.py --project <项目根目录>
& $envPy scripts\validate_final_media.py --project <项目根目录>
```

上述三个阶段都支持 `--review-policy user_first|agent_first`。线稿验证成功后自动生成 `reviews/line-art-review-<identity>.md` 与 current technical manifest，主窗口只交付文件链接、identity 和异常摘要。`user_first` 在必要技术校验后记录 `semanticReview.status=skipped_by_user` 并直接交给用户；`agent_first` 只准备宿主可消费的 spawn package，由 child 通过 findings/result 文件交接完整意见，不自动批准。两种策略都保留对应人工确认关卡。

Edge TTS 的样音、完整旁白和真实时长流程见 [语音合同](references/voiceover.md)。人工批准、annotation candidate 和恢复流程的完整命令见 [SKILL.md](SKILL.md)。

## 配置与产物

- [`workspace.example.json`](config/workspace.example.json)：工作区、并发和字幕编码 preset。
- [`image-providers.example.json`](config/image-providers.example.json)：图片服务配置模板。
- [`voice-providers.example.json`](config/voice-providers.example.json)：Edge TTS 配置模板。

`execution.agents` 控制 Codex 子任务并发，`execution.concurrency` 控制本地 worker；两者是独立资源池。首次运行建议从 `default: 1` 开始。

主要项目产物：

```text
<项目根目录>/
|-- project.json
|-- source/source.srt
|-- planning/                 # generation、timing、voice plan
|-- scenes/                   # 图片、annotation、单幕视频
|-- audio/                    # Edge 旁白、SRT、timeline
|-- previews/
|-- reviews/                  # identity 绑定的线稿 Markdown 交接
|-- manifests/
`-- output/
    |-- final-video-only.mp4
    |-- final-subtitled-video-only.mp4
    `-- final.mp4
```

## 后续方向

以下是 Roadmap，不代表当前版本已经实现：

- 接入更多云端与本地配音模型，扩展音色、语言、情绪和风格控制。
- 接入更多生图模型和协议，支持按项目或场景选择模型。
- 优化图片、语音、标注、渲染和媒体验证的并发与生成速度。
- 通过精简上下文、缓存、增量构建和 artifact 复用降低 Token 与模型调用成本。
- 优化 Agent 职责划分、动态调度、能力检查、失败恢复和有序发布。
- 提升内容结构、分镜、画面一致性、标注区域和 reveal 节奏的生成质量。
- 对内容、分镜、生图、视觉审查和标注提示词进行分层、版本化和回归测试。
- 简化安装、配置和首次运行，逐步降低固定盘符、字体和平台依赖。

## 测试

自动测试使用 fake provider，不会调用真实图片或语音服务：

```powershell
& $envPy -m unittest discover -s tests -p "test_*.py"
```

自动测试通过不代表真实 provider 或人工视觉、听觉验收通过。

## 已知限制

- 当前仅支持 Windows，并要求 `D:` 盘工作区和 Microsoft YaHei。
- 只实现软件编码，尚未接入 NVENC、QSV 或 AMF。
- topic/text 只支持 Edge TTS，不支持静音模式。
- 图片 provider 当前只支持 OpenAI Images Generations 兼容协议。
- 视觉标注仍需要具备图片理解能力的 Agent 或人工处理。

## 文档与贡献

详细合同：

- [内容输入](references/content-input.md)
- [图片生成](references/image-generation.md)
- [语音与真实时间轴](references/voiceover.md)
- [字幕与最终媒体](references/subtitles.md)
- [Agent 编排](references/subagent-orchestration.md)
- [Annotation drafting role](references/annotation-drafting-role.md)

欢迎提交 Issue 和 Pull Request。涉及 provider、identity、stale、恢复或人工批准的改动，请同时补充测试和迁移说明，并且不要提交 `config/*.local.json`、API Key、真实用户内容或大体积成片。

## 交流

作者微信：`Remove147`

## 许可证

本项目使用 [MIT License](LICENSE)。二次发布或分发时，请保留上游项目的版权与许可声明。
