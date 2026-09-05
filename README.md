# InkCue

> 把主题、正文或 SRT 制作成按叙事顺序流式落墨的白板手绘视频。

InkCue（墨序）是一套面向 Codex 的 Windows 本地视频工作流。它把内容草案、字幕分镜、手绘图片、局部揭示、整轨旁白和媒体封装串成一条可恢复的制作链，最终输出 1920×1080、60fps、带烧录字幕的 H.264 MP4。

项目基于 [geeklee/srt-whiteboard-animation](https://github.com/geeklee/srt-whiteboard-animation) 二次开发。可在 [Bilibili](https://www.bilibili.com/video/BV1ik836ME8B/) 查看白板动画演示。

## 能做什么

| 输入 | 处理方式 | 旁白 |
|---|---|---|
| `topic` | 从主题生成自然口播、cue、scene 和分镜 | 自动使用本地激活的语音 provider |
| `text` | 保留原文或润色后生成 cue、scene 和分镜 | 自动使用本地激活的语音 provider |
| `srt` | 直接使用已有严格 SRT | 使用激活的 provider；也可明确制作静音成片 |

非 SRT 输入需要给出 15–600 秒的目标时长。阶段 0 不使用经验性的字符/秒硬门槛，也不据此反复压缩旁白；外部请求前只保留完整 `text_prompt` 不超过 3000 字符、provisional 整轨不超过 120 秒的技术上限，生成后以真实音频 duration 和同次响应的原生字幕为最终证据。

核心能力：

- 画面按字幕事件和局部时间轴逐簇落墨，不会一次显示整张图片。
- 支持 Edge TTS、MiniMax 和豆包 `seed-audio-1.0` 整轨旁白；MiniMax、豆包使用同次响应的原生词级时间证据。
- 支持 OpenAI Images Generations 兼容的图片服务。
- 内置六套 `warm-paper-stream-v1` 视觉模板：暖米黄极简粗线、暖米黄铅笔素描、粗线扁平国风、清新治愈手账、复古报纸拼贴、漫画墨线解释。
- 支持 BGM、局部重做、有界并发、断点恢复、current/stale 检查和 artifact identity 绑定。
- 使用 FFmpeg/libx264 完成场景编码、合并、字幕烧录和音频封装，并检查流结构、帧数、时长与完整解码。

当前六套模板共享 `#F5EBD7` 暖米黄纸张画布；模板选择会冻结到 generation plan，恢复旧项目时不会静默换风格。

## 制作流程

```text
主题 / 正文 / SRT
        ↓
内容草案 + cue/scene + 分镜与制作方案
        ↓
阶段 0：一次联合确认内容、BGM、后续模式和生图方式
        ↓
严格 SRT + generation plan + 整轨旁白与真实时间轴
        ↓
线稿确认 → 标注/reveal 确认 → 场景视频确认
        ↓
合并 → 字幕烧录 → 旁白封装 → 媒体验证
        ↓
output/final.mp4 最终确认
```

`topic` / `text` 会先创建受限的 `pending_initial_approval` 预项目。阶段 0 只准备内容和制作方案，不生成样音、正式图片或视频；通过联合确认后才进入有费用或重媒体的阶段。

项目保留以下质量边界：

- candidate、mock/fixture、技术 `PASS` 和无报错都不等于用户批准。
- 人工模式会在完整旁白、线稿、标注、场景视频和最终成片等关口等待确认。
- 自主模式仍保留 current identity 与全部技术 Gate；它只能记录“阶段 0 授权后的技术推进”，不能声称 AI 已完整听音。
- 视觉 Gate 必须由具备图片或视频查看能力的批准主体检查 current artifact。
- provider 已可能产生费用、但回执或原生字幕证据不完整时，状态为 `unknown_external_outcome`，不会自动重发请求。

完整阶段合同以 [SKILL.md](SKILL.md) 和 [references](references/) 为准，README 不复制逐阶段命令和恢复矩阵。

## 环境要求

- Windows 10/11。
- Python 3.11（推荐）。
- 可写的 `D:` 盘工作区；当前实现要求 `workspaceRoot` 是 `D:` 盘绝对路径。
- FFmpeg、ffprobe、libass/ASS filter 和 libx264。
- `C:\Windows\Fonts\msyh.ttc`（Microsoft YaHei）。
- 图片生成与在线旁白所需的网络和 provider 配置。

当前仅使用软件 x264 编码，尚未接入 NVENC、QSV 或 AMF。

## 快速开始

### 1. 安装

```powershell
Set-Location "$env:USERPROFILE\.codex\skills"
git clone https://github.com/renmengwen/InkCue.git srt-whiteboard-animation
Set-Location .\srt-whiteboard-animation
```

### 2. 创建本地配置

```powershell
Copy-Item config\workspace.example.json config\workspace.local.json
Copy-Item config\image-providers.example.json config\image-providers.local.json
Copy-Item config\voice-providers.example.json config\voice-providers.local.json
```

至少需要在 `workspace.local.json` 中设置 `workspaceRoot`。图片和语音配置通过各自的 `activeProvider` 选择当前 provider；正常 `topic` / `text` 流程会自动读取并冻结选择，不会要求用户在 Edge、MiniMax 和豆包之间临时选择。

API Key 只能写入被 `.gitignore` 排除的 `config/*.local.json`，不要提交、打印或复制到项目产物。豆包固定使用 prompt-only `text_prompt` 控制音色与表演，不配置或发送 `speaker`、`references`、`audio_data` 或 `audio_url`。

### 3. 在 Codex 中调用

从主题开始：

```text
使用 $srt-whiteboard-animation，以“为什么习惯很难改变”为主题，
制作约 60 秒的白板手绘视频，并在每个人工关卡等待我确认。
```

从正文开始：

```text
使用 $srt-whiteboard-animation，把下面这段正文润色为约 90 秒的自然口播，
再制作成白板手绘视频：……
```

从 SRT 开始并保持静音：

```text
使用 $srt-whiteboard-animation，把 examples/一分钟理解习惯回路.srt
制作成无旁白白板动画，并在每个人工关卡等待我确认。
```

Codex 会读取 [SKILL.md](SKILL.md)，自动准备工作区、冻结 provider 与视觉模板，并在需要选择时给出可直接回复的完整选项。若选择自主推进，也必须先完成阶段 0 联合确认。

## 诊断与恢复

完整生产流程建议由 Codex 编排。以下命令只用于传统 SRT、兼容路径或诊断；正常 `topic` / `text` 新任务由 Skill 使用单次 bootstrap，不需要先手动执行这些预检。

```powershell
# 检查目标工作区是否真的可创建、读写和删除探针文件
python scripts\prepare_env.py --check-workspace-access

# 检查专用环境；仅在提示环境或依赖未准备时去掉 --check 再运行
python scripts\prepare_env.py --check
python scripts\prepare_env.py
```

环境准备完成后，命令末行会给出 `ENV_PY=<绝对路径>`。后续业务脚本应直接使用这个解释器，不要先用系统 Python 试跑。

常用的确定性入口只有三个：

```powershell
& <ENV_PY> scripts\coordinator_cli.py project-status --project <项目根目录>
& <ENV_PY> scripts\run_phase.py --project <项目根目录> --phase annotation-preview
& <ENV_PY> scripts\run_phase.py --project <项目根目录> --phase final-delivery
```

两个 runner 都会在预期质量 Gate 停止，并明确返回 `approvalWritten=false`；进程退出码 0 只表示确定性步骤完成，不授权自动批准下游。

## 配置与目录

主要配置：

- [`workspace.example.json`](config/workspace.example.json)：工作区、编码和安全的全 `1` 并发基线。
- [`workspace.performance.example.json`](config/workspace.performance.example.json)：测量后调优的示例，不是默认值。
- [`image-providers.example.json`](config/image-providers.example.json)：图片 provider 模板。
- [`voice-providers.example.json`](config/voice-providers.example.json)：Edge TTS、MiniMax 和豆包语音模板。

仓库结构：

```text
SKILL.md       # Codex 入口路由与不可变边界
references/    # 各阶段的完整合同
scripts/       # 确定性 CLI、provider 适配和媒体处理
agents/        # 短上下文角色合同
assets/        # 手部素材、BGM、字体与视觉模板预览
config/        # 可提交的 example 配置；local 配置被忽略
examples/      # SRT、generation plan 与 annotation 示例
tests/         # fast mock/fake、纯逻辑和静态合同测试
benchmarks/    # fixture 性能测量，不代表质量验收
```

一个正式项目通常包含：

```text
<项目根目录>/
├─ project.json
├─ source/source.srt
├─ planning/       # generation、timing、voice plan
├─ scenes/         # 图片、annotation、单幕视频
├─ audio/          # 整轨旁白、narration SRT、timeline
├─ previews/
├─ reviews/
├─ manifests/
└─ output/
   ├─ final-video-only.mp4
   ├─ final-subtitled-video-only.mp4
   └─ final.mp4
```

## 已知限制

- 当前只支持 Windows、`D:` 盘工作区和固定 Microsoft YaHei 字体。
- `topic` / `text` 需要激活 Edge TTS、MiniMax 或豆包语音；静音模式只用于传统 SRT。
- 图片 provider 当前只支持 OpenAI Images Generations 兼容协议。
- 六套视觉模板目前共用 `warm-paper-stream-v1` renderer，不包含暗黑、赛博、3D 或独立 Remotion 分支。
- annotation 与视觉质量判断仍需要具备图片理解能力的 Agent 或人工处理。
- 自动化测试只验证本地 mock/fake/fixture 合同，不代表真实 provider、真实媒体或主观质量通过。

## 文档与开发

- [阶段 0：内容与联合确认](references/phase-0-content.md)
- [内容输入与严格 SRT](references/content-input.md)
- [图片生成与视觉模板](references/image-generation.md)
- [语音与真实时间轴](references/voiceover.md)
- [字幕与最终媒体](references/subtitles.md)
- [Agent 编排](references/subagent-orchestration.md)
- [恢复、identity 与 stale](references/recovery-and-identity.md)
- [Phase 4 runner](references/phase-4-runner.md)

参与开发前请先阅读 [AGENTS.md](AGENTS.md)。当前协作规则要求最小闭环、禁止擅自使用 `git worktree`，并禁止在开发过程中新增或运行测试。不要提交 `config/*.local.json`、API Key、真实用户内容、临时 provider URL 或大体积成片。

## 交流与许可

作者微信：`Remove147`

项目使用 [MIT License](LICENSE)。二次发布或分发时，请保留上游项目的版权与许可声明。
