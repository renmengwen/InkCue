# 可配置并发与全流程性能优化开发计划

> **2026-08-17 更新：** 本计划中的 narration review MP4 生成、深验和联合预审关卡已由当前合同移除；语音并发仍适用于 segment WAV，timeline/SRT/full identity 与批准仍串行。权威流程以 `SKILL.md` 和 `references/voiceover.md` 为准。

> 日期：2026-08-15  
> 状态：待实施  
> 适用 Skill：`srt-whiteboard-animation`  
> 工作目录：`C:\Users\MOVER\.codex\skills\srt-whiteboard-animation`  
> 正式项目根目录：由 `config/workspace.local.json` 的 `workspaceRoot` 决定，当前为 `D:\SRTWhiteboard`  
> 基线说明：当前 Skill 目录不是 Git 仓库；本计划只冻结开发合同与实施顺序，不代表任何业务代码已经落地。实施时必须逐阶段保留可回退边界，不能依赖 branch、commit 或 worktree 隔离。  
> 核心目标：从内容草案、SRT、准备包、建项和环境预检开始，直到图片、TTS、批量校验、区域预览、正式渲染和最终媒体交付，按真实耗时选择“减少反馈轮次、批量预检、去重、证据复用、artifact-first subagent 或 JSON 驱动的有界 worker 并发”；缺失配置或并发数为 `1` 时维持现有串行语义；主窗口只保留全局状态、结构化结果和用户决策，不回灌长 SRT、逐幕推理或工具日志；全过程不绕过现有人工关卡、identity、stale、恢复和正式媒体合同。

## 1. 计划结论

本轮讨论冻结以下产品与技术决定：

1. 并发数量的权威配置放在 `config/workspace.local.json`，而不是要求用户每次在 CLI 传 `--concurrency`。
2. `execution.agents` 与 `execution.concurrency` 各自采用“本资源池 default + role/stage 覆盖”；两个 `default` 缺失时都内置为 `1`，不得跨资源池继承，最终值为 `1` 时对应 agent/worker 严格串行。
3. 并发是本机执行策略，不进入项目内容、图片、音频、时序、标注、渲染、字幕或 final identity，也不触发业务 stale。
4. 每次运行必须把 `configuredConcurrency`、`effectiveConcurrency` 和任务数写入不含秘密的运行摘要或 manifest run 审计记录，以便诊断限流和资源争抢。
5. worker 只处理相互独立的外部请求、文件验证、预览或候选产物；manifest、时间线、SRT、合并顺序、identity 和批准记录始终由一个协调器串行提交。
6. 图片与 TTS 生成可以有界并发；图片继续保持“单幕失败不阻止其他幕”，TTS 则使用滚动窗口，在首个失败后停止派发新 unit，只允许已经在途的 unit 安全收尾并 checkpoint。
7. 截图所示的逐幕 annotation 校验首先要消除重复：完整 Edge 旁白、audio/timeline、review 和批准证据在一次批量运行中只深度验证一次，不能把同一套全局媒体校验简单复制到多个线程。
8. 区域预览改成批量命令：全局证据校验一次、每幕独立预览并发生成、正式预览原子发布，并额外生成一张全局 contact sheet 供快速审阅。
9. 正式单幕渲染优先消除 OpenCV MP4V 中间编码和发布后的第二次深度校验；直接把 BGR 帧流式送入 FFmpeg/libx264，候选深度验证一次，原子发布后只核对 SHA/bytes。
10. 合并、字幕烧录、mux 和最终验证应区分“新产物深度验证”与“已验证上游的快速 binding 检查”，不在每个下游阶段重复完整解码同一字节。
11. 当前“每幕完成后停止并等待用户确认”的人工关卡默认保持不变。多幕正式候选并发渲染属于单独产品决策；在合同未明确更新前，`sceneRender` 必须保持 `1`。
12. 真实图片供应商、真实 Edge TTS、真实 1080p/60fps 项目和人工视觉/声音判断必须单列验收；fixture PASS 不能冒充真实外部或人工 PASS。
13. 前半程增加无写入 content draft 校验和批量环境依赖探测；SRT、准备包和建项保持严格串行，因为实测只有毫秒级，缓存或并发不划算。
14. 图片消费校验改为每张 PNG 单次完整解码；narration review 的新候选只深验一次，后续普通 validate/approve 在 current SHA/bytes 和验证器合同完全匹配时复用持久化证据。
15. 主 agent 是唯一 coordinator 和用户接口；subagent 只处理上下文重、需要语义或视觉判断、能输出独立候选工件的任务，不替代普通脚本、worker、validator 或批准命令。
16. subagent 首版 role allowlist 只有传统 SRT 的 `storyboardPlanning`、线稿只读 `visualReview` 和逐幕候选 `annotationDrafting`；但 role allowlist 不等于允许实际派发。只有 runtime 提供可验证的文件系统/tool 写入隔离时才能 dispatch；当前共享文件系统 runtime 必须 fail-closed，三个 role 都由 coordinator 按相同 task/candidate validator 合同串行 fallback。
17. subagent 通过 workspace draft scope（正式建项前）或项目 `.work` scope（正式建项后）的 `task.json` 与 `result.json` 交接；prompt 不粘贴完整 SRT、提示词/路径数组、主对话或长日志，自然语言“完成”不能作为成功证据。
18. agent concurrency 单独配置在 `execution.agents`，默认 `1`，与 `execution.concurrency` 的脚本 worker 资源池分开；effective agent 数受 runtime child slot、ready task 数和 coordinator 资源预算共同限制，禁止并发乘法。
19. 在通过强隔离 Gate 的 runtime 中，subagent 产生的只是不含批准的 candidate。coordinator 必须重验 task/result schema、输入 SHA/current binding、候选 SHA 和业务 validator，之后才可按正式顺序原子发布；agent 完成顺序不改变 scene、timeline、SRT、manifest 或 identity 顺序。`allowedOutputs`、pre/post hash inventory 和 prompt 禁令只能做合同校验/侦测，不能冒充操作系统权限隔离。
20. 本计划阶段先不修改当前 `SKILL.md` 或业务代码；实施时必须先落地 agent 配置、task/result validator、路径保护和测试，Phase Gate 通过后才把编排入口接入 Skill，避免形成无运行支撑的纸面能力。
21. “并发不进入 identity”只表示调度参数和完成顺序不是作品身份输入；新调用模型生成的 storyboard/annotation candidate 允许字节不同，正式发布后仍由实际候选字节及现有业务合同计算 identity。只有复用同一组冻结 task/result/candidate 做串行与并发回放时，才要求得到相同正式 identity。
22. `storyboardPlanning` 必须直接产出最终 generation-plan scene schema：使用非空 `prompt` 和 `cueRange`，不能把 content draft 的 `imagePrompt` 或 timing plan 的 `sourceCueRange` 原样写进正式 generation plan；正式 validator 同步收紧为每个非空 scene 必须有非空 `prompt`。
23. 现有线稿、标注、区域预览、最终时序、单幕和 clean master 继续是 coordinator 负责的聊天人工关卡；普通 CLI 只强制技术 current/validated，不声称能读取聊天批准。当前版本 `sceneRender` 无条件限制为 `1`，Phase 8 仅保留为未来合同设计，不列入本轮实施。
24. 外部请求 worker 只写已登记 attempt candidate，绝不直接写正式 PNG/WAV；coordinator 采用“预登记 attempt → candidate receipt → 深验 → 保留 candidate 的原子发布 → validated checkpoint”协议，并对模糊外部结果 fail-closed，不能自动重复付费请求。
25. 新正式媒体的精确帧数必须来自同一次完整解码的 `decodedFrameCount`；`nb_frames` 只用于快速预检或已深验字节的 binding，不能单独证明累计帧合同。
26. 人工确认前的 content draft 校验使用 `--stdin` 或进程内纯函数，不能为了调用只读 CLI 先写未确认草案文件；零写入通过调用前后文件树快照证明。
27. 非 Git 目录的 SHA 清单只用于检测，不能称为可回退。实施前及每个 Phase 都必须在 Skill 外建立带原字节、pre/post manifest 和安全恢复检查的阶段快照；初始化 Git/基线提交仍需用户另行授权。
28. 每个 agent attempt 冻结一份 `role-contract.md`，task/result 同时绑定 `roleContractVersion` 与 `roleContractSha256`；subagent 不读取执行中可能变化的 live reference。

## 2. 当前实现事实与性能瓶颈

### 2.1 前半程复核与本机耗时证据

此前标题使用“后半程”是因为最初性能讨论集中在 annotation 校验、区域预览、正式渲染和后续媒体检查；但现有计划实际上已经包含 TTS 与图片生成。按 Skill 的阶段 0–5 重新检查后，标题应改为“全流程”，同时必须避免把本来只有毫秒级的确定性步骤也机械并发化。

2026-08-15 在本机现有项目上的只读快照如下。它们只用于判断优化优先级，不是跨机器 SLA，也不表示真实 provider、媒体或人工验收已经重新执行：

| 路径 | 样本 | 实测 | 结论 |
|---|---:|---:|---|
| SRT 严格解析 + 分组 | 33 cue 项目，循环 1000 次 | `0.121s` | 单次约 `0.12ms`，不并发 |
| source 准备包完整重建校验 | 8 幕 draft，循环 20 次 | `0.086s` | 单次约 `4.3ms`，保留信任边界校验 |
| 正式项目完整 `load_project()` | 8 幕 Edge 项目，循环 20 次 | `0.262s` | 单次约 `13.1ms`，不引入缓存失效复杂度 |
| Edge 环境依赖检查 | 基础依赖 + `edge-tts` | `1.265s` | 可把多个 Python 子进程合为一次批量探测，低风险小收益 |
| 图片消费验证 | 8 张、合计 `21.15MB` PNG | `0.529s` | 可并发，并去掉同一 PNG 的 `verify + load` 双重解码 |
| 真实 Edge `full` | 10 个 unit | `40.468s` | 外部请求是主要等待点 |
| 真实 Edge `full` | 33 个 unit | `169.635s` | 最后 unit 在 `155.445s` 完成；合并、timeline、review 与收尾再用 `14.190s` |

因此前半程按三档处理：

1. **高优先级：** TTS unit 有界并发、图片生成有界并发、图片校验单次解码、narration review 深度证据复用；
2. **中低优先级：** 内容草案在人工确认前增加无写入校验，减少“确认后才发现 schema/时长错误”的往返；环境依赖改为一次批量子进程探测；
3. **明确不优化：** SRT 解析、provisional 排时、source 包重建校验、建项和 `load_project()` 继续串行。它们承担信任边界和确定性绑定，当前耗时不值得换取缓存或并发复杂度。

阶段 0 的 Codex 内容创作和用户确认也不纳入 JSON concurrency：当前 Skill 不自行调用文本模型，旁白、cue、scene 和跨幕视觉一致性存在语义依赖，人工等待更不能由线程池绕过。这里的优化方式是一次生成完整结构、确认前做只读确定性校验、修改时只展示受影响差异，而不是并发生成互相可能漂移的草案片段。

### 2.2 工作区配置尚未支持并发

当前 `config/workspace.local.json` 只有：

```json
{
  "schemaVersion": 1,
  "workspaceRoot": "D:\\SRTWhiteboard"
}
```

`scripts/project_workspace.py::load_workspace_config()` 当前只读取和校验 `schemaVersion` 与 `workspaceRoot`。即使用户手工添加并发字段，现有脚本也不会生效。

并发不应统一放入：

- `project.json`：并发不是作品合同，不应导致项目升级或 stale；
- `image-providers.local.json`：它只能表达图片供应商特性，无法覆盖 TTS、预览、渲染和媒体校验；
- `voice-providers.example.json`：当前只是示例合同，正式语音 CLI 并未读取，而且也无法覆盖非语音阶段。

### 2.3 图片生成严格串行

`scripts/generate_images.py` 当前按 generation plan 顺序逐幕执行：

```text
requesting
  → provider request/retry
  → decoding
  → normalizing
  → validated/failed
  → manifest.save()
```

每幕目标文件和临时文件以唯一 `sceneId` 隔离，天然适合有界并发；但是 `ManifestStore` 是共享可变字典和原子 JSON 文件，不允许多个 worker 无锁写入。

### 2.4 完整旁白严格串行

`scripts/generate_voiceover.py::_full()` 当前逐个 speech unit 请求 Edge、规范化 WAV、校验并 checkpoint。每个 unit 有独立 `voiceSynthesisIdentityHash` 和正式 `audio/segments/unit-xxxx.wav`，可以并发；最终 WAV 合并、timeline、narration SRT、review 和 full identity 有严格顺序依赖，必须串行。

`scripts/edge_tts_adapter.py` 已用锁保护请求启动间隔，适合多个线程共享同一个 adapter：请求之间仍遵守 queue interval，但长请求可以重叠等待。

### 2.5 annotation 批量校验重复全局深度验证

当前临时批量脚本 `tmp/validate_current_annotations.py` 对每一幕调用 `resolve_formal_scene()`。Edge 项目中每次调用都会重新进入：

```text
validate_current_voiceover(require_full=True)
  → 逐个验证 segment WAV
  → 验证 narration.wav
  → 验证 timeline / narration.srt
  → 验证 full identity / approval
  → validate_narration_review()
      → ffprobe review MP4
      → FFmpeg full decode review MP4
```

8 幕会把同一套项目级证据深度验证 8 次。这里的首要优化是批量运行内去重，而不是直接开 8 个线程同时解码同一文件。

### 2.6 区域预览一次只处理一幕

`scripts/render_annotation_preview.py` 当前每次启动一个 Python 进程，重新加载 Pillow、字体、原图和 annotation，再直接写目标 PNG。批量 8 幕会重复进程与字体初始化，并串行进行 PNG 压缩。

现有代码还存在两个小问题：

- 28px `font` 被加载但未使用；
- 对 PNG 传 `quality=95` 没有实际意义，未利用低压缩级别换取更快的无损预览保存。

### 2.7 正式单幕存在双重编码和双重深度校验

`scripts/render_stream_whiteboard.py` 当前：

```text
OpenCV 逐帧生成
  → cv2.VideoWriter(mp4v) 写完整 raw MP4
  → FFmpeg 重新读取和解码 raw MP4
  → libx264 medium/CRF18 编码 H.264 candidate
  → validate_video(candidate)
  → full_decode(candidate)
  → os.replace 原子发布
  → validate_video(正式文件)
  → full_decode(正式文件)
```

同一场景被编码两次，正式发布前后相同字节又被深度验证两次。

### 2.8 通用媒体校验可能重复完整扫描

