# InkCue

> 字幕驱动的 AI 白板手绘视频工作流

InkCue（墨序）可以把主题、正文或 SRT 制作为 1920x1080、60fps 的中文白板手绘视频。画面跟随字幕和真实语音时间轴逐区域落墨，最终输出带烧录字幕和可选旁白的 H.264 MP4。

本项目基于 [geeklee/srt-whiteboard-animation](https://github.com/geeklee/srt-whiteboard-animation) 二次开发，感谢原作者提供的创意与实现基础。

[在 Bilibili 观看 InkCue 白板动画演示](https://www.bilibili.com/video/BV1ik836ME8B/)

> 当前版本面向 Windows 本地工作流。内容、声音、线稿、标注、场景和最终成片都保留明确的人工确认关卡，不是无人值守的一键生成器。

## 核心能力

- 支持 `topic`、`text`、`srt` 三种输入。
- 支持静音成片、Edge TTS 和 MiniMax 中文旁白。
- 按字幕事件和全局帧边界依次落墨，而不是一次性显示整张图片。
- 支持 OpenAI Images Generations 兼容的图片服务。
- 提供标注预览、局部重做、断点恢复和 stale 检查。
- 正式多幕按 `sceneRender` 有界并行生成 candidate，并由 coordinator 按 generation plan 顺序复核、原子发布。
- 使用 FFmpeg/libx264 编码，并进行 ffprobe、帧数和完整解码验证。
- 人工批准绑定 artifact identity，上游变化后不会误用旧结果。
- 326 个 `unittest` 测试覆盖主要工作流。

## 输入与输出

| 输入 | 内容策略 | 配音模式 |
|---|---|---|
| `srt` | 使用现有严格 SRT | 默认读取 `activeProvider`；明确静音时为 `disabled` |
| `topic` | 生成完整旁白与分镜 | 自动读取 `activeProvider` |
| `text` | 保留原文或润色 | 自动读取 `activeProvider` |

topic/text 的旁白 provider 不需要用户选择。skill 始终读取自身目录
`config/voice-providers.local.json` 的 `activeProvider`，自动冻结为 `edge-tts` 或
`minimax`；缺少 `voiceoverMode` 的内容输入会自动补入该值，传入不一致值会被拒绝。
只有明确要求静音时，传统 SRT 才显式使用 `--voiceover-mode disabled`。

最终的 `output/final.mp4` 始终带烧录字幕：

| 模式 | 时间轴与字幕 | 最终媒体 |
|---|---|---|
| `disabled` | 原始 `source/source.srt` | H.264，静音 |
| `edge-tts` / `minimax` | 已批准的真实音频时间轴和 `audio/narration.srt` | H.264 + AAC 旁白 |

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
线稿确认 -> 区域标注与预览确认 -> 正式多幕有界并行 -> 场景联合确认
        |
        v
合并 -> 字幕烧录 -> 可选旁白封装 -> 媒体验证
        |
        v
output/final.mp4 最终确认
```

技术校验通过不等于人工批准。每次批准只对当时的文件、时间轴和配置 identity 有效。

正式多幕渲染会记录 `configuredSceneRenderConcurrency` 与 `readySceneCount`，并按 `effectiveSceneRenderConcurrency = min(configuredSceneRenderConcurrency, readySceneCount)` 计算有效 worker 数。worker 只生成并深验彼此独立的单幕 candidate；coordinator 即使收到乱序结果，也必须按 generation plan 顺序复核 current binding 并原子发布。任一必需幕失败时 batch 仍为 `FAIL`；即使已有部分幕成功发布，也不能进入全量 scene review。只有全部必需幕 current、用户明确批准 current 有序 scene bundle 后，才允许合并。

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

先单独验证当前回合是否真的能写目标工作区：

```powershell
python scripts\prepare_env.py --check-workspace-access
```

成功会输出 `WORKSPACE_ACCESS={..."code":"workspace_access_ok"...}`，并已真实完成
create/write/flush/read/delete。`workspace_write_denied` 表示当前进程或 Windows ACL
拒绝目标路径；若 UI 刚切换为完全访问，必须在新回合重新运行本预检。
`CreateProcess rejected by policy` 属于命令启动前的宿主策略拦截，不是 Python
文件写入失败；不要把两者都报告成“目录不可写”。

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
  --srt <字幕.srt> --plan <分镜.json> --voiceover-mode disabled `
  --background-music disabled

# 不传 --voiceover-mode 时，新项目读取 config/voice-providers.local.json 的 activeProvider；
# 如果需要明确创建静音项目，请显式传 --voiceover-mode disabled。
# 阶段 0 还需让用户选择 --background-music enabled|disabled；enabled 使用内置 CC0
# 轻音乐，以固定 -15 dB 在最终旁白封装阶段混入，不增加独立人工 Gate。

# Edge：生成样音；完整试听并明确确认后，才批准刚试听的 current identity
& $envPy scripts\generate_voiceover.py sample --project <项目根目录> `
  --voice zh-CN-YunjianNeural --rate 0
& $envPy scripts\generate_voiceover.py approve-sample --project <项目根目录> `
  --identity-hash <刚完整试听的-SAMPLE_IDENTITY>

# Edge：生成并技术校验完整旁白；完整试听并确认真实时长后，才批准 current identity
& $envPy scripts\generate_voiceover.py full --project <项目根目录>
& $envPy scripts\validate_voiceover.py --project <项目根目录>
& $envPy scripts\generate_voiceover.py approve-full --project <项目根目录> `
  --identity-hash <刚完整试听旁白所对应的-FULL_IDENTITY> `
  --review-policy user_first
# 仅当真实时长偏差超过 10% 且用户明确接受时，approve-full 再附加：
#   --duration-decision accept_actual

# 图片
& $envPy scripts\generate_images.py --project <项目根目录>
# 旁白项目默认读取 approve-full 冻结值；显式参数只能与冻结值一致
& $envPy scripts\validate_generated_images.py --project <项目根目录>
# 技术 PASS 后自动生成 identity 绑定的 reviews/line-art-review-*.md；
# 主窗口只交付文件链接、identity 和异常摘要，不逐图嵌入聊天
# 改为 agent_first 时，技术验证后只准备 global visualReview 宿主 spawn package

# 标注预览
& $envPy scripts\serve_preview.py --ensure --project <项目根目录>
# 旁白项目继承 approve-full 冻结的 review policy
& $envPy scripts\generate_annotation_previews.py --project <项目根目录> --all
# agent_first 只在 preview bundle 完成后增加一次预审；annotationDrafting 仍须查看原图
# 仅在用户一次确认 current 标注、区域预览、protectedRegions 与 reveal 时序后：
& $envPy scripts\approve_annotation_review.py --project <项目根目录> `
  --identity-hash <annotationReviewIdentitySha256>

# 场景渲染与联合审阅
& $envPy scripts\render_stream_whiteboard.py --project <项目根目录> --all
# 旁白项目继承 approve-full 冻结的 review policy
& $envPy scripts\scene_review.py --project <项目根目录>
# agent_first 只准备一次全量 bundle 预审，每幕仅抽少量关键帧
# 仅在用户确认全部 current scene 的有序 bundle 后：
& $envPy scripts\approve_scene_review.py --project <项目根目录> `
  --identity-hash <sceneReviewIdentityHash>

# 合并、字幕、按需旁白封装与验证
# --inputs 必须严格使用 current approved scene review bundle 所绑定的 scene 集合与
# generation plan 顺序；不得用目录枚举、完成顺序或旧日志自行拼接输入。
& $envPy scripts\merge_scenes.py --project <项目根目录> --inputs <场景视频...>
& $envPy scripts\burn_subtitles.py --project <项目根目录>
# 仅 Edge TTS / MiniMax：
& $envPy scripts\mux_voiceover.py --project <项目根目录>
& $envPy scripts\validate_final_media.py --project <项目根目录>
# 推荐：scene bundle 已批准后，用一个确定性 runner 连续完成上述全部技术步骤：
& $envPy scripts\run_phase.py --project <项目根目录> --phase final-delivery
# 仅在用户完整观看 current final（旁白模式还须完整听音）并明确确认后：
& $envPy scripts\approve_final_media.py --project <项目根目录> `
  --identity-hash <刚完整看片听音的-FINAL_IDENTITY>
```

`merge_scenes.py` 会在写 concat 列表或 candidate 之前硬校验 current scene review approval；批准缺失、stale、scene 集合或输入顺序不匹配时返回退出码 5。批准通过后，`merge_scenes.py → burn_subtitles.py →（旁白模式）mux_voiceover.py → validate_final_media.py` 是连续技术链路，clean master 不增加人工关卡。技术验证完成后仍必须停在最终成片人工确认；CLI 不读取或推断聊天批准。

上述三个阶段都支持 `--review-policy user_first|agent_first`。旁白项目必须在 `approve-full` 时显式选择并冻结，后续不传参时自动继承；显式传入冲突值会 fail-closed，不能静默改回 `user_first`。旧项目若已有批准但缺少该字段，应对 current `FULL_IDENTITY` 重新执行一次带 `--review-policy` 的 `approve-full`。线稿验证成功后自动生成 `reviews/line-art-review-<identity>.md` 与 current technical manifest，主窗口只交付文件链接、identity 和异常摘要。`user_first` 在必要技术校验后记录 `semanticReview.status=skipped_by_user` 并直接交给用户；`agent_first` 只准备宿主可消费的 spawn package，由 child 通过 findings/result 文件交接完整意见，不自动批准。两种策略都保留对应人工确认关卡。

Edge TTS / MiniMax 的样音、完整旁白和真实时长流程见 [语音合同](references/voiceover.md)。人工批准、annotation candidate 和恢复流程的完整命令见 [SKILL.md](SKILL.md)。

### Phase 4 可选 coordinator runner

需要减少命令启动和主窗口往返时，可以使用可选 runner 串联本地确定性步骤；它不会自动批准，也不会把图片/TTS provider 请求藏进 agent task：

```powershell
& $envPy scripts\run_phase.py --project <项目根目录> --phase annotation-preview
& $envPy scripts\run_phase.py --project <项目根目录> --phase final-delivery
```

`annotation-preview` 完成 annotation technical validation、current receipt 复用、candidate/区域预览、contact sheet 和 review manifest 后，必须停在 annotation 联合人工确认。`final-delivery` 在 current scene bundle 已批准后，同一进程连续执行 merge → burn →（旁白模式）mux → final validation，输出每步 `timingsMs` 和总墙钟时间，然后停在最终看片/听音 Gate。两者都输出 artifact、identity、status、`approvalWritten=false` 和下一步需要的明确用户回复；技术 PASS、candidate、receipt 或 agent findings 都不等于用户批准。

runner 到达预期人工 Gate 时输出 `status=WAITING_HUMAN_GATE`、`technicalStatus=PASS`、`processOutcome=completed_waiting_for_user`，并以进程退出码 0 结束，避免通用 PowerShell/桌面包装层显示为技术失败。自动化仍必须读取 JSON，看到 `approvalWritten=false` 时停止；退出码 0 绝不授权调用批准脚本或继续需要批准的下游。

runner 中断后可直接恢复，也可退回逐步 CLI；保留 current binding 的步骤可以复用 receipt，binding 变化则重新 deep validation 并 fail closed：

```powershell
& $envPy scripts\generate_annotation_previews.py --project <项目根目录> --all --review-policy user_first
& $envPy scripts\approve_annotation_review.py --project <项目根目录> `
  --identity-hash <annotationReviewIdentitySha256>
```

失败只重做受影响步骤，不重发 provider 请求；`unknown_external_outcome` 不自动重试，旧批准也不会跨 identity/manifest/timeline/SRT 变化复用。完整字段合同、Gate 停止协议与恢复矩阵见 [Phase 4 runner 参考](references/phase-4-runner.md)。

## 配置与产物

- [`workspace.example.json`](config/workspace.example.json)：首次运行的全 `1` 安全基线，不主动增加外部请求或本机负载。
- [`workspace.performance.example.json`](config/workspace.performance.example.json)：性能配置示例，只能作为测量后调优的起点，不是默认配置。
- [`image-providers.example.json`](config/image-providers.example.json)：图片服务配置模板。
- [`voice-providers.example.json`](config/voice-providers.example.json)：Edge TTS / MiniMax 配置模板。

`execution.agents` 控制 Codex 子任务并发，`execution.concurrency` 控制本地 worker；两者是独立资源池。首次运行直接使用 `workspace.example.json` 的全 `1` 安全基线。`workspace.performance.example.json` 中高于 `1` 的值只是性能示例；启用前需基于实际测量评估 CPU、内存、磁盘、provider 限流和费用。`sceneRender` 已是当前正式多幕能力，但不承诺某个固定值一定最快；它只改变运行调度和审计，不进入作品 identity。

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
$runtimePython = "D:\SRTWhiteboard\runtime\.venv\Scripts\python.exe"
& $runtimePython -m unittest discover -s tests -p "test_*.py"
```

自动测试通过不代表真实 provider 或人工视觉、听觉验收通过。

测试/CI 报告必须分别标记三类状态：本地 fake/fixture 的自动 `PASS`、真实图片或语音 provider/媒体不可用时的 `SKIP` 或 `BLOCKED`，以及尚未由用户明确确认的人工 Gate（`待确认`）。fixture 的通过不能冒充真实 provider、真实媒体或人工验收通过。

固定场景 benchmark 同样只属于第一类；例如：

```powershell
& $runtimePython benchmarks\run_scene_render_benchmark.py --fixture fixture-medium
```

该命令输出的 `PASS` 只覆盖仓库 fixture 的渲染、技术验证和恢复探针。真实 provider 未执行时必须另报 `SKIP`（外部条件不可用则报 `BLOCKED`），人工 Gate 必须保留为“待确认”且 `approvalWritten=false`。

## 已知限制

- 当前仅支持 Windows，并要求 `D:` 盘工作区和 Microsoft YaHei。
- 只实现软件编码，尚未接入 NVENC、QSV 或 AMF。
- topic/text 只支持 Edge TTS 或 MiniMax，不支持静音模式。
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
