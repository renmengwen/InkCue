# 多供应商生图与消费验证

本参考说明第一版 OpenAI-compatible `POST /images/generations` 接入。它只支持同步响应，不支持异步任务轮询、自动故障转移或按场景自动切换供应商；独立场景请求和图片消费验证按照工作区 JSON 中的 `imageGeneration` / `imageValidation` 做有界并发。

## 目录

- [工作区与项目边界](#工作区与项目边界)
- [供应商配置](#供应商配置)
- [Generation plan](#generation-plan)
- [JSON 并发配置](#json-并发配置)
- [生成、覆盖与失败重试](#生成覆盖与失败重试)
- [标注前消费验证](#标注前消费验证)
- [线稿文件交接](#线稿文件交接)
- [Global visualReview 初检](#global-visualreview-初检)
- [机器摘要与退出码](#机器摘要与退出码)
- [合并](#合并)

## 工作区与项目边界

正式项目只能在用户确认传统 SRT 模式/分镜策略，或完成 topic/text 内容与制作方案联合确认并生成 source package 后创建。topic/text 的确定性派生与已确认方案一致时不再设置第二次策略确认。默认工作区由 `config/workspace.local.json` 指向 `D:\SRTWhiteboard`，项目位于 `D:\SRTWhiteboard\projects\<项目名>`。配置缺失、D 盘不可用或目录不可写时必须停止，不得回退 C 盘、当前目录或系统临时目录。

项目中的关键文件为：

```text
project.json
source/source.srt
planning/generation-plan.json
scenes/*.png
manifests/generation-manifest.json
manifests/line-art-review-manifest.json
reviews/line-art-review-<identity前12位>.md
previews/*
output/final.mp4
.work/<运行 ID>/*
```

创建项目：

```powershell
<ENV_PY> scripts/create_project.py --name <项目名> --srt <字幕.srt> `
  --plan <已确认策略.json> --voiceover-mode disabled
```

`--plan` 指向已经确认的 generation plan。未提供时只创建空场景的计划骨架，必须补全并校验计划后才能生图。

## 供应商配置

复制 `config/image-providers.example.json` 为 `config/image-providers.local.json`，再填写真实密钥。密钥只保存在 local 配置中，不放入命令行、generation plan、manifest 或日志。

每个命名供应商必须声明：

- `protocol`: 第一版固定为 `openai-images-generations`。
- `baseUrl`: 包含 API 版本前缀；程序只追加一次 `/images/generations`。
- `apiKey`、`model`。
- `request.size`、`request.responseFormat` 和 `request.timeoutSeconds`。
- `download.timeoutSeconds` 与 `download.maxBytes`。
- 可选 `extraBody`；不得覆盖 `model`、`prompt`、`n`、`size`、`response_format`。

正式路径必须省略 `--provider`，直接使用当前 local 配置的 `activeProvider`。只有用户明确要求临时覆盖 active provider 时才传 `--provider <provider-name>`；该覆盖只影响当前运行，不改写配置。一次运行只使用一个供应商，不会自动切换 backup。

```powershell
<ENV_PY> scripts/generate_images.py --project <项目根目录>

```

也可以选择另一份绝对路径的 local 配置：

```powershell
<ENV_PY> scripts/generate_images.py --project <项目根目录> `
  --config D:\secure\image-providers.local.json
```

若 local 配置属于 Git 仓库，它必须已被 Git 忽略；未忽略时脚本以退出码 `3` 拒绝请求。配置不属于任何 Git 仓库时会明确警告无法证明忽略状态，但可继续。

## Generation plan

`planning/generation-plan.json` 冻结本次画布、全局视觉提示和场景顺序。第一版固定要求：

- topic/text 内容草案使用 `imagePrompt`，formal generation plan 的 scene 字段固定为 `prompt`；用户批准 current 草案后，只能由 coordinator 做 `imagePrompt` → formal `prompt` 的确定性映射，文本与 scene 顺序保持不变。正式 plan 不接受 `imagePrompt`，provider 也不直接消费内容草案。

- `outputCanvas` 为 `1920×1080`、`#F5EBD7`、`contain`。
- `constraints.forbidText` 严格为 `true`。
- `globalPrompt` 非空，并完整包含 Skill 的统一视觉规范。
- 每幕 `sceneId` 唯一、`sceneDurationMs` 为正整数。
- `outputFile` 是不含目录和 `..` 的唯一 `.png` 文件名。

请求提示词按以下格式确定性拼接：

```text
<globalPrompt>

场景要求：
<scene.prompt>
```

每个非空 scene 的 `prompt` 必须是去空白后非空字符串。缺失或空白 prompt 会在读取 provider 配置、创建 client 或发出任何 provider 请求前以 plan 错误拒绝，禁止只用 `globalPrompt` 静默继续。

每幕都是不共享对话或上一张图片的独立 provider 请求。每个 scene `prompt` 必须在本条内重复该图成立所需的画布、纸张、线稿、配色、按该幕实际语义确定的造型锚点、构图、留白和禁字/禁水印要求；不得使用“延续”“沿用”“同上”“上一幕”“参照前图”等跨请求指代。运行时虽然会确定性拼接 `globalPrompt`，但这不能作为省略 scene 自包含约束的理由。

### 场景与提示词视觉拓扑

generation plan 的 scene 划分、独立视觉簇、贯穿性结构、prompt 自包含约束和
annotation 可消费性统一由 [`prompt-writing.md`](prompt-writing.md) 定义。本文只规定
provider 消费与发布行为；该规范不增加 schema 字段，也不改变 provider 请求、人工线稿
确认或图片 identity。

### 下游 annotation 分区边界

生图构图必须满足 [`prompt-writing.md`](prompt-writing.md) 的 annotation 可消费性；
实际 region、reveal 串行性和 `protectedRegions` 仍由 annotation 阶段按 current 图片判断，
不得从 prompt 机械派生。允许掩码公式、时序 schema 与渲染算法保持不变。

## JSON 并发配置

图片链只读取 `config/workspace.local.json` 中已经由共享 loader 严格校验的配置：

```json
{
  "execution": {
    "concurrency": {
      "default": 1,
      "imageGeneration": 4,
      "imageValidation": 4
    }
  }
}
```

字段缺失时从 worker pool 的 `default` 继承，整个 pool 缺失时为 `1`。worker pool 与 `execution.agents` 独立且不做乘法；agent task 不得再启动 provider worker。配置只决定本机执行策略，不进入图片 identity。每次生成/验证摘要都记录 `configuredConcurrency`、`effectiveConcurrency` 与 `taskCount`；无论 worker 完成顺序如何，manifest 与校验摘要始终按 generation plan 场景顺序提交。

## 生成、覆盖与失败重试

跨阶段状态、identity、stale、attempt 恢复、自动重试与
`unknown_external_outcome` 的权威规则见
[`recovery-and-identity.md`](recovery-and-identity.md)。本节只描述图片阶段的命令、
checkpoint 名称和 provider 错误分类。

独立 provider 请求可有界并发；单幕明确失败不会停止其它幕，也不会删除已经成功发布的场景。worker 只处理 provider 请求、下载、完整解码与规范化，并且只能原子写已预登记 attempt 下的 `candidate.png` 和去敏 `candidate-receipt.json`。worker 不得写正式 `scenes/*.png`、generation manifest 或批准状态。

coordinator 是 generation manifest 的唯一 writer。每个 attempt 固定经过：

```text
prepared → requesting → candidate_ready → publishing → validated
```

coordinator 先冻结 `attemptId`、image input identity、candidate/receipt/formal 相对路径与 overwrite 决定并持久化 `prepared`；`requesting` 后 worker 才能调用 provider。candidate 通过 receipt、input identity、SHA、bytes、PNG 解码与元数据重验后，coordinator 串行写 `candidate_ready`、`publishing`，再把 candidate 复制到正式目录同目录临时文件、flush/fsync、原子替换并核对正式 SHA/bytes。只有 `validated` checkpoint 成功落盘后才清理 candidate。

```powershell
# 默认拒绝覆盖已有 PNG
<ENV_PY> scripts/generate_images.py --project <项目根目录>

# 显式替换全部目标幕；替换失败不破坏旧有效 PNG
<ENV_PY> scripts/generate_images.py --project <项目根目录> --overwrite

# 只处理 manifest 中外部结果明确 failed 的场景
<ENV_PY> scripts/generate_images.py --project <项目根目录> --retry-failed

# 失败幕已有旧文件且用户明确允许替换时，只覆盖失败幕
<ENV_PY> scripts/generate_images.py --project <项目根目录> --retry-failed --overwrite
```

同一项目的 `generate_images.py` 运行由 `.work/image-generation.lock` 做跨进程互斥。
如果上一条命令仍在 provider 请求或下载，第二条命令会以
`image_generation_in_progress` 返回且不发起新的 provider 请求；应先等待第一条命令
输出最终 JSON，再按 manifest 状态恢复。这样可以避免两个 coordinator 并发写 manifest，
把仍在进行的请求误判为最终失败或重复发起新 attempt。进程异常退出后，只有在锁内 PID
已确认不存在时才会清理陈旧锁。

### 全片内容驱动封面（可选）

在场景图片成功发布后，可在同一阶段显式传入 `--cover` 生成独立社交平台封面：

```powershell
<ENV_PY> scripts/generate_images.py --project <项目根目录> --cover
```

封面不是 scene 源图，也不进入 `generation-plan.json` 的普通场景集合。`cover_generation.py` 从全片 `topic/body`（或 source SRT）、全部 narration cues，以及 formal generation plan 中每个 scene 的 `coreIdea`、`visualSubject`、`prompt` 汇总语义，固定记录 `semanticSource=whole_video`。标题和副标题由本地确定性排版完成；provider 不可用或没有正式场景图时，使用暖米黄白板画布或已有 scene 图作为 fallback。

成功产物为：

- `previews/social-cover.png`（1920×1080）；
- `manifests/cover-manifest.json`（封面 SHA、语义输入快照、`coverFrameRange`、`visualReviewExcluded=true`）。

后续成片封装可将封面替换到最终静音视频的第 0 帧，保持总帧数、音频和字幕时间轴不变；当前首版不把封面作为 scene 渲染或 annotation。单幕/成品的视觉语义检查应排除 manifest 声明的 `coverFrameRange`，避免封面文字触发“场景源图禁字”误判；H.264 解码、尺寸、fps、帧数、SHA、streams 和完整性等技术检查仍必须包含该帧。

网络错误、超时、HTTP 408、429 和 5xx 最多自动尝试 3 次，并使用指数退避和少量抖动。400、401、403、404、非法响应、图片安全约束失败和路径冲突不自动重试。

崩溃恢复只使用 manifest 登记状态和确定路径，不扫描 `.work` 猜结果：

- `prepared/not_started` 可以安全派发；
- `requesting` 且完整 candidate/receipt 存在时重验并采用，provider 调用数为 0；
- `requesting` 且 candidate/receipt 不完整时进入 `unknown_external_outcome`，禁止自动重试；
- `candidate_ready` / `publishing` 从 candidate 或已发布且 SHA/bytes 相同的正式文件继续，provider 调用数为 0；
- `validated` 且 candidate 尚未清理时只完成清理，不把旧结果伪报成本次新生成；
- `failed` 只有在外部结果明确失败时才允许 `--retry-failed` 新建 attempt。

正式目标已存在且未传 `--overwrite` 时，所有 overwrite 冲突在创建 client/worker 前统一拒绝，本次 provider 调用数为 0；旧 validated 记录和旧图片保持不变。`unknown_external_outcome` 需要用户明确决定是否承担新的外部请求，不能由 `--retry-failed` 绕过。

供应商返回 `b64_json` 或 `url` 均可；二者同时存在时优先 `b64_json`。URL 下载不携带供应商 Authorization Header。图片按真实字节解码，保持比例 contain 到 `1920×1080`，居中补 `#F5EBD7`，不裁切、不拉伸。成功结果重新打开验证并计算 SHA-256 后才原子替换正式 PNG。

所有临时数据进入项目 `.work/<运行 ID>`。只有与 `validated` checkpoint 绑定的 candidate 才清理；恢复所需 attempt 保留。摘要额外报告 `adoptedCandidateCount` 与 `unknownExternalOutcomeCount`，且不保存 API Key、完整 provider 响应、临时 URL 或绝对正式路径。

## 标注前消费验证

每次进入第 3 步标注前都必须重新执行：

```powershell
<ENV_PY> scripts/validate_generated_images.py --project <项目根目录> `
  --review-policy user_first
```

验证器先串行冻结 project/plan/manifest 全局合同，再按 `imageValidation` 对独立 PNG 有界并发。每张 PNG 只在同一个 `Image.open()` 周期执行一次完整 `load()`，并在该周期检查 format/mode/1920×1080 与截断、CRC/解码损坏；随后核对文件 SHA，禁止 `verify()` 后重新打开造成双重解码。

验证器检查：

- `project.json`、generation plan 和 manifest 的 schema 与 `projectId`。
- manifest 记录的 generation plan SHA-256 与当前文件一致。
- 每幕状态为 `validated`，输出文件仍位于项目 `scenes` 目录。
- PNG 能完整打开、模式/格式可消费、实际尺寸为 `1920×1080`。
- 实际图片 SHA-256 与 manifest 一致。
- 失败幕不会进入可消费集合。

脚本向标准输出写单个 JSON 摘要。`validated` 只表示文件技术上完整且与计划一致，不能代替第 2 步结束后的用户明确确认。只有技术验证通过且用户确认画面语义和视觉质量后，才可开始标注。

## 线稿文件交接

全量图片技术验证 PASS 后，`validate_generated_images.py` 必须在同一次运行中确定性生成：

- `manifests/line-art-review-manifest.json`：current 技术证据，绑定 project、generation plan SHA、generation manifest SHA、场景顺序，以及每张 current PNG 的相对路径、SHA 和 bytes；输出 `lineArtReviewIdentitySha256`，但不写人工批准。
- `reviews/line-art-review-<identity前12位>.md`：面向用户的有序审阅文件，按 scene ID 展示 current 图片、全分辨率相对链接和必要的场景语义/提示词。它只是由 manifest identity 派生的审阅视图，不是第二份机器权威源。

coordinator 面向用户只能交付可点击 review 文件链接、完整 identity、场景计数和异常 scene 摘要；不得把全部 PNG、完整提示词或 Markdown 全文重新嵌入主聊天。`user_first` 时 coordinator 不为介绍文件而逐张打开图片；`agent_first` 时 visualReview child 在新鲜短上下文中查看 current PNG，把完整意见留在 attempt 的 `findings.json/result.json`，coordinator 只接收结果路径、status、validator 状态和精简摘要，不得重复逐图审阅。

用户仍须回到聊天，以 current identity 明确确认全部线稿，或按 scene ID 指出需要修改的幕。打开 Markdown、点击原图、技术 PASS、child completed、findings 无问题或用户没有反对都不构成批准。generation plan、generation manifest、场景顺序或任一 PNG 字节变化都会生成新 identity 和新 review 文件；旧文件可保留为历史证据，但旧聊天确认不得用于 current bundle。进入 annotation 前必须复核用户确认的 identity 与 current `line-art-review-manifest.json.identityHash` 一致。

### 可选 Global visualReview 初检

技术验证始终执行；图片完成后必须显式选择直接交用户，或先准备 global review：

```powershell
<ENV_PY> scripts/validate_generated_images.py --project <项目根目录> `
  --review-policy user_first

<ENV_PY> scripts/validate_generated_images.py --project <项目根目录> `
  --review-policy agent_first
```

`user_first` 不创建或派发 visualReview，在机器摘要记录 `reviewPolicy=user_first`、`semanticReview.status=skipped_by_user`、`approvalWritten=false` 和 `userConfirmationRequired=true`，随后直接交付 current 线稿 review 文件。`agent_first` 通过现有 `whiteboard-agent-task-v1` 合同创建并重验 `visualReview` task：task 冻结 generation plan、generation manifest、全部 current PNG/SHA、role contract 与 current bindings；只允许写 attempt 内 `findings.json/result.json`，`formalWritesAllowed=false`、`approvalWritesAllowed=false`。旧 `--prepare-visual-review` 保留为 `agent_first` 兼容入口。

命令返回 `visualReview.spawnPackage`，其中 `spawnAgentCall` 已包含宿主真实 `spawn_agent` 所需的短 task name、`fork_turns:none` 和最小冻结 prompt。`preparedOnly:true`、`hostSpawnExecuted:false`、`peakChildAgents:0` 明确表示 Python 只准备了 attempt，没有创建 child、没有伪造 agentId。coordinator 收到该包后应立即调用宿主协作工具，不得再阅读 Python 源码重新研究派发方式；真实 agent/task 标识只能在宿主派发后补入审计。child 或 fallback coordinator 都必须实际具备 `viewImage`，否则报告 `BLOCKED`。

visualReview findings 只用于提示跨幕人物、配色、纸张、构图漂移和建议重点重生成的 scene；它不修改图片、不调用 provider、不写 generation manifest、不重试生图，也绝不写线稿批准。即使 result 为 completed，仍须等待用户逐图明确确认。

同一策略也用于后续两个视觉 bundle，但不改变各阶段的生成职责：`generate_annotation_previews.py --all --review-policy user_first|agent_first` 只控制 preview 生成后的额外 AI 复查，`annotationDrafting` 仍必须查看原图；`scene_review.py --review-policy user_first|agent_first` 在全部 current 单幕形成一次有序 bundle 后决定是否准备预审，`agent_first` 每幕只抽首帧、中段和完成帧等少量关键帧。两者的 `agent_first` 同样只准备宿主 spawn package，不自动批准；技术验证和人工批准始终保留。

## 机器摘要与退出码

生成和验证脚本都输出机器可读 JSON 摘要，不输出密钥、完整 API 响应或临时 URL。生成摘要包含配置/实际并发、task 数、采用 candidate 数和 unknown 外部结果数；验证摘要包含配置/实际并发、task 数，并固定输出 `userConfirmationRequired: true`、`approvalWritten: false`。全量技术 PASS 时还输出 `lineArtReview.reviewFile`、`lineArtReview.manifestFile`、`lineArtReview.lineArtReviewIdentitySha256` 与 `lineArtReview.sceneCount`；审阅文件生成失败时整次验证不得报告成功或启动 visualReview。

- `0`：全部目标场景成功，或全部待消费图片验证成功。
- `1`：批量已执行，但至少一幕失败。
- `2`：参数、工作区、项目、配置、计划或 manifest 无效。
- `3`：敏感配置安全检查失败。

空场景计划不会发出请求。它会输出 `total=0` 的可预测摘要并以退出码 `2` 停止，避免把空计划误报为生成成功；应先把已确认策略写入 generation plan。

## 成片链路中的技术合并

全部正式单幕按 generation plan 有界并行渲染、逐幕技术验证并按 plan 顺序发布，但不在每幕之间等待用户，也不逐幕重复 AI 视觉复查。全部 current scene 由 `scene_review.py --review-policy user_first|agent_first` 形成一次有序 review bundle 并输出 `sceneReviewIdentityHash`；`agent_first` 只准备一次少量关键帧预审的宿主 spawn package。用户一次明确确认后，`approve_scene_review.py` 把批准持久化为 `manifests/render-manifest.json.sceneReviewApproval`。bundle 绑定 generation plan SHA、sceneOrder、timing plan file/SHA/activeTimeline、render profile SHA，以及逐幕 render identity、MP4 SHA/bytes/frameRange；`merge_scenes.py` 必须在创建 concat 列表或候选前硬校验该批准，缺失、stale、scene 集合或输入顺序不符时返回 5。输入、输出必须属于同一项目；FFmpeg concat 列表只写入该项目本次 `.work/merge-<运行 ID>` 目录。clean master 是字幕烧录所需的内部技术工件，不是独立人工关卡。

```powershell
<ENV_PY> scripts/merge_scenes.py --project <项目根目录> `
  --inputs <幕1.mp4> <幕2.mp4>
```

脚本发布并验证 `output/final-video-only.mp4`；新 clean candidate 的同一次 full decode 产生 `decodedFrameCount` receipt，发布后只做 SHA/bytes binding，不重复深验。脚本结束时只删除本次 concat 列表和本次运行目录，不扫描其它 `.work` 内容。技术验证通过后立即继续字幕烧录和按需音频封装，不展示 clean master，也不单独等待用户确认。