`scripts/media_validation.py::probe_media()` 固定使用 `ffprobe -count_frames`，多数调用方随后又执行 `full_decode()`，同时还要计算 SHA-256：

```text
ffprobe -count_frames 读取/统计全片
FFmpeg full_decode 再完整解码全片
SHA-256 再读取全部字节
```

优化目标不是直接信任容器 `nb_frames`，而是把精确帧统计合并进原本就必须执行的 full decode：一次完整解码同时得到 `decodedFrameCount` 和解码 PASS，再让相同 SHA/bytes 的下游 binding 复用该 receipt。这样可以删除独立 `-count_frames` 扫描，又不削弱累计帧合同。

### 2.9 合并、字幕、mux 和 final 存在可去重或并发的验证

- `merge_scenes.py` 合并前逐幕串行 `validate_video + full_decode`；真正 concat 已优先 `-c:v copy`。
- `burn_subtitles.py` 对 clean master 先 `probe_media()`，随后 `validate_video()` 内部再次 probe；烧录本身使用 libx264 `medium/CRF18`，必然整片重编码。
- `mux_voiceover.py` 在 `-c:v copy` 封装前又深度验证 clean 与 captioned 两份已经验证过的上游视频。
- `validate_final_media.py` 串行深度验证 clean、captioned、final 三份相互独立的正式输出。
- 最终字幕 contact sheet 对每个采样时间单独启动一次 FFmpeg；收益较小，但可以在主链优化后再并发截帧。

### 2.10 主窗口上下文与 Subagent 编排缺口

当前 Skill 已把确定性合同拆进 `references/` 和脚本，但权威工作流仍默认由主窗口完成全部语义分镜、逐图初检、逐幕 annotation 构思、命令输出阅读和用户汇报。幕数增多后，主窗口容易累积完整 SRT、全部图片细节、逐幕坐标推理、长工具日志和重复校验说明。

外部 `YangAgent/whiteboard-animation-skill@36798ab` 的可取部分是：主 agent 明确作为 coordinator，subagent 只读专项 reference，并以工件路径和小摘要返回；不可照搬的部分是：阶段仍串行等待、把完整 prompt/路径数组粘进 subagent、用目录时间戳扫描收集结果、固定图片并发 `10`、让 subagent 只包一层脚本，以及缺少 manifest/recovery/identity/人工关卡。

本计划只吸收前者，并按现有严格流程重构：

| 当前阶段 | 是否使用 subagent | 原因与边界 |
|---|---|---|
| topic/text 内容草案 | 否 | 旁白、cue、scene 与用户修改高度耦合，继续由当前对话完整生成和展示 |
| 传统 SRT 语义分镜 | 条件式单个 `storyboardPlanning`；当前 runtime fallback | 隔离长 SRT/分镜推理；只输出候选，不建项、不批准；强隔离 Gate 未过不派发 |
| 图片 provider 生成/验证 | 否 | 用脚本 bounded worker，避免 agent 乘法和付费 retry 风险 |
| 多幕线稿初检 | 条件式单个 global `visualReview`；当前 runtime fallback | 隔离多图视觉上下文；只返回 findings，不替代用户逐图确认；强隔离 Gate 未过不派发 |
| 完整旁白/TTS | 否 | provider、checkpoint、timeline/SRT 和 approval 必须由确定性代码/coordinator 控制 |
| 逐幕 annotation 候选 | 条件式 `annotationDrafting` 有界并发；当前 runtime fallback | scene 之间独立且上下文重；只有隔离 runtime 才派发，只写 `.work` candidate，coordinator 验证/发布 |
| annotation 校验/预览 | 否 | 由 Phase 4/5 的批量脚本去重和并发 |
| 正式渲染/合并/字幕/mux/final | 否 | 媒体严格性、顺序和人工关卡由脚本/coordinator 保持 |

subagent 的第一收益是上下文隔离；只有强隔离 Gate 通过且 `annotationDrafting` 有多个 ready scene 时才宣称 wall-time 并行收益。`storyboardPlanning` 和 global `visualReview` 通常只有一个任务，即使配置大于 `1`，隔离 runtime 的 effective 也最多为 `1`；当前共享文件系统 runtime 三个 role 的 effective 都是 `0`，由 coordinator fallback。

global `visualReview` 也不能无限吞入图片：派发前必须按 runtime 声明的图片数量、单图字节/像素和上下文能力做 preflight。首版只在单个 global task 可安全容纳全部 current scene 时启用；超限或 runtime 无法给出可信能力边界时，不把它静默拆成会丢失跨幕一致性的独立 reviewer，也不谎报完成，而是由 coordinator 按同一检查表串行查看并报告 fallback。若 coordinator 同样缺少真实图片查看能力，则报告 `BLOCKED`。大项目的分片 + 全局汇总需要单独设计跨分片一致性证据，不在首版伪装成已解决。

计划中的 agent 工件和 prompt 采用 artifact-first：主窗口给 subagent 的普通消息只含 attempt 冻结 `role-contract.md` 的绝对路径、`task.json` 的绝对路径、对应 SHA 和固定返回格式，不引用 live Skill reference；业务内容、当前 SHA/identity、允许输出和写权限全部冻结在 task 文件。subagent 的逐步推理、工具日志和完整 findings 留在 task 目录，主窗口只读取 `result.json`、validator 结果、最高优先级 findings 和需要用户决定的差异。

## 3. JSON 权威配置合同

### 3.1 配置位置与结构

权威文件：

```text
config/workspace.local.json
```

建议完整配置：

```json
{
  "schemaVersion": 1,
  "workspaceRoot": "D:\\SRTWhiteboard",
  "execution": {
    "agents": {
      "default": 1,
      "storyboardPlanning": 1,
      "visualReview": 1,
      "annotationDrafting": 1
    },
    "concurrency": {
      "default": 1,
      "imageGeneration": 4,
      "voiceGeneration": 4,
      "imageValidation": 4,
      "voiceValidation": 4,
      "annotationValidation": 4,
      "annotationPreview": 4,
      "sceneRender": 1,
      "sceneMediaValidation": 2,
      "finalMediaValidation": 2
    }
  }
}
```

首版 `config/workspace.example.json` 必须展示完整结构；`config/workspace.local.json` 可以只显式写 `execution.agents.default: 1` 与 `execution.concurrency.default: 1`，避免未经用户测量就改变本机 agent slot、外部请求和本地负载。`agents.default: 1` 是 configured fallback，不会绕过强隔离 Gate；当前共享文件系统 runtime 的 actual dispatch 仍为 0。文档另提供上面的建议起步值。

### 3.2 字段语义

| 字段 | 任务 | 缺省 |
|---|---|---:|
| `default` | 未单独配置的并发阶段 | `1` |
| `imageGeneration` | 多幕图片请求、解码与归一化 | `default` |
| `voiceGeneration` | Edge speech unit 请求与规范化 | `default` |
| `imageValidation` | 多幕 PNG 解码、尺寸与 SHA 校验 | `default` |
| `voiceValidation` | segment WAV 深度验证 | `default` |
| `annotationValidation` | 多幕 annotation/timing/binding 校验 | `default` |
| `annotationPreview` | 多幕区域预览 PNG 生成 | `default` |
| `sceneRender` | 多幕正式候选渲染 | `default`；现合同下强制为 `1` |
| `sceneMediaValidation` | 合并前独立场景 MP4 验证 | `default` |
| `finalMediaValidation` | clean/captioned/final 深度验证 | `default` |

agent 并发与 worker 并发分开解释：

| `execution.agents` 字段 | 任务 | 缺省/上限语义 |
|---|---|---|
| `default` | 未单独配置的 subagent batch | `1` |
| `storyboardPlanning` | 传统 SRT 语义分镜候选 | `default`；强隔离 Gate 通过后 ready task 仍只有 1，因此 effective 最大为 1；当前 runtime 为 0 |
| `visualReview` | 多幕线稿全局只读初检 | `default`；强隔离 Gate 通过后首版使用一个 global reviewer，effective 最大为 1；当前 runtime 为 0 |
| `annotationDrafting` | 独立 scene 的候选 annotation | `default`；先受强隔离 Gate，再受 ready scene 和 runtime child slot 限制；当前 runtime 为 0 |

`execution.agents` 只控制同时在途的 subagent 数，不控制模型内部并行、脚本线程、provider 请求或 FFmpeg 进程。coordinator 必须保留自己的执行槽；JSON 值只是 configured 上限，不保证 runtime 能提供同等 child slot。这里的 `runtimeChildSlots` 统一表示“保留 coordinator 后还能同时运行的子 agent 数”；若 runtime API 返回的是包含 coordinator 的总 slot，适配层必须先安全减 1，不能在 scheduler 内再次扣减或重复预留。

### 3.3 校验规则

- `execution` 缺失：合法，所有阶段为 `1`；
- `execution.agents` 缺失：合法，所有 agent role 为 `1`；
- `execution.concurrency` 缺失：合法，所有阶段为 `1`；
- 两个配置对象各自的 `default` 缺失：内置为 `1`；
- agent role 或 worker stage 字段缺失：只回退本对象的 `default`，不得跨对象继承；
- 每个值必须是 `1–16` 的整数；
- `sceneRender` 是当前合同的显式例外：只能为 `1`，大于 `1` 必须配置失败；未来多幕候选并发需要新的 feature/contract 版本，不能从聊天批准状态推断；
- Python `bool` 必须显式拒绝，不能因其是 `int` 子类而接受；
- `0`、负数、浮点数、字符串、NaN、Infinity 和超过 `16` 都必须失败；
- `execution.agents` 与 `execution.concurrency` 内未知字段都必须失败，防止拼写错误静默回退；
- 该新增字段是 schema v1 的向后兼容可选扩展，不要求旧本地配置升级；
- 配置错误按参数/配置错误处理，禁止静默回退到更高并发或默认工作区。

### 3.4 解析 API

扩展 `scripts/project_workspace.py`：

```python
@dataclass(frozen=True)
class ExecutionConcurrency:
    default: int = 1
    image_generation: int | None = None
    voice_generation: int | None = None
    image_validation: int | None = None
    voice_validation: int | None = None
    annotation_validation: int | None = None
    annotation_preview: int | None = None
    scene_render: int | None = None
    scene_media_validation: int | None = None
    final_media_validation: int | None = None

    def for_stage(self, stage: str) -> int:
        ...

@dataclass(frozen=True)
class ExecutionAgentConcurrency:
    default: int = 1
    storyboard_planning: int | None = None
    visual_review: int | None = None
    annotation_drafting: int | None = None

    def for_role(self, role: str) -> int:
        ...
```

`WorkspaceConfig` 新增 `concurrency` 与 `agents`。所有 CLI 通过同一解析函数获取 worker stage；coordinator 通过同一 loader 获取 agent role，不允许脚本、SKILL 或 subagent 各自重新解释 JSON。

首版不要求公开 `--concurrency`；JSON 是正常使用的单一权威来源。测试可以注入 `WorkspaceConfig` 或临时配置文件。若未来增加 CLI 临时覆盖，必须另行更新合同并明确 `CLI > 阶段配置 > default > 1`，本计划首版不实现该双来源。

### 3.5 审计与 identity

worker/agent 并发参数、调度顺序和 agent task contract 不进入任何正式作品 identity；但 provider/model/subagent 实际产生并被 coordinator 接受的图片、音频、plan 或 annotation 字节，仍按现有合同进入相应内容/媒体 identity。每次运行的摘要或非正式 run 审计按任务类型记录其子集：

```json
{
  "stage": "annotationPreview",
  "configuredConcurrency": 4,
  "effectiveConcurrency": 4,
  "taskCount": 8,
  "startedAt": "...",
  "finishedAt": "..."
}
```

agent batch 示例：

```json
{
  "stage": "annotationDrafting",
  "configuredAgentConcurrency": 3,
  "effectiveAgentConcurrency": 2,
  "dispatchAllowed": true,
  "isolationGate": "enforced",
  "taskCount": 8,
  "completedCount": 7,
  "failedCount": 1,
  "cancelledCount": 0,
  "startedAt": "...",
  "finishedAt": "..."
}
```

只有强隔离 Gate 通过时，`effectiveAgentConcurrency` 才取 configured、ready task、`runtimeChildSlots` 和 coordinator resource budget 的最小值。runtime 在保留 coordinator 后只能提供 2 个 child slot 时，配置为 3 必须如实记录 configured=3/effective=2。Gate 未通过时统一记录 `dispatchAllowed=false`、`effectiveAgentConcurrency=0` 和非敏感原因，再执行 coordinator fallback；不能声称 configured 已生效，也不能擅自提高 runtime 配置。总 slot 与 child slot 的转换只允许发生一次，并纳入单元测试，避免 off-by-one。

禁止保存线程 ID、PID、绝对临时路径、完整主对话、subagent 隐藏推理、API Key、Cookie、Token 或供应商完整响应。agent role、task/result contract version 和计数可以进入 run 审计；候选正式发布后仍由候选业务字节与现有 identity 合同决定 current 身份。

## 4. 共享 Worker 与 Subagent 编排合同

### 4.1 Worker bounded executor

新增建议文件：

```text
scripts/bounded_execution.py
tests/test_bounded_execution.py
```

职责：

1. 接受计划顺序任务列表和 `max_workers`；
2. `max_workers=1` 时使用显式普通循环，不创建线程池，确保默认路径与现有串行行为一致；
3. `max_workers>1` 时最多保持 `effectiveConcurrency` 个在途任务，不一次性向无限队列提交全部任务；
4. worker 返回结构化结果或阶段事件，不直接写共享 manifest；
5. 协调器串行消费事件、保存 checkpoint，并按原计划顺序输出最终摘要；
6. 支持两种失败策略：
   - `continue_independent`：图片、图片验证、annotation、预览等独立批处理继续处理其他任务；
   - `stop_dispatch`：TTS 首个失败后停止派发新任务，已在途任务可安全完成；
7. 取消时停止新任务，尝试取消尚未开始的 future，不粗暴删除已经验证产物；
8. worker 异常必须映射为原有稳定错误类别，不能泄漏秘密；
9. 主线程发生 manifest/文件系统致命错误时停止派发，保留已经原子发布且有完整证据的产物；
10. 任务结果的展示顺序永远是计划顺序，而不是完成顺序。

测试不使用固定 `sleep` 猜测并发；使用 `threading.Event`、`Barrier` 或受控 executor 证明峰值在途数量。

#### 4.1.1 外部请求 candidate、提交与崩溃恢复协议

图片和 TTS 属于付费或有外部副作用的请求。现有 `normalize_and_store_image()` 与 `normalize_audio_bytes()` 会在函数返回前直接发布正式 PNG/WAV，不能原样放进并发 worker，否则会出现“正式文件已写、manifest 仍是 requesting”的不可恢复窗口。并发改造必须先把底层 API 拆成 candidate 模式：worker 的 destination 只能是 coordinator 预登记的 attempt candidate，正式路径只能由 coordinator 发布。

每个外部 task 的 manifest attempt 至少包含：

```json
{
  "attemptId": "scene-03-attempt-0001",
  "status": "prepared",
  "inputIdentitySha256": "<image-or-synthesis-identity>",
  "candidateFile": ".work/<run-id>/external-tasks/scene-03/attempt-0001/candidate.png",
  "candidateSha256": null,
  "candidateBytes": null,
  "validatorReceipt": null,
  "formalFile": "scenes/scene-03-example.png",
  "externalOutcome": "not_started"
}
```

提交顺序固定为：

```text
coordinator 校验正式目标/overwrite 策略并持久化 prepared attempt
  → coordinator 写 requesting（含确定性 candidate 路径和 input identity）
  → worker 调 provider，只原子写 attempt candidate，不写正式路径/manifest
  → worker 返回 candidate SHA/bytes/identity 与去敏 provider receipt
  → coordinator 重验 input/current binding、candidate 和 validator receipt
  → coordinator 写 candidate_ready checkpoint
  → coordinator 写 publishing checkpoint
  → 从 candidate 复制到正式目标同目录临时文件，fsync 后 os.replace；candidate 暂不删除
  → coordinator 核对正式 SHA/bytes，写 validated checkpoint
  → validated 持久化成功后才允许清理 candidate
```

manifest 与正式文件不是跨文件事务，因此恢复必须按已登记状态和确定路径处理，禁止扫描目录猜测结果：

- `prepared/not_started`：没有外部调用，可以安全重新派发；
- `requesting` 且 candidate 已存在：验证 candidate identity、SHA、bytes 和 receipt；完全匹配时提升为 `candidate_ready`，provider 调用数保持 0；
- `requesting` 且 candidate 不存在：若 provider 支持同一 idempotency key 查询/恢复，则只查询或用同 key 继续；否则标记 `unknown_external_outcome` 并停止自动 retry，等待用户决定，不能为追求成功静默再次付费；
- `candidate_ready`：不调用 provider，继续正式发布；
- `publishing`：若正式文件与 candidate SHA/bytes 相同，补写 validated；若正式文件不存在则从 candidate 重放发布；若正式文件不同则停止并报告冲突；
- `validated`：正式 SHA/bytes/current identity 匹配时直接复用，不调用 provider；
- `failed/cancelled`：只有外部结果明确失败且不存在可采用 candidate 时，才允许按原 retry 合同创建新 attempt。

这套协议提供的是“不会自动重复不确定结果”，不是无法实现的跨 provider exactly-once。特别是 provider 已收费但进程在 candidate 落盘前崩溃、且 provider 又不支持幂等查询时，只能进入 `unknown_external_outcome`；需要用户明确授权才可新建付费 attempt。

崩溃注入测试必须覆盖：prepared 后、requesting 后、provider 返回后但 candidate 前、candidate 原子落盘后、candidate_ready 后、正式发布后、validated manifest 前、validated 后清理前。每个边界都验证正式旧文件不被错误覆盖、manifest 不停留在假 running、可采用 candidate 时 provider 调用数为 0、结果不确定时不会自动重复请求。

### 4.2 Coordinator 与 Subagent 权限

主 agent 是唯一 coordinator，也是唯一用户接口。只有 coordinator 可以：

- 决定当前阶段和展示用户可见结果；
- 接收用户明确确认或修改意见；
- 冻结 source/generation/timing/audio/render/image SHA 与 current identity；
- 创建 agent task、计算 effective agent concurrency、派发/取消/retry；
- 验证 result、候选 schema/SHA/current binding；
- 原子发布正式候选；
- 串行写 manifest、timeline、SRT、identity、stale、checkpoint 和批准；
- 报告 PASS/FAIL/BLOCKED/SKIP/待确认边界。

subagent 只能按 allowlisted role 生成候选工件或只读 findings。首版所有 task 必须包含 `formalWritesAllowed: false` 和 `approvalWritesAllowed: false`；任一值缺失或不为 `false` 都拒绝派发。subagent 完成、所有 task 完成或技术 validator PASS 都不能推断人工批准。

这里必须区分三层能力，不能混写：

1. **Schema 防护：** task validator 可以拒绝声明了绝对路径、`..` 或 task 外 `allowedOutputs` 的任务；
2. **事后侦测：** coordinator 可对受保护正式路径做 pre/post SHA inventory，发现意外变更后中止并从阶段快照恢复，但 inventory 不能证明“没有读取”也不能阻止写入；
3. **运行时强隔离：** 只有 OS 沙箱、隔离 worktree/workspace 或工具级 read/write allowlist 能真正限制 subagent 只读 inputs、只写 task attempt 目录。

实际派发 `storyboardPlanning`、`visualReview` 或 `annotationDrafting` 前，runtime 必须返回并由适配层验证 `readIsolationEnforced=true`、`writeIsolationEnforced=true`、`networkDenied=true` 和 `allowedWriteRoot=<attempt-dir>`。任一能力缺失、未知或只是 prompt 约定时，`dispatchAllowed=false`。当前 Codex 协作 runtime 的 agent 共享同一文件系统与工作目录，因此按本合同不得把写 candidate 的 role 派发给 subagent；coordinator 只能在主进程内按相同 task/candidate validator 串行执行。即使 `visualReview` 本身只计划写 findings/result，它也拥有同等文件工具权限，仍受相同 Gate。

运行时适配接口建议固定为：

```python
@dataclass(frozen=True)
class AgentIsolationCapabilities:
    read_isolation_enforced: bool
    write_isolation_enforced: bool
    network_denied: bool
    allowed_read_files: tuple[str, ...]
    allowed_write_root: str | None
    evidence_kind: str  # os_sandbox | isolated_workspace | tool_allowlist | unavailable

    def allows_dispatch(self, task_dir: Path, inputs: Sequence[Path]) -> bool:
        ...
```

`evidence_kind=prompt_contract` 不在允许值内；能力结果必须来自 runtime/tool 实际机制。适配器不认识当前 runtime、不能核对 read files 或 write root 时返回 `unavailable`，不能乐观默认 true。

### 4.3 Artifact-first 任务目录

新增建议文件：

```text
references/subagent-orchestration.md
scripts/agent_task_contract.py
tests/test_agent_task_contract.py
```

agent task 有两种 scope，必须位于 `config/workspace.local.json` 配置的同一 workspace 内。`scopeRoot` 的语义固定为：draft task 使用 `<workspace>/drafts/<draft-id>`，project task 使用 `<project>`；task/result 中的所有相对路径都相对该 `scopeRoot` 解析：

```text
# 阶段 1、正式建项前
<workspace>/drafts/<draft-id>/.work/<run-id>/agent-tasks/<task-id>/attempt-<NNNN>/

# 正式建项后
<project>/.work/<run-id>/agent-tasks/<task-id>/attempt-<NNNN>/

# 两种 scope 的 task 目录内容相同
  task.json
  result.json
  scene-brief.json                  # annotation task 的冻结最小语义输入，按 role 可选
  agent.log                         # 可选，只供诊断
  role-contract.md                 # 从权威 reference 冻结到本 attempt 的只读副本
  candidate.generation-plan.json   # 按 role 可选
  candidate.annotation.json        # 按 role 可选
  findings.json                    # findings 很长时可选
```

`draft-id`、`run-id` 和 `task-id` 由 coordinator 生成并通过安全文件名校验；`taskId` 在同一逻辑任务的 retry 间保持稳定，`attempt` 从 1 递增并对应独立的 `attempt-0001`、`attempt-0002` 目录，成功证据以 `(taskId, attempt, taskSha256)` 唯一标识。`storyboardPlanning` 必须使用 `scopeKind=draft`：coordinator 先把传统 SRT 原样复制为 `<scopeRoot>/source.srt`，把严格 parser 结果写为 `<scopeRoot>/parsed-srt.json`，记录二者 SHA 后再派发 task，不创建正式项目。`visualReview` 与 `annotationDrafting` 必须使用 `scopeKind=project`。

task/result 内只保存相对当前 scope root 的 POSIX 路径；正式 project JSON 合同仍只保存项目相对路径。coordinator 给 subagent 的 prompt 只包含 attempt 冻结 `role-contract.md` 和 `task.json` 的本机绝对路径用于定位，不包含 live Skill reference；不得把绝对路径复制到正式 JSON、manifest 或 identity。任何 draft/project scope 都不得位于 C 盘临时目录、用户目录或 workspace 之外。

`scopeRoot` 不由不可信 task 自报，也不写入 task JSON；coordinator 根据已校验的 draft/project 对象把可信 `scopeRoot` 作为 validator 参数传入，并验证 `task.json` 的真实路径正好位于该 scope、run、taskId、attempt 对应目录。task 内的 `scopeKind` 只能与该可信上下文交叉核对，不能改变解析根。

subagent 不得扫描其他 run、用户目录、整个 workspace 或未列入 inputs 的正式文件；只读 inputs，只写 `allowedOutputs`。只有上述强隔离能力 Gate 通过才可实际派发；prompt、task JSON 或“不得”文字本身不构成隔离。无论是否派发，都不得把秘密或 provider 配置写进 task；pre/post protected-file inventory 仅作额外侦测与恢复证据。

### 4.4 `task.json` 最小合同

```json
{
  "contractVersion": "whiteboard-agent-task-v1",
  "taskId": "annotation-scene-03",
  "taskKind": "annotationDrafting",
  "scopeKind": "project",
  "roleContractVersion": "whiteboard-subagent-orchestration-v1",
  "roleContractSha256": "<sha256>",
  "attempt": 1,
  "sequence": 3,
  "sceneId": "scene-03",
  "inputs": [
    {
      "file": "scenes/scene-03-example.png",
      "sha256": "<sha256>"
    },
    {
      "file": "planning/timing-plan.json",
      "sha256": "<sha256>"
    },
    {
      "file": ".work/<run-id>/agent-tasks/annotation-scene-03/attempt-0001/scene-brief.json",
      "sha256": "<sha256>"
    },
    {
      "file": ".work/<run-id>/agent-tasks/annotation-scene-03/attempt-0001/role-contract.md",
      "sha256": "<same-role-contract-sha256>"
    }
  ],
  "currentBindings": {
    "generationPlanSha256": "<sha256>",
    "renderProfileSha256": "<sha256>",
    "activeTimelineSha256": "<sha256-or-null>"
  },
  "requiredCapabilities": [
    "readFiles",
    "viewImage",
    "writeCandidateJson"
  ],
  "allowedOutputs": [
    ".work/<run-id>/agent-tasks/annotation-scene-03/attempt-0001/candidate.annotation.json",
    ".work/<run-id>/agent-tasks/annotation-scene-03/attempt-0001/result.json"
  ],
  "formalWritesAllowed": false,
  "approvalWritesAllowed": false
}
```

规则：

- 顶层和嵌套字段均使用 allowlist，未知字段失败；
- `taskKind` 只允许 `storyboardPlanning | visualReview | annotationDrafting`；
- `scopeKind` 只允许 `draft | project`，并与 role 匹配；
- `attempt` 必须从 1 开始递增，并与 `attempt-<NNNN>` 目录严格一致；retry 保持逻辑 `taskId`，创建新 attempt 目录和新 task/result，不原地覆盖旧 result；
- 输入必须同时有 scope 相对路径与 current SHA，不能只传易变路径；
- `roleContractVersion` 必须来自 allowlist；coordinator 创建 attempt 时把权威 `references/subagent-orchestration.md` 的相应版本原子复制为本 attempt 的 `role-contract.md`，其 SHA 同时写入 task 和 inputs。派发前、subagent 读取前以及收取 result 后都核对同一 SHA；retry 创建新的冻结副本，不能读取 live reference；
- role 所需语义只冻结到最小 brief：annotation task 只含本幕 cue/scene，不复制完整 SRT；visual review 通过图片引用清单读取 current 图片；storyboard task 使用 draft scope 中冻结的 SRT 与 parser 结果；
- `requiredCapabilities` 使用 allowlist；coordinator 派发前必须确认 runtime/subagent 具备 role 所需能力。`visualReview`/`annotationDrafting` 缺少真实图片查看能力时不得仅凭文件名或 metadata 继续；只有 coordinator 自身也通过同一 capability preflight 时才允许串行 fallback，否则报告 `BLOCKED`。fallback 不是绕过能力检查，也不能形成“缺能力→无限 fallback”的循环；
- `allowedOutputs` 必须解析后仍位于当前 task 目录；拒绝绝对路径、`..`、符号链接逃逸和跨 run 路径；
- coordinator 在派发前计算 `taskSha256`；task 派发后不可原地改写，收取 result 后必须重新计算并与派发 SHA 相同；输入变化时创建新 attempt/task，旧结果只能作为历史证据；
- task contract 不包含完整主对话、隐藏推理、秘密、provider 配置、批准凭据或未冻结的自然语言状态。

### 4.5 `result.json` 最小合同

```json
{
  "contractVersion": "whiteboard-agent-result-v1",
  "taskId": "annotation-scene-03",
  "taskKind": "annotationDrafting",
  "scopeKind": "project",
  "attempt": 1,
  "taskSha256": "<sha256>",
  "roleContractVersion": "whiteboard-subagent-orchestration-v1",
  "roleContractSha256": "<same-sha256>",
  "sequence": 3,
  "status": "completed",
  "inspectedInputs": [
    {
      "file": "scenes/scene-03-example.png",
      "sha256": "<sha256>"
    },
    {
      "file": ".work/<run-id>/agent-tasks/annotation-scene-03/attempt-0001/role-contract.md",
      "sha256": "<same-role-contract-sha256>"
    }
  ],
  "outputs": [
    {
      "file": ".work/<run-id>/agent-tasks/annotation-scene-03/attempt-0001/candidate.annotation.json",
      "sha256": "<sha256>"
    }
  ],
  "findings": [],
  "warnings": [],
  "error": null
}
```

`status` 只允许 `completed | failed | cancelled`。subagent 自然语言消息、最终回答、进程退出码或目录时间戳扫描都不能单独作为成功证据。coordinator 必须重新读取 result，核对 task ID/kind/scope/attempt/sequence、`taskSha256`、role contract version/SHA、role 必需的 `inspectedInputs` 路径/SHA、输出路径/SHA、current inputs 未变化，并运行对应确定性业务 validator。

findings 过长时写 `findings.json`，result 只保留路径、SHA、计数和最高优先级摘要。`agent.log`、findings 和 error 不得包含秘密、绝对临时路径、PID、线程 ID、完整 provider 响应或模型隐藏推理。

### 4.6 Prompt 与上下文隔离

实现后的 coordinator prompt 模板固定为：

```text
读取本 attempt 冻结的 <role-contract-absolute-path>，
只执行 role=<taskKind> 的合同。

读取任务文件：<task-json-absolute-path>
冻结任务 SHA-256：<task-sha256>
冻结 role contract SHA-256：<role-contract-sha256>

不要依赖或复述主对话历史；不要扫描 task.json 未列出的目录；
只读取 inputs，只写 allowedOutputs；不得写正式路径、manifest、identity 或批准。

完成后验证 result.json，并且只返回：
TASK_STATUS=<completed|failed|cancelled>
RESULT_JSON=<result-json-absolute-path>
```

若 runtime 支持 fork 控制，使用空历史或最小历史；禁止默认复制完整主窗口上下文。主窗口只接收 task status、result path、结构化摘要、validator 结果和用户需决策的差异，不回灌完整 SRT、完整提示词数组、所有逐幕视觉推理或长工具日志。

### 4.7 调度、失败与恢复

agent scheduler 必须：

1. 先执行强隔离 capability Gate；Gate 未通过时不创建真实 subagent dispatch，记录 configured 值、`effectiveAgentConcurrency=0` 与 fallback 原因，由 coordinator 串行执行；
2. Gate 通过后才派发 dependency 已满足、role contract/inputs SHA current 的 ready task；
3. effective concurrency 取 configured、ready task、runtime child slot 和 coordinator resource budget 的最小值；
4. coordinator 永远保留自己的执行槽；
5. agent task 不得再启动 provider、FFmpeg、深验或其他受 `execution.concurrency` 控制的批处理，避免 agent×worker 并发乘法；
6. 完成结果按 `sequence` 汇总，不按返回时间；
7. 单 scene candidate 失败时允许其他独立 scene 完成；
8. 全局 generation/timing/audio/render binding 在 batch 期间变化时停止派发，取消未开始 task，把尚未发布候选判 stale；
9. retry 只创建 failed/cancelled/stale 的新 attempt；current completed 且 inputs/role contract SHA 未变的候选不重复执行；
10. subagent 失联、超时、缺少 result、schema 无效、声明越界路径、protected-file inventory 变化或输出 SHA 不符均为失败；其中只有强隔离 runtime 能声称“实际越界写入被阻止”，inventory 命中只能说明事后检测到违规；不能从自然语言“已完成”推断成功；
11. runtime 不支持强隔离 subagent 时，coordinator 按同一 task 合同串行生成候选，不跳过 validator、正式发布或人工确认。

失败候选留在本次 `.work` 供诊断，不覆盖旧 validated/current 文件。agent batch 的 status、configured/effective concurrency 和计数进入 run 审计，但不进入作品 identity，也不触发业务 stale。

这里的串行 fallback 仍受 role capability 约束：文本 role 可在 coordinator 具备文件读写能力时执行；视觉 role 必须同时具备真实图片查看能力。若能力本身不存在，结果是 `BLOCKED`，不是继续套用另一个 fallback。当前共享文件系统 runtime 的预期验收结果是“真实 agent dispatch SKIP/不可用、coordinator fallback 生效”，不能把 fake scheduler PASS 写成隔离能力 PASS。

## 5. 分阶段实施计划

### Phase -1：实施前合同与可回退 Gate

本阶段不修改业务行为；它是开始 Phase 0 前的强制 Gate，冻结本轮 review 修正后的四个 P1 合同：

1. generation-plan candidate 使用最终 schema：`prompt` 非空、`cueRange` 连续覆盖全部 SRT cue；`imagePrompt` 只属于 content draft，`sourceCueRange` 只属于派生 timing plan；
2. 当前共享文件系统 runtime 不满足 subagent 强隔离，真实 agent dispatch 必须 fail-closed；Phase 1A 可以实现 task/schema/fake scheduler，Phase 1B 只能接入条件入口，实际运行走 coordinator fallback；
3. annotation/preview/scene/clean 等聊天人工关卡由 coordinator 保证调用顺序，CLI 只验证技术 current；当前 `sceneRender` 无条件为 `1`；
4. 图片/TTS worker 使用第 4.1.1 节的 candidate/commit/recovery 协议，底层直接发布 API 未拆分前禁止并发外部请求。

同时建立真实可回退基线。由于当前目录不是 Git 仓库，单纯 SHA 清单不满足恢复要求：

- snapshot root 固定在 Skill 目录之外：`C:\Users\MOVER\.codex\skill-development-snapshots\srt-whiteboard-animation\`，不得写入正式 D 盘 workspace 或项目 manifest；bootstrap 工具位于 `<snapshot-root>\tools\dev_snapshot.py`，恢复测试也在外部临时副本运行。这样首个 snapshot 前无需修改 Skill 内任何文件；
- `create` 只原子复制该 Phase 文档声明的明确 write set 及其现有原字节，不做全树备份；新增文件记录 pre 不存在。工具硬拒绝 `.env`、API key、provider credential local config（例如 `config/image-providers.local.json`）、cookies/tokens 和任何检测到敏感键的文件进入 snapshot；本计划不得修改这些秘密文件。manifest 记录 snapshot contract、相对路径、原 SHA/bytes、是否原本不存在、创建时间、目标 Phase、前一 snapshot manifest SHA；
- snapshot root 和文件 ACL 仅允许当前用户访问；manifest 不记录文件内容、密钥或 credential 值。write set 若误包含秘密文件必须 fail-closed，而不是为了“完整回退”复制秘密；
- 每个 Phase 开始前创建 pre snapshot，结束后记录 post manifest；新增文件也要记录“pre 不存在”，以便安全删除；
- 每个 `<snapshot-id>` 内保留一份可独立运行的 `dev_snapshot.py`，恢复不依赖 live Skill 或可能已损坏的开发工具；
- `verify` 重算 snapshot 副本字节；`restore` 默认要求当前文件等于对应 Phase 的已知 post SHA，出现未知用户修改、额外同名文件或 snapshot 损坏时拒绝覆盖并输出冲突清单；不提供静默 `--force`；
- 在临时 Skill 副本中执行 create → 修改/新增/删除 → restore，验证原字节恢复、新增文件安全删除、未知修改拒绝覆盖。

建议命令合同：

```powershell
$snapshotTool = 'C:\Users\MOVER\.codex\skill-development-snapshots\srt-whiteboard-animation\tools\dev_snapshot.py'

python $snapshotTool create `
  --skill-root 'C:\Users\MOVER\.codex\skills\srt-whiteboard-animation' `
  --phase 'phase-1a' `
  --write-set <phase-write-set.json>

python $snapshotTool verify --snapshot <snapshot-id>

# restore 默认先核对当前字节等于本 Phase 已知 post manifest；发现未知修改就拒绝
python $snapshotTool restore `
  --snapshot <snapshot-id> `
  --expect-post-manifest <phase-post-manifest.json>
```

`phase-write-set.json` 与 post manifest 都属于开发证据，不能包含 secret local config、正式项目绝对路径或 provider 内容。

如果用户另行授权初始化 Git，可用基线提交替代后续快照，但在授权发生前不得把“可以初始化 Git”当成已完成回退能力。

Phase -1 Gate：上述四个合同写入测试断言，首个外部 snapshot 完成且恢复演练 PASS；否则不得开始 Phase 0。

### Phase 0：前半程快路径、基线、保护与可测量性

**目标：** 在不触碰人工确认边界的前提下缩短阶段 0–3 的失败反馈路径，并在非 Git 目录中建立可核对基线，确保后续性能结论不是主观感觉。

**修改：**

- 新增 `scripts/validate_content_draft.py`
- `scripts/prepare_env.py`
- 新增 `tests/test_validate_content_draft_cli.py`
- `tests/test_project_workspace.py`
- 所有包含非空 generation-plan scene 的现有测试 fixture（实施前用 `rg` 列出实际文件，逐幕补有语义的非空 `prompt`，不得在 helper 中偷偷注入通用 prompt 掩盖缺陷）
- `references/content-input.md`
- `SKILL.md`

实施：

1. 增加无写入的 content draft 检查命令，`--stdin` 从标准输入读取 UTF-8 JSON，只在内存调用 current `validate_content_draft()` 与 `build_source_package()`，输出结构化校验结果；它不得调用模型、不得运行 `prepare_source.py`、不得创建 source 包或正式项目、不得写任何批准。`--stdin` 与 `--draft` 互斥；确认前的当前对话只能使用 `--stdin` 或直接调用纯函数，`--draft` 只用于已经持久化的已确认输入和测试 fixture。
2. Codex 展示草案前先运行该只读检查；用户修改后只重验并展示受影响 cue、scene、旁白和提示词差异，不重新生成未受影响内容。
3. `prepare_env.py` 把基础依赖和显式 feature 的 import/version 检查从“每个依赖启动一个 Python 子进程”改为“一次子进程批量返回结果”；安装仍只在非 `--check` 且确有缺失时串行执行。
4. 不合并 `prepare_source.py` 与 `create_project.py` 的信任边界，也不缓存 `load_project()`：现有本机测量证明这些步骤是毫秒级，保留独立重验更可靠。
5. 使用 Phase -1 的外部 snapshot 工具保存每个 Phase 修改文件的真实原字节、pre/post manifest 与测试基线；SHA-256 和大小只用于检测/绑定，不能单独称为可回退。开发 snapshot 路径不写进正式项目 manifest。
6. 跑现有自动测试，记录 PASS/FAIL/SKIP，不先清理用户文件或 `.work` 历史目录。
7. 建立固定 fixture 性能场景：8 幕、多个 TTS unit、三层短媒体；性能测试只记录相对调用次数和受控并发，不设置脆弱的固定毫秒门槛。
8. 对真实项目的性能测量单列为手工/外部验收，不纳入普通单元测试。

建议命令：

```powershell
python -m unittest discover -s tests -p "test_*.py"

# 人工确认前：coordinator 通过 subprocess stdin 直接写入内存中的 UTF-8 JSON；
# 以下命令的 stdin 由 coordinator 提供，不创建草案临时文件
<ENV_PY> scripts/validate_content_draft.py --stdin

# 已确认且已经持久化的输入或测试 fixture 才能使用 --draft
<ENV_PY> scripts/validate_content_draft.py --draft <confirmed-content-draft.json>
```

验收：

- content draft 合法时输出 identity、cue/scene 数量和 `writesPerformed: false`；测试还必须比较调用前后临时 workspace 文件树、文件 SHA 与目录项，不能只相信该自述字段；
- content draft 非法时退出码为 2，且不会出现 source 包、项目目录或批准记录；
- `prepare_env.py --check --feature edge-tts` 的依赖探测只启动一个 venv Python 子进程；
- SRT 解析、准备包验证和项目加载的严格性与字节身份不变；
- 现有失败和跳过项有明确清单；
- 未修改正式 D 盘项目；
- 未调用真实 Edge 或图片供应商；
- 形成可验证、可恢复的实施前原字节 snapshot、pre manifest 与测试基线；在临时副本中的恢复演练通过。

### Phase 1：JSON 配置、共享执行器与 Subagent 工件基础设施

**修改：**

- `config/workspace.example.json`
- `config/workspace.local.json`（只补显式 `agents.default: 1` 与 `concurrency.default: 1`，不擅自启用高并发）
- `scripts/project_workspace.py`
- 新增 `scripts/bounded_execution.py`
- 新增 `scripts/agent_task_contract.py`
- `tests/test_project_workspace.py`
- 新增 `tests/test_bounded_execution.py`
- 新增 `tests/test_agent_task_contract.py`
- 新增 `references/subagent-orchestration.md`（仅在 Phase 1A 基础设施 Gate 通过后进入 Phase 1B）
- `SKILL.md`（仅在 Phase 1A 基础设施 Gate 通过后进入 Phase 1B 接入）

实施：

1. 落地第 3 节 `execution.concurrency` 与 `execution.agents` JSON schema 和严格校验；
2. 保证旧 `{schemaVersion, workspaceRoot}` 配置的 worker 和 agent 两个资源池都回退为 `1`；
3. 分别实现 worker stage 与 agent role allowlist、`for_stage()` 和 `for_role()`，禁止跨对象继承；
4. 实现 worker 并发 `1` 普通循环和 `>1` 有界滚动调度；
5. 实现第 4 节 `task.json`/`result.json` validator、安全路径解析、SHA/current binding 核对和 attempt 语义；
6. 实现 agent configured/effective 计算接口；runtime child slot 由 coordinator 注入，配置 loader 不猜测运行时能力；
7. 建立空历史/最小历史 prompt 模板；每个 attempt 冻结 `role-contract.md` 并绑定 version/SHA，task 中不复制主对话或大数组，也不读取执行中可能变化的 live reference；
8. 实现计划顺序汇总、失败/取消/stale 和 retry 语义；
9. 先用 fake scheduler 验收基础设施，不启动真实 subagent、不修改正式 D 盘项目；以上 1–9 为 Phase 1A；
10. Phase 1A 的配置、schema、路径和测试全部通过后，才进入 Phase 1B：新增 `references/subagent-orchestration.md` 并在 `SKILL.md` 接入“强隔离能力满足才 dispatch，否则 coordinator fallback”的条件入口；在此之前 current Skill 行为不变。当前共享文件系统 runtime 的真实验收必须得到 `dispatchAllowed=false`，不能因 fake scheduler PASS 而启用 subagent；
11. Stage 1 的传统 SRT 可创建单个 `storyboardPlanning` task：强隔离 Gate 通过时由 subagent 执行，否则由 coordinator 串行执行。执行者读取 draft scope 中冻结的 `source.srt`、`parsed-srt.json` 与 `role-contract.md`，写不带正式 `projectId` 的 `candidate.generation-plan.json` 和 `result.json`；candidate 直接使用最终 generation-plan scene schema，必须包含非空 `prompt`、parser 对应的 `cueRange`、`sceneDurationMs`、`outputFile`、`coreIdea` 和 `visualSubject`。`imagePrompt` 是 content draft 字段，不得出现在该 candidate；`sourceCueRange` 是派生 timing plan 字段，也不作为 canonical generation-plan 输入；
12. 收紧 `validate_generation_plan_data()`：每个正式非空 scene 的 `prompt` 必须是去空白后非空字符串，堵住所有入口静默退化为全局提示词；已有非空项目若缺 prompt 必须显式失败并要求用户修复，不能从 `coreIdea` 猜 prompt 或继续生图。补 prompt 会改变 generation-plan SHA，并按既有 generation/image/downstream stale 规则重验。`cueRange` 的候选严格要求放在 pre-project/new-project validator：必须是两个递增正整数，并证明所有 frozen SRT cue 被 generation scenes 按顺序连续、无重叠、无遗漏地覆盖；随后在内存中注入临时 UUID，复用现有 generation plan 校验和 source timing plan 构建逻辑。验证通过后 coordinator 完整展示策略并等待用户确认；该校验不建项、不写批准、不修改原 SRT；
13. 用户明确确认后，coordinator 才把确认的 candidate 原子冻结为 `<draft-scope>/confirmed-generation-plan.json` 并记录 SHA；`create_project.py --plan` 直接读取该文件，由现有 `create_generation_plan()` 注入真实新项目 UUID，再生成 current generation/timing plan。用户要求修改时创建新 attempt/candidate，并重新展示、重新确认，不能原地改写已确认字节；
14. topic/text 阶段 0 不拆给多个 subagent，继续由当前 Codex 对话统一生成旁白、cue、scene、视觉建议并处理用户修改，防止跨片段语义和风格漂移。

测试：

- 缺失 execution/agents/concurrency/default；
- 每个合法阶段覆盖；
- 每个合法 agent role 覆盖，agent/worker default 不互相污染；
- 0、负数、17、浮点、字符串、bool、未知字段；
- `sceneRender=2` 在当前 contract 下显式失败，不查询或推断聊天批准；
- 并发 1 峰值为 1 且顺序与输入一致；
- 并发 4 峰值不超过 4；
- 完成顺序打乱但输出顺序稳定；
- `continue_independent` 与 `stop_dispatch`；
- 取消与异常不产生丢失结果或重复提交；
- task/result 未知字段、错误 contractVersion、task ID/kind/sequence 不匹配；
- role/scope 不匹配、attempt 非正整数或 result attempt 不匹配；
- 绝对 allowed output、`..`、符号链接、跨 task/run/scope/workspace 路径全部拒绝；draft/project scope 都不得回退到 C 盘；
- task 派发后被修改、result `taskSha256` 缺失或与派发 SHA 不同全部失败；
- role contract version/SHA 缺失、live reference 被直接读取、attempt 冻结副本被修改或 result 未回显同一 role contract identity 时失败；
- `formalWritesAllowed`/`approvalWritesAllowed` 缺失或不为 false 时拒绝派发；
- requiredCapabilities 未知、强隔离能力未知/缺失或 runtime 缺少 role 必需能力时不派发；当前共享文件系统 runtime 预期 `effectiveAgentConcurrency=0` 并走 coordinator fallback；visual role 不得在未实际查看图片时完成；
- completed result 缺输出、输出缺失、SHA 不符、输入 SHA 已变化时失败；
- 自然语言完成但无有效 result 时失败；
- configured=3/runtime child slots=2/ready tasks=8 时 effective=2；
- runtime 无 subagent 时走 coordinator 串行 fallback，validator 和人工关卡不变；
- 生成的 subagent prompt 不包含 source SRT 正文、提示词数组、provider 配置、秘密或主对话历史。
- storyboardPlanning 只在 workspace draft scope 产生候选，不创建正式项目、不改 SRT、不调用 provider、不写策略批准；
- storyboardPlanning candidate 使用非空 `prompt + cueRange`；缺 prompt、误用 `imagePrompt`、cue 重叠/遗漏都在建项前失败；pre-project generation/timing 校验、确认后冻结和 `create_project.py --plan` 消费链全部通过；
- 既有 generation plan scene 缺 prompt 时 loader/生图显式失败；用户补 prompt 后 generation/image/downstream 按 SHA stale，不静默沿用全局 prompt；
- 全量现有 fixture 经 audit 后显式携带逐幕 prompt；不能为了减少测试改动而放宽正式 validator 或在测试 helper 里隐式填默认值；
- topic/text content draft 路径不会误触发 storyboardPlanning 多代理分片。

Phase 1A Gate：worker/agent 配置、task/result schema、路径声明逃逸、role contract 冻结、强隔离 capability adapter、fake scheduler 和串行 fallback 测试未全绿，不允许创建编排 reference 或修改 `SKILL.md` 接入条件入口，也不进入任何外部请求或媒体改造。Gate 通过后才实施 Phase 1B；Phase 1B 只允许在真实隔离集成测试 PASS 的 runtime 启用 dispatch，当前共享文件系统 runtime 必须保持 fallback。validator 拒绝越界路径声明不能冒充 runtime 已实际阻止越界写入。

### Phase 2：图片生成与图片验证并发

**修改：**

- `scripts/generate_images.py`
- `scripts/image_generation.py`（仅在 manifest run 需要审计字段时修改）
- `scripts/validate_generated_images.py`
- `tests/test_image_generation_cli.py`
- `tests/test_image_generation.py`
- `references/image-generation.md`
- `references/subagent-orchestration.md`
- `SKILL.md`

图片生成：

1. 从 workspace JSON 读取 `imageGeneration`；
2. `generate_images.py` 在通过严格 plan validator 后使用 `scene["prompt"]`，不得再用 `scene.get("prompt", "")` 静默回退成只有 global prompt；缺失/空白 prompt 以配置/plan 错误在任何 provider 请求前失败；
3. 并发前先把 `normalize_and_store_image()` 拆为 candidate API：接收 attempt candidate 路径并只在该路径原子落盘，不得在 worker 内 `os.replace(..., scenes/<formal>.png)`；串行兼容包装也走同一 coordinator commit 协议；
4. coordinator 按第 4.1.1 节为每幕预登记唯一 attempt、candidate 路径、input/image identity 和 overwrite 决策；正式目标已存在且未授权 overwrite 时在 provider 请求前失败；
5. worker 处理请求、下载/解码和规范化，只返回 candidate `ImageMetadata`、SHA/bytes、identity 和去敏 receipt，不写正式 scene 或共享 manifest；
6. coordinator 单写 `ManifestStore`，依次保存 `candidate_ready/publishing/validated`，保留 candidate 直到 validated checkpoint 落盘；
7. 单幕明确失败继续其他幕，保留成功场景；`unknown_external_outcome` 不自动 retry；
8. `--retry-failed` 仍只处理外部结果明确 failed 的场景；candidate/publishing 恢复不重新调用 provider；
9. 已 validated 且未授权 overwrite 的场景不被降级或伪报为本次成功；
10. 不自动切换 backup provider；
11. 摘要增加配置与实际并发数，以及 adoptedCandidateCount、unknownExternalOutcomeCount。

图片验证：

1. 先串行验证 project/plan/manifest 全局合同；
2. `_validate_image()` 不再对同一 PNG 先 `Image.verify()`、再重新打开 `Image.load()`；改为一次完整 `load()` 并在同一打开周期检查 PNG format、1920×1080、RGB 和截断/解码错误，随后核对 SHA；
3. 各幕 PNG 的单次完整解码与 SHA 按 `imageValidation` 并发执行；
4. 汇总按 generation plan 顺序；
5. 技术 validated 仍不替代用户线稿确认。

只读视觉初检：

1. 图片技术验证完成后，coordinator 可以创建一个 `visualReview` task；只有强隔离 Gate 通过才派发 subagent，当前共享文件系统 runtime 由 coordinator 串行执行同一 task/checklist。首版使用单个 global review 同时观察全部 scene，避免每幕各自通过却漏掉跨幕人物/配色/纸张漂移；
2. task 只引用 generation plan、current 图片/SHA、风格约束和允许写入的 findings/result 文件；不包含 provider credential；
3. subagent 只输出按 scene ID 排序的 findings、warnings 和建议用户重点检查/重生成的 scene ID；
4. visual reviewer 不修改图片、不调用 provider、不写 generation manifest、不自动重试生图；
5. coordinator 验证 result 后把 findings 作为辅助信息与图片一起展示，仍必须等待用户逐图明确确认；
6. 派发前按 runtime 的图片/上下文能力做 preflight；单个 global task 超限时首版不自动切成彼此失去全局视野的独立 reviewer，而是由具备真实图片查看能力的 coordinator 按同一检查表串行 fallback，并如实报告；双方都无视觉能力时为 `BLOCKED`；
7. runtime 不支持 subagent 时采用同一 capability-gated fallback，不影响图片技术验证的真实结果，也不绕过人工关卡。

测试：

- 并发 1 与旧测试行为一致；
- 受控客户端证明并发 4；
- 完成顺序 3→1→2，但 manifest scene 顺序仍 1→2→3；
- 一幕失败、其他成功、retry 只重试失败幕；
- overwrite 冲突不破坏旧 validated 记录；
- API Key 不出现在异常、summary、manifest；
- plan scene 缺失/空白 prompt 时 provider 调用数为 0；不会只使用 global prompt 继续；
- PNG 临时文件不冲突，原子发布失败不覆盖旧图；
- prepared/requesting/candidate_ready/publishing/validated 各边界的崩溃恢复符合第 4.1.1 节；有可采用 candidate 时 provider 调用数为 0，unknown outcome 不自动 retry；
- 截断、CRC/解码损坏、错误格式/尺寸/mode 继续失败，并证明每张 PNG 只完整解码一次。
- visualReview completed 不写线稿批准，findings 顺序按 generation plan；
- global 图片 SHA 在 review 期间变化时 result stale；
- global task 超过 runtime 图片/上下文上限时不派发；coordinator 有视觉能力时记录 fallback，无视觉能力时报告 BLOCKED；
- prompt/task/result 不包含 API Key、完整 provider 响应或绝对正式路径；
- agent 缺失/失败时图片技术结果仍如实保留，并向用户报告初检 FAIL/SKIP，不伪报人工 PASS。

### Phase 3：TTS unit 生成与语音深度验证并发

**修改：**

- `scripts/generate_voiceover.py`
- `scripts/edge_tts_adapter.py`（仅补线程安全测试或必要小修）
- `scripts/voiceover.py`（让 FakeProviderAdapter 的并发测试受锁保护）
- `scripts/audio_normalization.py`（拆分 candidate 与正式发布）
- `scripts/narration_review.py`
- `scripts/validate_voiceover.py`
- `tests/test_voiceover_cli.py`
- `tests/test_voiceover.py`
- `tests/test_edge_tts_adapter.py`
- `tests/test_narration_review.py`
- `references/voiceover.md`
- `SKILL.md`

生成合同：

1. 从 JSON 读取 `voiceGeneration`；
2. 调度器最多保持 N 个 unit 在途；
3. 并发前把音频规范化拆为 candidate API：worker 只能把 canonical WAV 原子写入已登记 attempt candidate，不能在返回前发布 `audio/segments/unit-xxxx.wav`；
4. 每个 unit 的 attempt candidate 路径、synthesis identity 和临时规范化目录独立；coordinator 在外部请求前持久化 prepared/requesting；
5. worker 不直接修改共享 voice manifest，也不写正式 segment；只返回 candidate media receipt；
6. coordinator 保存 `requesting/candidate_ready/publishing/validated/failed/cancelled/unknown_external_outcome` checkpoint，并按第 4.1.1 节发布/恢复；
7. 首个 provider、取消、规范化失败或 unknown outcome 后停止派发新 unit；
8. 已经在途且 candidate 已落盘的成功 unit 由 coordinator 原子发布并登记；
9. 尚未启动的 unit 保持可恢复非完成状态；
10. `--retry-failed` 不重请求 current、validated、identity 未变的 unit，也不自动重试 unknown outcome；
11. 全部 unit 成功后，严格按 unit index 串行合并 WAV、构建 timeline/SRT、生成 review 和 full identity。

语音验证：

1. 显式深度验证中的 segment WAV 可以使用 `voiceValidation` 并发；
2. timeline、SRT、scene 累计帧、full identity、review identity 和 approval 仍串行验证；
3. 增加 deep validation 与 downstream binding validation 的内部边界：
   - `deep`：新 WAV/review 候选生成后，或旧项目缺少 current 技术证据、验证器合同版本变化、用户显式要求强制深验时，包含 ffprobe/full decode；
   - `binding`：普通 `validate_voiceover.py`、批准和正式下游在 current SHA/bytes 与持久化深度证据完全匹配时，只重验 identity、timeline/SRT、批准和证据绑定，不重复深度解码相同字节；
4. narration review 的 `technicalValidation` 增加验证器合同版本、被深验字节 SHA、bytes、流/codec/尺寸/fps/帧数/时长与 full-decode PASS；候选深验后原子发布，相同 SHA 的正式文件复用该证据；
5. `validate_narration_review()` 删除同一次调用中“先 `sha256_file()`、后 `validate_video()` 再计算一次 SHA”的重复全文件读取；无 current receipt 时只允许转入 deep，不能静默通过；
6. approval 始终重新计算 current 文件 SHA 并绑定用户提交的 current identity，但相同字节不再仅为批准动作重复 full decode；
7. binding 绝不能在缺少 current 深度证据、SHA/bytes 变化、验证器合同变化或 approval stale 时降级通过。

测试：

- 并发 1 保持当前首错退出语义；
- 并发 4 峰值受控；
- unit 完成顺序打乱但 narration WAV/timeline/SRT 顺序不变；
- 首错后不再补充新请求，在途成功可保留；
- retry 不重复已验证付费请求；
- candidate_ready/publishing 在恢复时不调用 Edge；provider 返回后、candidate 前崩溃且无法幂等查询时进入 unknown_external_outcome，必须人工决定；
- queue interval 在共享 Edge adapter 下仍成立；
- 串行和并发 fixture 得到相同正式 WAV、timeline、SRT 和 full identity；
- deep 与 binding 对已改字节、旧 evidence、旧验证器合同、旧 approval 都必须失败；
- 新 review 只完整解码一次；随后普通 validate/approve 在 SHA 不变时走 binding；`--force-deep` 再次深验并刷新 receipt。

真实 Edge 只在自动 fixture 全绿后单列验收；429/DNS/服务不可用必须报告 BLOCKED，不得以 fixture PASS 代替。

### Phase 4：批量 annotation 校验去重

**修改：**

- `scripts/render_timing.py`
- 新增 `scripts/validate_annotations.py`
- `scripts/agent_task_contract.py`
- `tests/test_render_timing.py`
- 新增 `tests/test_annotation_batch.py`
- 新增 `tests/test_annotation_agent_orchestration.py`
- `references/subagent-orchestration.md`
- `SKILL.md`

新增一次运行内只读结构：

```python
@dataclass(frozen=True)
class FormalValidationContext:
    timing_plan_sha256: str
    timing_plan_file: str | None
    render_profile_sha256: str
    active_timeline: dict[str, Any]
    audio_sha256: str | None
    full_approval_identity_hash: str | None
```

实施：

1. `build_formal_validation_context(project)` 对全局 evidence 只验证一次；
2. `resolve_formal_scenes(project, scene_ids, context=...)` 复用该上下文；
3. 现有 `resolve_formal_scene()` 保留兼容包装，单幕路径行为不变；
4. 图片已获用户确认且 FormalValidationContext 冻结后，coordinator 为每个待处理 scene 创建唯一 `annotationDrafting` task；task 只引用该幕图片/字幕/时序、冻结 role contract 和全局 binding 摘要，不复制主对话；
5. 只有强隔离 Gate 通过才按 `execution.agents.annotationDrafting` 有界派发；默认 `1` 时逐个 isolated subagent 串行，`>1` 时只并发 ready scene。当前共享文件系统 runtime 必须记录 effective=0，由 coordinator 在主进程中逐幕串行生成 candidate；
6. 在强隔离 runtime 中，每个 subagent 只写本 task 目录的 `candidate.annotation.json` 与 `result.json`，不能写扁平 `scenes/` 正式 annotation；当前 runtime 由 coordinator 在同一 task 目录写 candidate；
7. `validate_annotations.py` 支持 candidate root：先验证 task/result/SHA/current binding，再按 `annotationValidation` 并发验证每幕独立图片、candidate annotation、frame range、timing source、region、protected region 和局部 reveal 时序；
8. coordinator 只把通过确定性 validator 且全局 context 仍 current 的候选按 generation plan 顺序逐幕原子发布到正式 `.annotation.json`；这里是“单文件原子、batch 可部分成功”，不是整批事务；失败 scene 不覆盖旧 current annotation，其他独立 scene 可以发布；
9. 发布只表示该幕 current 技术候选齐备。只要任一必需 scene 失败、缺失或 stale，batch 总状态就是 `FAIL`，并在摘要中记录 `partialSuccess: true` 与成功/失败计数（不新增第四种 agent result status）：可以向用户展示已成功幕供提前检查，但不得记录全局标注确认，也不得运行 Phase 5 的 `--all`；
10. 只有全部必需 scene 都是 current 且 validator PASS 后，coordinator 才按 generation plan 完整展示标注并停止等待用户确认标注内容；用户确认后仍只能进入区域预览，不能跳过预览确认或最终标注与时序确认；
11. 批量结束前重新核对关键全局 SHA/identity 未变化，防止运行中被修改；任一全局 binding 变化时尚未发布候选全部 stale；
12. 失败按 scene 聚合，输出仍按 generation plan 顺序；retry 只为 failed/cancelled/stale scene 创建新 attempt，current completed 且输入 SHA 未变的候选不重复生成；
13. runtime 不支持强隔离 subagent 时，只有具备真实图片查看能力的 coordinator 才能按同一 task/candidate/result 合同串行生成 annotation；否则报告 `BLOCKED`。后续 validator、正式发布和人工关卡完全相同。

测试硬门槛：

- 8 幕批量校验中 `validate_current_voiceover` 只调用 1 次；
- 不允许 8 个 worker 各自完整解码同一 review MP4；
- 强隔离 fake/runtime adapter 中 agent default=1 时峰值 child 为 1，配置 3 且真实隔离允许时峰值不超过 3；当前共享文件系统 runtime 峰值 child 必须为 0 并走 coordinator fallback；
- subagent 完成顺序 3→1→2，但 candidate 汇总与正式发布仍为 1→2→3；
- 每个 task prompt 只含冻结 role-contract/task 绝对路径、对应 SHA 和固定返回合同，不含 live reference、完整 SRT、主对话或其他 scene 图片路径；
- task validator 对正式 annotation、manifest、timing、approval 或 task 目录外的声明路径必须拒绝；真实隔离集成 harness 还必须证明越界写入被 runtime 阻止。当前共享文件系统 runtime 不运行该 subagent，不能用 schema 拒绝冒充文件系统隔离 PASS；
- completed result 未经 candidate validator 不能发布；
- agent task 全部 completed 不能写标注人工确认；
- 某一幕 annotation 失败不隐藏其他幕结果；
- 部分 scene 发布时 batch 必须为 FAIL 且 `partialSuccess: true`，不能写全局标注确认，也不能启动全量区域预览；
- 某一幕失败后 retry 不重做输入未变的 completed scene；
- 批量期间全局 timing/audio identity 改变时整体 stale；
- Disabled 与 Edge 两条路径分别覆盖；
- schema v1 disabled 兼容仍只能显式启用。

### Phase 5：区域预览批量生成与 contact sheet

**修改：**

- 重构 `scripts/render_annotation_preview.py` 为可导入的纯函数；
- 新增 `scripts/generate_annotation_previews.py`
- 新增 `tests/test_annotation_preview_batch.py`
- `SKILL.md`

命令：

```powershell
<ENV_PY> scripts/generate_annotation_previews.py `
  --project <项目根目录> `
  --all
```

并发数只从 JSON `annotationPreview` 读取。

实施：

1. coordinator 是阶段 6“标注内容确认”的唯一聊天 Gate：只有在当前对话收到用户明确确认后才允许调用 `--all`。该确认首版不持久化，CLI 不读取、推断或伪造聊天状态；用户修改 annotation 后 coordinator 必须视此前聊天确认失效并重新确认；
2. `generate_annotation_previews.py --all` 只强制技术边界：generation plan 中全部必需 scene 的 annotation 都存在、current、validator PASS；不满足时按既有无效/stale 语义退出 2 或 5。它不读取此前某次 batch 的聊天/运行状态，也不能把技术 current 当作人工批准；
3. 使用 Phase 4 的全局上下文和已验证场景，不重复全局深度校验；
4. 各 worker 在本次 `.work/annotation-preview-<runId>/` 生成唯一候选 PNG；
5. 删除未使用的 28px 字体加载；字体实例按线程安全方式复用或线程本地加载；
6. PNG 使用 `compress_level=1, optimize=False`，保持像素无损；
7. 候选重新打开验证 PNG/RGB/1920×1080，计算 SHA 后原子发布到 `previews/<scene-stem>-annotation-preview.png`；
8. 失败候选不覆盖旧 current 预览；
9. 生成 `previews/annotation-preview-contact-sheet.png`，按 scene 顺序排列缩略图，并标明 scene ID、名称、元素数和时长；
10. contact sheet 只是快速总览，不替代独立全分辨率预览，也不自动写人工确认；
11. 生成完必须停止，向用户展示 contact sheet 和必要的全分辨率预览，等待明确确认。

测试：

- 8 幕预览全局 voice/timing evidence 只验证一次；
- annotation batch 非全 current 或 validator 未全 PASS 时，CLI 按技术无效/stale 合同失败且不生成新预览；测试明确证明 CLI schema 中不存在“聊天已批准”字段，也不会因技术 PASS 写人工确认；
- coordinator 编排测试证明未收到聊天确认时不会调用 CLI，annotation 变更后旧聊天确认在 coordinator 状态机中失效；该测试不能伪装成 CLI 自己知道聊天内容；
- 并发 1 与并发 4 的预览像素完全一致；
- contact sheet 顺序固定；
- 一幕失败不覆盖其旧预览，其他幕可成功；
- PNG 是无损 RGB 1920×1080；
- 输出与 JSON 不包含绝对工作目录或秘密；
- 用户确认状态没有被脚本自动写入。

### Phase 6：媒体校验去重与有界并发

**修改：**

- `scripts/media_validation.py`
- `scripts/render_stream_whiteboard.py`
- `scripts/merge_scenes.py`
- `scripts/burn_subtitles.py`
- `scripts/mux_voiceover.py`
- `scripts/validate_final_media.py`
- `tests/test_final_media.py`
- `tests/test_render_timing.py`
- `tests/test_subtitles.py`
- `tests/test_edge_delivery_e2e.py`

#### 6.1 probe 帧数策略

1. 新增轻量 metadata probe，可读取合法 `nb_frames` 作为预检提示，但它不能单独成为新正式候选的 exact frame count 证据；
2. 重构 `full_decode()` 为一次权威 deep decode：FFmpeg 完整解码到 null sink 的同时通过受控 `-progress`/统计接口返回 `decodedFrameCount`，并验证进程退出、`progress=end`、解码错误和唯一视频流；
3. 新候选的帧数必须以 `decodedFrameCount` 对照 timing plan；如果容器同时声明 `nb_frames`，两者不一致必须失败；
4. deep receipt 绑定媒体 SHA/bytes、validator contract version、decodedFrameCount、streams、duration 与 full-decode PASS；相同 SHA 的下游 binding 可复用，不再另跑 `-count_frames` 或第二次完整读取；
5. 旧项目缺少 deep receipt 时执行一次新的统计型 full decode；`-count_frames` 只保留为诊断/迁移对照，不作为绕过 deep decode 的快速 PASS；
6. 对异常、变帧率、多视频流、progress 不完整或帧数不可证明的容器必须失败，不能估算冒充 exact frame count；
7. manifest 记录 `frameCountEvidence: decoded_frames_v1`；`container_nb_frames` 只能作为附属 metadata，不得标记为权威证据。

#### 6.2 原子发布后的快速核对

新产物候选：

```text
candidate 统计型深度验证一次
  → 保存 SHA/bytes/streams/decodedFrameCount/validation
  → 原子发布
  → 正式路径重新核对 SHA/bytes
  → 复用候选技术证据
```

不再对同一字节发布前后各跑一次 `validate_video + full_decode`。

#### 6.3 合并前场景验证

1. 从 JSON 读取 `sceneMediaValidation`；
2. 若 render manifest 中有 current 深度证据且正式文件 SHA/bytes 未变，优先做快速 binding；
3. 缺证据或证据不 current 时，按配置并发深度验证各场景；
4. concat/re-encode 和最终 clean master 验证始终串行；
5. 输入/摘要顺序始终按 timing plan；
6. concat copy 继续优先，fallback re-encode 仍为显式既有路径。

#### 6.4 字幕、mux 和 final

- 删除 `burn_subtitles.py` 对 clean master 的显式 `probe_media()` + `validate_video()` 双 probe；只保留一次权威验证结果。
- mux 前对 current clean/captioned 优先核对持久化 SHA/bytes/identity，不重复 full decode；新 final 必须深度验证。
- final validator 从 JSON 读取 `finalMediaValidation`，并发执行 clean/captioned/final 的独立媒体探测、SHA 和完整解码，再串行检查三者的 subtitle/audio/timeline/delivery identity。
- Disabled final 与 captioned SHA 相同且字节相同，可复用同一深度媒体证据，但最终路径存在性、大小和 SHA 仍需独立核对。

测试：

- 有无 `nb_frames` 都必须对新候选执行一次统计型 full decode；它不会再额外启动 `-count_frames` 扫描；
- `nb_frames` 与 decodedFrameCount 不一致、progress 非 end、解码错误或统计缺失都严格失败；
- 原子发布后正式 SHA/bytes 不一致必须失败；
- 单幕正式发布只发生一次深度解码；
- scene validation 并发受 JSON 限制且顺序稳定；
- final 三层媒体最大并发受控，交叉 identity 串行检查；
- 已修改上游字节不能仅凭旧 manifest evidence 通过；
- 所有现有退出码语义保持不变。

### Phase 7：正式渲染单次编码管道

**修改：**

- `scripts/render_stream_whiteboard.py`
- 可能新增 `scripts/ffmpeg_frame_sink.py`
- `tests/test_render_timing.py`
- `tests/test_edge_delivery_e2e.py`
- `SKILL.md`

目标流程：

```text
Python/OpenCV 生成 BGR24 帧
  → FFmpeg stdin rawvideo
  → libx264 medium/CRF18
  → H.264/yuv420p candidate
```

实施要求：

1. 保持正式 1920×1080、60fps、累计帧边界、H.264、yuv420p、0 音频、CRF18 和默认 medium；
2. 删除正式路径的 OpenCV MP4V 中间文件与二次解码转码；
3. writer 抽象必须兼容现有 `write(frame)` 调用，不能重写绘制算法；
4. Windows 上 FFmpeg stderr 写入本次 `.work` 日志或由专门线程消费，不能因 pipe 填满死锁；
5. FFmpeg 提前退出时立即停止帧生成并报告稳定媒体错误；
6. 关闭 stdin 后等待子进程，严格验证退出码和目标帧数；
7. 失败候选保留在本次 run 或按既有证据规则清理，绝不覆盖旧正式场景；
8. 候选按 Phase 6 深度验证一次，发布后只核对 SHA/bytes。

测试：

- 第 0 帧仍为干净纸底；
- 未开始区域不提前露线；
- protectedRegions、ink→color 顺序和尾部 0.5 秒不变；
- 帧数严格等于 timing plan；
- H.264/yuv420p/60fps/0 音频合同不变；
- FFmpeg 早退、BrokenPipe、取消和磁盘错误不破坏旧正式文件；
- 与现有 renderer 的关键抽帧像素按明确容差比较，不能只验证文件能播放。

### Phase 8：正式多幕候选并发（条件阶段）

本节只保留为未来设计备忘，不属于本计划的实施阶段。当前版本没有持久化的“聊天已批准新关卡”状态，配置 loader 或独立 CLI 无法证明用户曾在对话中授权，因此不能把 `sceneRender > 1` 的合法性绑定到不存在的批准记录。

本计划实施期间的机器可验证合同固定为：

- `sceneRender` 只允许 `1`；配置大于 `1` 一律按配置错误拒绝；
- 正式渲染继续由 coordinator 逐幕调用、逐幕展示、逐幕等待聊天确认；
- 不新增 `scripts/render_scenes.py`，也不声称 CLI 能读取聊天批准。

未来若决定实施多幕候选并发，必须另开计划并至少选择一种可执行授权边界：版本化 feature gate/contract version，或每次由 coordinator 向新 CLI 显式传入只对本 run 生效的候选批处理模式；该选择不能由当前计划预判。未来仍应保持以下人工语义：

建议新合同：

```text
全部 annotation、区域预览和最终时序已获用户批准
  → 可按 sceneRender 有界并发生成正式单幕候选
  → 按 timing plan 顺序逐幕完整播放/检查
  → 每幕仍分别获得明确确认
  → 所有幕确认后才能合并
```

未来方案可额外评估：

- 每幕首帧/重叠中段/颜色中段/最终帧 contact sheet；
- 只供审阅的 `previews/scenes-review-reel.mp4`，不作为 clean master，也不替代逐幕判断。

### Phase 9：字幕编码与 contact sheet 可选加速

**默认行为保持：** libx264 `medium`、CRF18。

可选扩展仍放在 `execution`，但与并发字段分离：

```json
{
  "execution": {
    "videoEncoding": {
      "subtitlePreset": "medium"
    }
  }
}
```

允许值首版：`medium | fast | veryfast`。preset 会改变正式字节、文件大小和编码 identity，必须写入 delivery/burn contract；不能像并发数一样排除在 identity 外。

字幕 contact sheet 的 3–4 个时间点可以有界并发快速 seek 截帧，但优先级低于整片编码和重复校验优化。

硬件编码（NVENC/QSV/AMF）不进入本计划首版：它受硬件、驱动、质量和可移植性影响，必须另立 provider/profile 合同，不能自动探测后静默切换。

### Phase 10：文档、全量回归与真实验收

**更新：**

- `SKILL.md`
- `README.md`
- `references/image-generation.md`
- `references/voiceover.md`
- `references/subagent-orchestration.md`
- 必要时 `references/subtitles.md`
- `config/workspace.example.json`

文档必须明确：

- worker/agent JSON 配置位置和两个资源池默认均为 `1`；
- 每个 worker stage、agent role 字段和建议值；
- worker/agent 并发与 task contract 不进入 identity，但进入运行审计；
- coordinator/subagent 权限、artifact-first task/result、最小历史 prompt 和串行 fallback；
- runtime 强隔离 capability Gate；当前共享文件系统 runtime 为什么必须 `dispatchAllowed=false`，以及 schema 防护、事后侦测和真实权限隔离的区别；
- attempt 级冻结 role contract version/SHA，不读取 live reference；
- storyboardPlanning、visualReview、annotationDrafting 的前置条件、允许输出和禁止行为；
- 全局证据去重和 deep/binding 边界；
- external candidate/commit/recovery 状态机、unknown external outcome 和 manifest 单写；
- 聊天人工关卡由 coordinator 保证调用顺序，CLI 只验证技术 current；
- fixture、真实 provider、真实媒体和人工验收的 PASS/BLOCK/SKIP 边界。

自动回归：

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

必须单列结果：

```text
自动 fixture：PASS/FAIL
agent task/result contract：PASS/FAIL
agent 串行 fallback：PASS/FAIL
annotationDrafting fake subagent 并发：PASS/FAIL
真实 runtime 强隔离能力：PASS/BLOCKED/SKIP（当前共享文件系统预期 SKIP，dispatchAllowed=false）
主窗口上下文隔离检查：PASS/FAIL
真实图片 provider：PASS/BLOCKED/SKIP
真实 Edge：PASS/BLOCKED/SKIP
真实 1080p/60fps 多幕渲染：PASS/FAIL/SKIP
人工区域预览确认：待用户确认/已确认
人工逐幕确认：待用户确认/已确认
人工 clean master 确认：待用户确认/已确认
人工 final 完整看片听音：待用户确认/已确认
```

## 6. 跨阶段不变量

任何 Phase 都不能破坏以下不变量：

1. **默认兼容：** 旧 workspace JSON、缺失 execution、缺失 stage 和值为 1 都是串行。
2. **确定顺序：** 正式 scene、unit、timeline、SRT、合并和摘要顺序来自冻结 plan，不来自 future 完成顺序。
3. **单写者：** generation/voice/render/delivery manifest 及批准记录只有 coordinator 写入。
4. **Coordinator 正式发布：** worker 只写已登记 attempt candidate；只有 coordinator 能按 candidate receipt 原子发布正式文件并写 validated checkpoint。失败候选不覆盖旧 validated/current 文件。
5. **恢复不自动重复付费：** current validated 或可采用 candidate 在 identity 未变化时不重复外部请求；外部结果不确定且无法幂等查询时进入 `unknown_external_outcome`，不得自动重试。
6. **无自动 provider 切换：** 并发、失败或限流都不能自动改 provider、voice、rate、model 或编码器。
7. **人工批准不可推断：** 技术校验、批量成功、contact sheet 或“用户未反对”都不是批准。
8. **时钟唯一：** Disabled 只用 source SRT；Edge 只用 approved audio timeline 和 narration SRT。
9. **累计帧边界：** 不逐幕独立 ceil 后相加，不使用 `-shortest` 掩盖误差。
10. **秘密边界：** JSON、manifest、日志和异常不保存 API Key、Cookie、Token、临时 URL、PID 或绝对临时路径。
11. **媒体严格性：** 新正式媒体必须有可验证 codec/流/尺寸/fps/decodedFrameCount/时长/SHA，并至少在其正式生产或最终验证边界完成一次带帧统计的完整解码；`nb_frames` 不是独立权威证据。
12. **并发参数不是 identity：** concurrency 和完成顺序本身不写入作品 identity；确定性任务或冻结候选回放必须保持正式顺序与 identity。对重新调用 provider/model 的非确定性任务，实际输出字节可能不同，正式 identity 必须如实随内容变化；编码 preset 也会改变正式字节，必须进入 identity。
13. **Coordinator 唯一：** 只有主 agent 与用户交互并写正式 manifest/identity/stale/checkpoint/批准。只有强隔离 Gate 通过时才可实际派发 subagent；task 中的 false/allowedOutputs 声明本身不等于权限。
14. **Candidate 不是 current/approval：** task/result completed 只说明候选可进入 validator，不代表已发布、已确认或可进入下一人工关卡。
15. **上下文 artifact-first：** subagent prompt 不携带完整主对话、大 SRT、提示词/路径数组或长日志；输入和输出以路径+SHA 的 task/result 工件交接，role contract 也冻结到 attempt 并绑定 SHA。
16. **Agent/worker 资源池隔离：** agent task 不嵌套启动 provider、渲染或深验 batch；configured/effective 分别审计，禁止并发乘法。
17. **能力降级不降合同：** runtime 无强隔离或 child slot 不足时，真实 agent effective 降为 0/受限值并使用 coordinator 串行 fallback，不跳过候选 validator、正式发布和人工确认；当前共享文件系统不能启用 agent dispatch。

## 7. 测试矩阵

| 场景 | 并发 1 | 并发 >1 | 失败/恢复 | 顺序/identity |
|---|---|---|---|---|
| workspace 配置 | 旧 JSON 回退 1 | 阶段覆盖 | 非法值拒绝 | 与项目 identity 无关 |
| agent 配置 | 缺失回退 1 | role 覆盖且受 runtime 限制 | 非法 role/值拒绝 | 与项目 identity 无关 |
| storyboardPlanning | 当前 runtime coordinator 单候选 | 仅隔离 runtime 可派发，ready 仍为 1 | schema/receipt 失败不发布；越界声明与真实隔离分别测 | coordinator 展示后才确认 |
| visualReview | 当前 runtime coordinator global review | 仅隔离 runtime 可派发，effective 最大 1 | findings 失败不改图片状态 | 不能写线稿批准 |
| annotationDrafting | 当前 runtime coordinator 逐 scene | 仅隔离 runtime 可 bounded agents | 失败只 retry 对应 scene | candidate/publish 按 plan |
| 图片生成 | 旧串行语义经 candidate commit | 峰值受控 | candidate 可恢复；unknown outcome 不自动 retry | manifest/正式发布由 coordinator 按 plan |
| TTS 生成 | 旧首错停止经 candidate commit | 滚动窗口 | 首错停派发；candidate 可恢复；unknown outcome 停止 | 合并按 unit index |
| 图片验证 | 串行 | 并发 PNG | 聚合全部错误 | 汇总按 scene |
| 语音验证 | 串行 segments | 并发 segments | 任一 stale 失败 | timeline/full identity 不变 |
| annotation 校验 | 串行 scenes | 并发 scenes | 单幕错误聚合 | 全局 evidence 只验 1 次 |
| 区域预览 | 串行 PNG | 并发 PNG | 失败不覆盖旧图 | 并发像素相同，contact sheet 有序 |
| 单幕渲染 | 现行路径 | 当前合同拒绝 >1 | 候选失败不覆盖旧幕 | 帧边界与像素合同不变 |
| 场景媒体校验 | 串行 | 并发 2 | stale evidence 回退深验或失败 | timing 顺序不变 |
| final 校验 | 串行三层 | 并发 2 | 任一层失败整体失败 | 交叉 identity 串行核对 |

并发测试必须证明：

- 峰值 active 数；
- 没有超过配置；
- 并发 1 不创建并行执行；
- 完成乱序不改变正式顺序；
- 确定性 fixture 或复用同一组冻结 candidate/result 的回放，在并发 1 与并发 N 下得到相同正式 identity；fresh provider/model 调用只验证调度参数不入 identity，不能要求随机输出字节相同；
- 无共享 manifest 写入竞争；
- validated/candidate_ready/publishing 恢复没有重复外部请求；unknown external outcome 不自动 retry；
- 取消和异常不会留下无法恢复的假 `running` 状态。
- agent prompt 内容不随 SRT 正文长度或幕数线性复制完整业务数组；
- agent valid result 之前没有 coordinator 正式发布，result completed 之后仍必须通过业务 validator；worker candidate 也必须先经过 candidate_ready receipt；
- agent task 完成乱序不改变 candidate 汇总、正式发布或用户展示顺序；
- runtime 强隔离 Gate 未通过时 effective=0 并 fallback；Gate 通过后 child slot 小于 configured 时 effective 如实降低；
- subagent 不调用图片/TTS provider、FFmpeg、批准命令或其他 bounded batch。

## 8. 性能验收方法

不以固定“必须小于 X 秒”作为跨机器硬门槛。采用结构性指标和同机前后对比：

1. 内容草案：人工确认前通过 stdin/纯函数校验，调用前后文件树和文件 SHA 证明写入数为 0；合法草案一次通过，非法草案在建项前失败；
2. 环境检查：基础依赖 + Edge feature 从每依赖一个解释器子进程降为一次批量解释器子进程；
3. SRT、source 包和项目加载：保持当前严格串行与独立信任边界，只监测是否出现数量级退化，不设并发目标；
4. 8 张图片验证：每张 PNG 从两次完整打开/解码降为一次，并按 `imageValidation` 受控并发；
5. TTS：记录 unit 数、外部请求区间、最后 unit 完成时间、post-unit 合并/review 时间和峰值在途 unit；不得只用整个 `full` 总时间掩盖瓶颈；
6. narration review：新候选完整解码一次；SHA/bytes 不变时，普通 validate、approve 和下游不重复 full decode；
7. 8 幕 annotation 批量校验：全局 `validate_current_voiceover` 调用从 8 次降为 1 次；
8. 区域预览：一个 Python 批量进程，最大 active worker 等于 effective concurrency，输出像素与串行一致；
9. 正式单幕：从 MP4V + H.264 两次编码降为直接 H.264 一次编码；
10. 单幕发布：从发布前后两次 deep validation 降为一次 deep + 一次 SHA/bytes 核对；
11. 媒体帧数：新候选的一次统计型 full decode 同时产生 `decodedFrameCount`，不再另跑 `-count_frames`；`nb_frames` 只作预检且与 decoded count 不一致必须失败；
12. 合并/mux：current 上游已有深度证据且 SHA 不变时不重复 full decode；
13. final：三层媒体深度验证最大并发受 JSON 限制，之后交叉 identity 仍串行；
14. 同一真实项目分别用默认串行和建议配置运行，记录 wall time、各 stage time、外部请求次数、ffprobe 次数、full decode 次数和峰值 worker；
15. 主窗口上下文：在强隔离 harness 中比较 coordinator fallback 与 artifact-first agent 编排时主窗口接收的正文/路径/日志字符量、消息块数和 scene 细节量；task 文件可以增长，但主窗口不得回灌完整副本。当前共享文件系统只报告 fallback，不虚构真实 agent 隔离收益；
16. storyboardPlanning/visualReview：强隔离 harness 中通常只有一个 task，只验上下文隔离和结构化返回，不伪造并行加速结论；当前 runtime agent dispatch 为 SKIP；
17. annotationDrafting：只在强隔离 harness/未来合格 runtime 中用同一 8 幕 fixture 比较 agent default=1 与 effective=2/3 的 wall time、峰值 child、task retry 数和主窗口摘要大小；当前 runtime 验证 effective=0 与 coordinator fallback。候选字节允许因模型判断不同，但都必须通过同一 validator 和人工关卡；
18. 资源隔离：agent batch 期间 provider 请求数、FFmpeg 数和 worker peak 不得因 agent 数增加而乘法放大；
19. candidate 恢复：candidate_ready/publishing 崩溃恢复的 provider 调用数为 0；unknown external outcome 自动调用数也为 0，并如实报告需要用户决定；
20. 性能提升不能以降低正式分辨率、fps、字幕质量、音频合同、候选验证、强隔离 Gate 或人工关卡换取。

## 9. 建议配置与调优边界

保守起步：

```json
{
  "execution": {
    "agents": {
      "default": 1,
      "storyboardPlanning": 1,
      "visualReview": 1,
      "annotationDrafting": 1
    },
    "concurrency": {
      "default": 1,
      "imageGeneration": 2,
      "voiceGeneration": 2,
      "imageValidation": 2,
      "voiceValidation": 2,
      "annotationValidation": 2,
      "annotationPreview": 2,
      "sceneRender": 1,
      "sceneMediaValidation": 1,
      "finalMediaValidation": 1
    }
  }
}
```

常见 SSD/多核机器建议起点：

```json
{
  "execution": {
    "agents": {
      "default": 1,
      "storyboardPlanning": 1,
      "visualReview": 1,
      "annotationDrafting": 1
    },
    "concurrency": {
      "default": 1,
      "imageGeneration": 4,
      "voiceGeneration": 4,
      "imageValidation": 4,
      "voiceValidation": 4,
      "annotationValidation": 4,
      "annotationPreview": 4,
      "sceneRender": 1,
      "sceneMediaValidation": 2,
      "finalMediaValidation": 2
    }
  }
}
```

调优原则：

- 当前共享文件系统 runtime 的 agent 配置保持 `1`，但 capability Gate 会记录 `effective=0` 并走 coordinator fallback；配置值不能绕过隔离 Gate；
- 只有未来 runtime 真实提供 read/write/network 隔离并通过集成测试后，多个 ready scene 的 `annotationDrafting` 才可把 configured 逐步升到 2–3；
- `storyboardPlanning` 和 `visualReview` 首版各只有一个 global task，配置更高也不会产生多个有效任务；
- agent effective 先受强隔离 Gate 限制，再受 runtime child slot 限制；coordinator 必须保留自己的槽位并如实记录 configured/effective；
- agent 与 worker 并发分别调优，不允许用多个 agent 各自启动图片/TTS/渲染 batch；
- 外部 API：先从 2 开始，观察 429/timeout，再决定是否升到 4；
- PNG/WAV：通常 4 有效；
- 大 MP4 完整解码：通常 2，机械盘可能 1 更快；
- 1080p/60fps 正式渲染：当前必须 1；本计划不实现多幕候选并发；
- 绝不因为配置较高而无限提交任务；
- provider 限流重试仍由各 provider 合同决定，并发层不扩大重试次数。

## 10. 明确不做

本计划首版不做：

- 通用分布式任务队列、Redis、数据库 worker 或多机渲染；
- 自动根据 CPU 数量、内存或 GPU 猜测并发并覆盖用户 JSON；
- 固定写死 subagent 并发为 10，或把 runtime slot 上限当成一定可用；
- 为了“使用多代理”给只需运行脚本、等待子进程或扫描目录的机械任务包一层 subagent；
- 在 subagent prompt 粘贴完整主对话、完整 SRT、提示词数组、全量路径数组或长日志；
- 通过目录扫描、时间戳排序、自然语言“完成”或单一退出码推断 agent task 成功；
- 让 subagent 直接写正式 scene/manifest/timeline/SRT/identity/stale/checkpoint/approval；
- 在共享文件系统或只有 prompt 约束的 runtime 派发 subagent，并把 `allowedOutputs`/hash inventory 冒充权限隔离；
- 让多个 subagent 各自启动 provider、TTS、FFmpeg 或 worker batch 形成并发乘法；
- 把 subagent completed、visual findings 或 candidate validator PASS 当作人工确认；
- 复制外部仓库无许可证的脚本、提示词或素材；本计划只独立实现 coordinator/artifact/context-isolation 思想；
- 自动切换图片/TTS provider；
- 对 `unknown_external_outcome` 自动重试付费请求，或让 worker 在 coordinator checkpoint 前直接发布正式 PNG/WAV；
- 自动切换软件/硬件编码器；
- 同一幕内部帧级并发重构；
- 降低 1920×1080、60fps、H.264、yuv420p 或字幕烧录合同；
- 跳过 full decode，而没有等价的新产物深度证据；
- 用容器 `nb_frames` 单独证明新正式候选的精确解码帧数；
- 用 contact sheet 替代完整单幕或 final 观看；
- 自动批准 annotation、区域预览、单幕、clean master 或 final；
- 让 CLI 从技术 current 推断聊天人工确认，或声称它能读取未持久化的聊天批准；
- 把 concurrency 写进 project.json、generation plan、timing plan 或正式 identity；
- 在普通自动测试中调用真实 Edge 或真实图片 API；
- 把性能优化扩张为整套 Skill 重写。
- 在本计划中实施 `sceneRender > 1`；这需要未来独立的 feature/contract 授权设计。

## 11. 实施顺序与停止条件

推荐按以下独立开发循环实施，每个循环都必须完成代码、测试、文档和自检后再进入下一个：

1. Phase -1：冻结 P1 合同、建立外部原字节 snapshot 并演练安全恢复；
2. Phase 0：stdin 内容草案只读校验、环境批量探测与可恢复基线；
3. Phase 1A：worker/agent JSON、bounded executor、task/result/role-contract 合同、强隔离 capability adapter 与 fake scheduler Gate；
4. Phase 1B：仅在 1A 全绿后接入 conditional subagent reference/Skill 入口；当前共享文件系统 runtime 仍强制 coordinator fallback；
5. Phase 2：先落地图片 candidate/commit/recovery，再实现图片生成/验证并发与条件 global visualReview；
6. Phase 3：先落地 TTS candidate/commit/recovery，再实现 TTS 生成/验证并发；
7. Phase 4：条件 annotationDrafting agent/coordinator fallback + 全局证据去重 + candidate validator；
8. Phase 5：区域预览批量并发 + contact sheet；
9. Phase 6：统计型 full decode、媒体校验去重与并发；
10. Phase 7：直接 FFmpeg 单次编码；
11. Phase 9：可选字幕 preset；
12. Phase 10：全量回归与真实验收。Phase 8 仅为未来设计备忘，不实施。

任一阶段出现以下情况必须停止进入下一阶段：

- 确定性任务或冻结候选回放中，并发 1 不再等价于串行；
- 正式顺序或 identity 因完成顺序变化；
- validated/current 产物被失败任务覆盖；
- 外部请求在 retry 时重复收费风险上升；
- worker 在 candidate_ready/validated checkpoint 前直接写正式 PNG/WAV，或 unknown external outcome 被自动重试；
- manifest 出现并发写竞争或 JSON 损坏；
- runtime 无强隔离却实际派发 subagent，或把 schema/哈希侦测写成“越界写入已被阻止”；
- 强隔离 runtime 中 subagent 写出 task allowedOutputs、当前 run 或 `.work` 边界；
- subagent 直接修改正式 annotation、manifest、timing、identity、stale、checkpoint 或批准；
- task/result 无效、输入 SHA 已变或候选未通过业务 validator，却被报告为 current/成功；
- agent 完成乱序改变正式 scene、展示、发布或摘要顺序；
- agent 数增加导致图片/TTS provider、FFmpeg 或 worker 并发乘法放大；
- 主窗口 prompt/result 重新携带完整 SRT、提示词/路径数组、长日志或 subagent 隐藏推理；
- runtime agent slot 不足时静默跳过任务、提高配置或把 configured 误报为 effective；
- 技术校验被误写成人工批准；
- CLI 被要求读取不存在的聊天批准，或技术 current 被当作 annotation/preview/scene/clean 人工确认；
- full decode 被删除但没有 current 深度证据替代；
- 新正式媒体仅凭 `nb_frames` 通过精确帧数验证，或 deep receipt 没有 decodedFrameCount；
- 真实媒体帧数、时长、字幕或音频合同退化；
- 日志或 manifest 泄漏秘密/绝对临时路径；
- 当前 Skill 目录的非 Git 状态使改动无法安全回退，或仅有 SHA 清单而没有外部原字节 snapshot/恢复演练；
- frozen role contract SHA 缺失、不匹配，或 task 执行时读取 live orchestration reference。

## 12. 完成定义

本计划只有在以下全部满足时才算完成：

- 内容草案可以在人工确认前通过 stdin/纯函数只读校验，文件树快照证明不会写草案临时文件、派生文件、项目或批准；
- Phase -1 外部原字节 snapshot、pre/post manifest、安全 restore 和未知用户修改拒绝覆盖均通过临时副本演练；
- 环境依赖使用一次批量解释器探测，SRT/source/建项的严格信任边界保持不变；
- worker/agent JSON 配置可用，旧配置和缺失配置两个资源池都默认 `1`；
- agent task/result schema、路径/SHA/current binding、attempt、冻结 role contract、失败/取消/stale 与串行 fallback 均有测试；
- `SKILL.md` 只在 agent 基础设施 Phase Gate 通过后接入 conditional coordinator/role 入口；当前共享文件系统 runtime 强制 `dispatchAllowed=false/effective=0`，不得实际派发 subagent；
- storyboardPlanning 只处理传统 SRT 候选，topic/text 完整草案仍由当前对话统一生成；
- storyboardPlanning candidate 使用最终 `prompt + cueRange` schema；缺 prompt、误用 imagePrompt、cue 重叠/遗漏均会失败；经 pre-project generation/timing validator、用户确认和 draft 冻结后，可被现有 `create_project.py --plan` 无歧义消费；
- visualReview 只输出跨幕 findings，不修改图片或替代用户线稿确认；global task 超出 runtime 容量时不会静默降级成失去全局视野的分片 PASS；
- 在合格隔离 runtime 中 annotationDrafting 只写 attempt 级 `.work` candidate；当前 runtime 由 coordinator 串行生成。coordinator 验证后按 plan 逐幕原子发布；部分成功 batch 仍为 FAIL，全部 current 后才继续等待标注/预览/最终时序人工确认；
- 主窗口不接收完整 SRT/提示词数组/逐幕推理/长日志，agent completed 不被当作 current 或批准；
- agent/worker 并发不乘法放大；强隔离缺失时 agent effective=0，隔离已满足但 slot 不足时 effective 如实降低；
- worker/agent 都受同一严格 workspace loader 控制；worker 使用 bounded executor，agent 使用 bounded scheduler，二者不共享或相乘并发预算；
- 图片/TTS worker 只写 attempt candidate，coordinator 完成 candidate receipt、正式发布和 validated checkpoint；各崩溃边界可恢复，unknown external outcome 不自动重复请求；
- 图片、TTS、图片/语音验证、annotation 校验、区域预览按合同并发；
- 每张消费 PNG 只完整解码一次，损坏检测合同不退化；
- current narration review 深度证据绑定 SHA/bytes 和验证器合同；相同字节的普通验证、批准和下游不重复 full decode；
- annotation 全局 voice/audio/review evidence 在批量运行中只深验一次；
- 区域预览有独立全分辨率文件和有序 contact sheet；
- 单幕正式渲染不再生成 MP4V 中间编码，且只深验候选一次；
- 新媒体一次统计型 full decode 同时产生 decodedFrameCount；不再执行额外 count scan，`nb_frames` 不作为独立权威帧数证据；
- 合并、mux 和 final 不重复深验 current 相同字节，且新正式产物仍有严格深度证据；
- 确定性任务或同一组冻结 task/result/candidate 回放时，并发 1/并发 N 的正式顺序、时序和 identity 等价；fresh provider/model 输出变化必须体现在正式 identity 中，不能被调度层抹平；
- 所有现有自动测试和新增并发/恢复/媒体测试通过；
- 真实外部和人工验收按 PASS/BLOCKED/SKIP/待确认如实报告；
- `SKILL.md` 和相关 references 已更新为“artifact-first coordinator + JSON 有界 worker；subagent 入口受强隔离 Gate 控制，两个 configured default 均为 1”；
- annotation/preview/scene/clean 聊天关卡仍由 coordinator 保证顺序，CLI 不读取聊天批准；`sceneRender` 当前无条件为 1，Phase 8 未实施；
- 没有绕过内容、样音、完整旁白联合预审、线稿、annotation、区域预览、最终标注、逐幕、clean master 和 final 人工关卡。

## 13. 计划文档自检清单

自检日期：2026-08-15。以下勾选只表示本计划已覆盖对应合同，不表示业务代码、配置或正式项目已经实施。进入每个 Phase 前，实施者仍须按当时工作区状态重新核对。

- [x] 配置权威位置明确为 `config/workspace.local.json`；
- [x] `execution.agents` 与 `execution.concurrency` 已分成独立资源池，默认均为 `1`；
- [x] coordinator 独占用户交互、正式发布、manifest/identity/stale/checkpoint 和批准；worker/subagent 只产 candidate；
- [x] subagent role allowlist 只含 storyboardPlanning、visualReview、annotationDrafting，且 role allowlist 不等于 dispatch 授权；
- [x] 当前共享文件系统 runtime 的强隔离 Gate 明确为 fail-closed；schema、hash inventory 与真实权限隔离没有混淆；
- [x] task/result artifact、路径/SHA/current binding、candidate validator 和串行 fallback 已写清；
- [x] draft/project 的可信 `scopeRoot`、路径解析根与 role 映射一致，建项前不再误用 project `.work`；
- [x] retry 使用稳定 taskId + 独立 attempt 目录，旧 result 不会被覆盖；
- [x] storyboard candidate 已写清 `parse_srt → prompt + cueRange → pre-project generation/timing 校验 → 用户确认 → create_project --plan` 的闭环；`imagePrompt` 不会误入正式 generation plan；
- [x] 每个 attempt 冻结 role-contract.md，task/result 绑定 version/SHA，不读取 live reference；
- [x] prompt 不复制主对话、大 SRT、数组或长日志，上下文隔离目标可测量；
- [x] global visualReview 有 runtime 容量 preflight，超限不伪装成丢失全局视野的并行 review；
- [x] agent×worker 并发乘法、目录扫描、自然语言成功和无证据发布均明确禁止；
- [x] Skill 接入排在基础设施 Phase Gate 之后，方案阶段不改变 current Skill；
- [x] Phase -1 已冻结 P1 合同并定义外部原字节 snapshot、安全 restore 与恢复演练；
- [x] 阶段 0–5 的前半程已单独复核，并明确哪些优化、哪些保持串行；
- [x] 内容草案 stdin/纯函数只读检查和环境批量探测已进入 Phase 0，零写入由文件树快照证明；
- [x] 默认并发明确为 `1`；
- [x] 阶段覆盖、一般范围 `1–16`、bool/未知字段拒绝已写清；`sceneRender` 当前只能为 1；
- [x] 并发不进入正式 identity，但进入 run 审计；
- [x] 已区分“调度参数不入 identity”和“fresh 非确定性候选字节变化必须改变 identity”；
- [x] manifest、timeline、SRT、identity 和批准是单写者；
- [x] 图片与 TTS 的失败策略不同且已写清；两者都使用 candidate/commit/recovery，unknown external outcome 不自动重试；
- [x] 图片单次完整解码和 narration review 证据复用已覆盖；
- [x] annotation 优先去重全局深验，而不是并发重复深验；
- [x] annotation 按幕原子发布与 batch 部分失败不冲突；部分成功仍为 FAIL，不得写全局标注确认或进入全量预览；
- [x] annotation/preview/scene/clean 人工关卡由 coordinator 负责；CLI 只验证技术 current，不读取或推断聊天批准；
- [x] 区域预览批量命令、原子发布和 contact sheet 已覆盖；
- [x] 正式渲染双重编码和双重深验均有明确改造；
- [x] 新媒体精确帧数由统计型 full decode 的 decodedFrameCount 提供；`nb_frames` 只作预检，未削弱累计帧合同；
- [x] 合并、字幕、mux、final 的 deep/binding 边界已写清；
- [x] Phase 8 已降为未来设计备忘，本计划不实现 `sceneRender > 1`，避免 CLI 推断聊天批准；
- [x] 字幕 preset 与 concurrency 的 identity 语义没有混淆；
- [x] 自动 fixture、真实 provider、真实媒体和人工确认边界已分开；
- [x] 非 Git 目录不再以 SHA 清单冒充回退；阶段 snapshot 保存原字节、pre/post manifest 并拒绝覆盖未知用户修改；
- [x] runtime 总 slot/child slot 只转换一次，coordinator 保留槽不存在重复扣减；
- [x] 每个 Phase 都有修改文件、测试和 Phase Gate；
- [x] 明确不做项防止范围膨胀。
