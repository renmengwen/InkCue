# SRT 白板动画：`/images/generations` 多供应商与 D 盘项目工作区设计

日期：2026-08-14  
状态：设计已在对话中批准，等待书面设计复核  
适用 Skill：`srt-whiteboard-animation`

## 1. 背景

当前 Skill 在用户确认配图策略后要求“生成线稿”，但没有定义实际生图入口、供应商配置、请求协议、响应解码、图片验证、生成记录或后续消费契约。运行时只能依赖当时可用的代理工具临时生图，因而无法稳定切换供应商，也无法证明目录中的 PNG 是本次计划生成、经过验证且可以进入标注流程的图片。

此外，Skill、Python 虚拟环境以及默认项目产物都可能位于 C 盘。图片、视频、Python 依赖和临时文件会持续扩大 C 盘占用。用户要求在第 1 步配图策略确认后才正式创建项目，并把项目和其他重资产放入 D 盘专用目录。

## 2. 目标

本次改造需要同时达成以下目标：

1. 为 Skill 增加可执行、可测试的 OpenAI-compatible `POST /images/generations` 生图入口。
2. 第一版支持多个命名供应商，通过配置切换 `baseUrl`、`apiKey`、`model` 和兼容参数。
3. 不在主流程中硬编码供应商名称。
4. 同时消费 `data[].b64_json` 和 `data[].url`。
5. 将成功响应转为经过验证的本地 PNG，并生成可审计但不含密钥的 manifest。
6. 只有技术验证通过且获得用户明确确认的图片才能进入标注流程。
7. 用户确认第 1 步配图策略后，才在 `D:\SRTWhiteboard` 下创建正式项目。
8. 项目、图片、视频、manifest、临时文件和 Python 运行环境均放在 D 盘；C 盘只保留体积较小的 Skill 代码、固定资源和本地配置。
9. 保持现有七步人工确认关卡以及标注、预览、流式白板渲染和多幕合并行为。

## 3. 非目标

第一版明确不实现：

- Seedream 等异步任务提交和轮询协议。
- Google、Replicate 等非 `/images/generations` 协议。
- 自动供应商故障转移。
- 同一次生成任务按场景自动选择供应商。
- 并发生图。
- 供应商名称对应的代码分支。
- 从环境变量、系统凭据库或其他目录自动猜测 API Key。
- 自动裁切或拉伸供应商返回的图片。
- 仅凭 HTTP 200、文件存在或 manifest 的技术状态绕过用户确认。
- 自动删除 C 盘上可能已经存在的旧 `.venv`。
- 初始化新的 Git 仓库。

未来接入异构协议时，应新增协议 adapter，同时复用本设计中的项目、generation plan、标准化 PNG、manifest 和消费验证协议。

## 4. 总体架构

```text
workspace.local.json
        +
image-providers.local.json
        ↓
用户确认第 1 步配图策略
        ↓
ProjectWorkspace 创建 D 盘项目
        ↓
generation-plan.json
        ↓
ProviderConfigLoader
        ↓
ImagesGenerationsClient
        ↓
ImageResultDecoder
        ↓
ImageNormalizer + ImageValidator
        ↓
SceneImageStore（原子落盘）
        ↓
generation-manifest.json
        ↓
validate_generated_images.py
        ↓
用户确认线稿
        ↓
现有 annotation / preview / render / merge 流程
```

边界原则：

- 供应商层只负责请求和返回图片数据。
- 图片层负责真实解码、16:9 归一化、验证和安全落盘。
- manifest 层负责证明图片与计划、文件和哈希一致。
- Skill 工作流负责用户确认，技术状态不能代替人工批准。

## 5. D 盘工作区

### 5.1 默认根目录

用户批准的默认根目录为：

```text
D:\SRTWhiteboard
```

派生结构：

```text
D:\SRTWhiteboard\
  projects\
    <项目名>\
      project.json
      source\
        source.srt
      planning\
        generation-plan.json
      scenes\
        scene-01-<名称>.png
        scene-01-<名称>.annotation.json
        scene-01-<名称>-whiteboard.mp4
      manifests\
        generation-manifest.json
      previews\
        scene-01-<名称>-annotation-preview.png
        scene-01-<名称>-preview.mp4
      output\
        final.mp4
      .work\
        <运行 ID>\
  runtime\
    .venv\
    cache\
      pip\
    tmp\
```

`scenes` 同时保存 PNG 和同名 annotation JSON，以兼容现有预览台按目录加载 `<名称>.png` 与 `<名称>.annotation.json` 的行为。

### 5.2 本地工作区配置

新增：

```text
config/workspace.example.json
config/workspace.local.json
```

`workspace.example.json`：

```json
{
  "schemaVersion": 1,
  "workspaceRoot": "D:\\SRTWhiteboard"
}
```

运行时默认读取 `workspace.local.json`。若配置缺失、路径不是绝对路径、目标盘不可用或目录不可写，任务必须失败，不得退回 Skill 目录、用户主目录、当前目录、`%TEMP%` 或 C 盘其他位置。

### 5.3 重资产边界

以下内容必须位于 D 盘：

- 原始 SRT 的项目副本。
- generation plan 和 manifest。
- 生成图片、标注、预览图、单幕视频和最终视频。
- HTTP 下载、Base64 解码、图片归一化和 FFmpeg concat 列表等临时文件。
- Python 虚拟环境和运行缓存。
- pip 安装过程使用的下载缓存和临时解包目录。

以下小型固定内容可以继续位于 C 盘 Skill 目录：

- `SKILL.md`。
- Python 源码。
- `preview.html`。
- `drawing-hand.png`。
- references、示例配置和本地配置。

### 5.4 运行环境

`prepare_env.py` 从 `workspace.local.json` 解析：

```text
<workspaceRoot>\runtime\.venv
```

成功时仍输出：

```text
ENV_PY=D:\SRTWhiteboard\runtime\.venv\Scripts\python.exe
```

创建或更新虚拟环境时，`prepare_env.py` 必须把 pip 缓存显式指向 `<workspaceRoot>\runtime\cache\pip`，并把 pip 子进程的 `TEMP`、`TMP` 显式指向 `<workspaceRoot>\runtime\tmp`。不得依赖 Windows 默认用户临时目录。

旧 Skill 目录中的 `.venv` 不自动删除。实现交付时只报告其存在与否；后续清理需要单独授权。

## 6. 项目创建

### 6.1 创建时机

第 1 步只解析 SRT、提出配图策略并等待确认，不创建项目。

用户明确确认配图策略后，执行：

1. 读取并验证 `workspace.local.json`。
2. 验证 D 盘工作区可用且可写。
3. 创建唯一项目目录。
4. 将原始 SRT 复制为 `source/source.srt`。
5. 写入 `project.json`。
6. 将已经确认的策略写为 `planning/generation-plan.json`。
7. 校验 generation plan。
8. 进入第 2 步生图。

“确认第 1 步配图策略”是正式创建项目的授权边界。

### 6.2 项目脚本

新增：

```text
scripts/create_project.py
scripts/project_workspace.py
```

调用：

```powershell
<ENV_PY> scripts/create_project.py --name <项目名> --srt <原始字幕.srt>
```

成功后输出稳定、可捕获的绝对路径：

```text
PROJECT_ROOT=D:\SRTWhiteboard\projects\<项目名>
PLAN_PATH=D:\SRTWhiteboard\projects\<项目名>\planning\generation-plan.json
SCENES_DIR=D:\SRTWhiteboard\projects\<项目名>\scenes
```

### 6.3 项目名和冲突

- 保留可读中文。
- 将 Windows 禁止字符替换为 `-`。
- 去除结尾的点和空格。
- 清理后为空则拒绝创建。
- 目标项目已存在时默认拒绝复用或覆盖。
- 只有显式 `--resume <项目目录>` 才能续接已有项目。
- `--resume` 必须校验 `project.json`、项目 ID 和原始 SRT SHA-256。
- 不自动追加随机数字掩盖冲突。

### 6.4 `project.json`

```json
{
  "schemaVersion": 1,
  "projectId": "创建时生成并永久保存的 UUID v4",
  "projectName": "示例项目",
  "createdAt": "2026-08-14T12:00:00+08:00",
  "source": {
    "file": "source/source.srt",
    "sha256": "..."
  },
  "paths": {
    "planning": "planning",
    "scenes": "scenes",
    "manifests": "manifests",
    "previews": "previews",
    "output": "output",
    "work": ".work"
  }
}
```

项目内部路径一律相对项目根目录。脚本解析后必须验证最终路径仍位于项目根目录内。

## 7. 多供应商配置

### 7.1 文件

新增：

```text
config/image-providers.example.json
config/image-providers.local.json
```

`.gitignore` 加入：

```gitignore
config/image-providers.local.json
config/workspace.local.json
config/*.local.json
```

`image-providers.local.json` 直接保存真实 `apiKey`。不使用 `api_key_env`，也不接受命令行密钥参数。

默认读取 Skill 内的 `config/image-providers.local.json`。`generate_images.py` 同时支持 `--config <绝对路径>` 选择另一份本地供应商配置，但该文件仍必须通过同等的忽略状态和凭据脱敏检查。

### 7.2 配置结构

```json
{
  "schemaVersion": 1,
  "activeProvider": "primary",
  "providers": {
    "primary": {
      "protocol": "openai-images-generations",
      "baseUrl": "https://api.example-a.com/v1",
      "apiKey": "replace-with-real-key",
      "model": "image-model-a",
      "request": {
        "size": "1792x1024",
        "responseFormat": "b64_json",
        "timeoutSeconds": 180
      },
      "download": {
        "timeoutSeconds": 120,
        "maxBytes": 52428800
      },
      "extraBody": {
        "quality": "standard"
      }
    },
    "backup": {
      "protocol": "openai-images-generations",
      "baseUrl": "https://api.example-b.com/v1",
      "apiKey": "replace-with-real-key",
      "model": "image-model-b",
      "request": {
        "size": "1536x1024",
        "responseFormat": "url",
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

### 7.3 配置规则

- `activeProvider` 必须指向现有供应商。
- `--provider` 可以覆盖 `activeProvider`，但不能修改配置文件。
- `protocol` 第一版只能为 `openai-images-generations`。
- `baseUrl` 必须包含 API 版本前缀；脚本规范化结尾斜杠并只追加一次 `/images/generations`。
- `apiKey`、`model` 和 `request.size` 不能为空。
- `responseFormat` 支持 `b64_json` 或 `url`。
- 第一版固定 `n=1`。
- `extraBody` 不得覆盖 `model`、`prompt`、`n`、`size` 或 `response_format`。
- 一次运行只使用一个供应商。改用 backup 必须由用户或代理显式指定新的 `--provider`。

### 7.4 凭据安全

- 不在命令行、generation plan、manifest 或正常日志中保存 API Key。
- 不打印完整供应商配置。
- 异常输出中与当前 API Key 相同的子串替换为 `[REDACTED]`。
- 若 Skill 位于 Git 仓库，生图前使用 Git 验证实际 local 配置已被忽略；未忽略则拒绝请求。
- 对 `--config` 指向的文件，在其所在路径向上查找 Git 仓库；若属于仓库则必须被该仓库忽略。
- 若实际配置不属于任何 Git 仓库，明确警告无法由 Git 证明忽略状态，但允许继续。

## 8. Generation Plan

### 8.1 位置

```text
<项目根目录>\planning\generation-plan.json
```

### 8.2 结构

```json
{
  "schemaVersion": 1,
  "projectId": "与 project.json 一致",
  "outputCanvas": {
    "width": 1920,
    "height": 1080,
    "background": "#F5EBD7",
    "fit": "contain"
  },
  "globalPrompt": "统一视觉、配色、材质、构图和无文字约束",
  "constraints": {
    "forbidText": true
  },
  "scenesDirectory": "scenes",
  "manifestFile": "manifests/generation-manifest.json",
  "scenes": [
    {
      "sceneId": "scene-01",
      "name": "核心概念",
      "subtitleRange": {
        "startMs": 0,
        "endMs": 30000
      },
      "sceneDurationMs": 30000,
      "prompt": "这一幕的主体、关系、动作和叙事表达",
      "outputFile": "scene-01-核心概念.png"
    }
  ]
}
```

最终请求提示词确定性拼接为：

```text
<globalPrompt>

场景要求：
<scene.prompt>
```

### 8.3 校验

- `projectId` 必须与 `project.json` 一致。
- 输出画布第一版固定为 `1920×1080`、`#F5EBD7`、`contain`。
- `globalPrompt` 不能为空；Skill 按统一视觉规范生成它，不让脚本通过自然语言关键词猜测语义约束。
- `constraints.forbidText` 第一版必须严格为 `true`，用于机器校验不得生成文字这一工作流约束已经被冻结。
- `sceneId` 唯一。
- `sceneDurationMs` 为正整数。
- 场景顺序严格使用数组顺序。
- `outputFile` 只能是 `.png` 文件名，不能是绝对路径、包含目录或包含 `..`。
- 不允许多个场景指向同一输出文件。
- generation plan 不保存密钥或供应商凭据。

## 9. 生图组件

### 9.1 文件职责

```text
scripts/generate_images.py
scripts/image_generation.py
scripts/validate_generated_images.py
```

- `generate_images.py`：CLI、批量调度、重试、摘要和退出码。
- `image_generation.py`：配置加载、HTTP、响应解码、图片归一化、验证、原子落盘和 manifest。
- `validate_generated_images.py`：标注前重新验证计划、manifest 和图片。

### 9.2 请求

```http
POST {normalizedBaseUrl}/images/generations
Authorization: Bearer <apiKey>
Content-Type: application/json
```

```json
{
  "model": "<provider.model>",
  "prompt": "<globalPrompt + scene.prompt>",
  "n": 1,
  "size": "<provider.request.size>",
  "response_format": "<provider.request.responseFormat>"
}
```

### 9.3 响应

支持：

```json
{"data":[{"b64_json":"..."}]}
```

和：

```json
{"data":[{"url":"https://..."}]}
```

规则：

- `data` 必须为非空数组。
- 第一版只消费 `data[0]`，因为请求固定 `n=1`。
- 两个字段同时存在时优先消费 `b64_json`。
- Base64 严格解码。
- URL 仅允许 HTTP 或 HTTPS。
- URL 下载默认不携带供应商 Authorization Header，防止将密钥发送到第三方存储域名。
- 不信任 HTTP `Content-Type`，必须按实际字节解码图片。
- 空文件、HTML、JSON 错误页、超限响应和不可解码数据均失败。
- 不持久化完整原始 API 响应正文。

## 10. 16:9 归一化和图片验证

### 10.1 固定输出

```text
尺寸：1920×1080
背景：#F5EBD7
模式：contain
格式：RGB PNG
```

### 10.2 算法

1. 解码供应商原图并读取真实宽高。
2. 保持比例计算能够完整放入 1920×1080 的最大尺寸。
3. 高质量缩放。
4. 在剩余区域填充 `#F5EBD7`。
5. 居中放置。
6. 不裁切、不拉伸。
7. 写临时 PNG。
8. 重新打开并验证临时 PNG。
9. 计算 SHA-256。
10. 原子替换正式 PNG。

### 10.3 安全限制

- 原图宽、高均不得小于 512 像素。
- 按 `contain` 放入 1920×1080 后，缩放图覆盖面积必须不低于最终画布面积的 55%；低于该阈值视为极端长宽比并失败。正方形图片覆盖 56.25%，因此可以完整补边消费；常见竖图会失败。
- 原始总像素数不得超过 40,000,000；超过上限时在完整解码前拒绝，防止图片解压炸弹。
- 下载字节数不得超过供应商配置的 `maxBytes`。
- manifest 记录原始尺寸、缩放尺寸、偏移、最终尺寸和是否补边。
- 不用本地规则声称已经可靠检查“无文字”“非写实”等语义质量；这些仍由生成后的用户确认关卡负责。

## 11. 临时文件和原子落盘

本次运行的所有临时文件位于：

```text
<项目根目录>\.work\<运行 ID>\
```

示例：

```text
scene-01.response.part
scene-01.decoded.part
scene-01.normalized.png.part
```

规则：

- 不使用系统默认 `%TEMP%`。
- 只有临时 PNG 重新打开成功后才能原子替换正式文件。
- 任一步骤失败时删除本次场景的临时文件并保留已有正式图片。
- 正常结束时只清理本次运行 ID 目录。
- 不递归清理其他运行或其他项目。
- 异常中断留下的目录在下次启动时报告，不擅自删除。
- `merge_scenes.py` 的 FFmpeg concat 列表也改为使用项目 `.work`。

## 12. Manifest

### 12.1 位置

```text
<项目根目录>\manifests\generation-manifest.json
```

### 12.2 顶层字段

- `schemaVersion`
- `projectId`
- generation plan 相对路径和 SHA-256
- `runs` 数组；每次完整生成或失败重试追加一个运行记录，不覆盖历史记录
- 每个 run 的运行 ID、供应商名称、协议、模型、开始时间、完成时间和退出结果
- manifest 的创建、更新时间和完成时间
- 场景总数、成功数和失败数
- 各场景结果

manifest 不使用单一顶层 provider 表示全部场景，因为用户可能显式选择 backup 重试失败幕。最终供应商以每幕记录为准，`runs` 保留每次人工选择的运行历史。

### 12.3 场景字段

- `sceneId`
- `outputFile`
- `status`
- `provider`
- `model`
- 最终提示词及 SHA-256
- 原始尺寸
- 归一化尺寸、缩放尺寸和偏移
- 图片 SHA-256
- `b64_json` 或 `url` 消费来源
- 尝试次数
- 创建时间
- 失败阶段
- 已脱敏错误摘要

状态只允许：

```text
pending
requesting
decoding
normalizing
validated
failed
```

脚本异常退出后，残留非终态场景在下一次恢复时按可重试失败处理，不能视为成功。

## 13. 正确消费契约

标注前必须运行：

```powershell
<ENV_PY> scripts/validate_generated_images.py --project <项目根目录>
```

验证内容：

1. `project.json` 和 manifest schema 有效。
2. generation plan SHA-256 与 manifest 一致。
3. `projectId` 在三个文件中一致。
4. 待消费场景状态为 `validated`。
5. 输出文件存在且位于 `scenes` 内。
6. 图片可以完整打开。
7. 实际尺寸为 1920×1080。
8. 实际 SHA-256 与 manifest 一致。
9. 没有失败幕被误加入可消费集合。

验证脚本输出机器可读 JSON 摘要并用退出码控制后续流程。

必须区分：

```text
validated：文件在技术上完整、尺寸正确、与计划一致。
用户确认：画面在语义和视觉上允许进入标注。
```

二者缺一不可。manifest 的 `validated` 状态不能替代第 2 步结束后的用户明确确认。

## 14. CLI

### 14.1 准备环境

```powershell
python scripts/prepare_env.py --check
python scripts/prepare_env.py
```

### 14.2 创建项目

```powershell
<ENV_PY> scripts/create_project.py --name <项目名> --srt <字幕.srt>
```

### 14.3 生成全部场景

```powershell
<ENV_PY> scripts/generate_images.py --project <项目根目录> --provider primary
```

省略 `--provider` 时使用 `activeProvider`。

使用另一份本地供应商配置：

```powershell
<ENV_PY> scripts/generate_images.py --project <项目根目录> --config <供应商配置绝对路径> --provider primary
```

### 14.4 覆盖已有图片

```powershell
<ENV_PY> scripts/generate_images.py --project <项目根目录> --provider primary --overwrite
```

### 14.5 重试失败场景

```powershell
<ENV_PY> scripts/generate_images.py --project <项目根目录> --provider primary --retry-failed
```

### 14.6 验证消费

```powershell
<ENV_PY> scripts/validate_generated_images.py --project <项目根目录>
```

### 14.7 退出码

- `0`：所有目标场景成功，或所有待消费图片验证成功。
- `1`：批量执行完成，但至少一个场景失败。
- `2`：参数、工作区、项目、配置、计划或 manifest 无效。
- `3`：敏感配置安全检查失败。

## 15. 失败、重试和覆盖

### 15.1 自动重试

最多尝试 3 次，并使用指数退避和少量抖动：

- 网络连接失败。
- 请求超时。
- HTTP 408。
- HTTP 429。
- HTTP 500–599。

### 15.2 不自动重试

- HTTP 400、401、403、404。
- 工作区、配置、项目或 generation plan 错误。
- 非法 Base64。
- URL 内容无效。
- 图片无法解码。
- 图片不满足安全约束。
- 输出路径冲突。
- 已有文件且未使用 `--overwrite`。

### 15.3 批量行为

- 严格按 generation plan 数组顺序串行生成。
- 单幕失败后继续后续幕。
- 成功图片立即安全落盘，不因其他幕失败而删除。
- 只要有失败幕，最终退出码为 `1`。
- `--retry-failed` 只处理失败或异常中断的幕。
- `validated` 场景不会被重试覆盖。
- generation plan 哈希变化后不得使用旧 manifest 直接重试。
- 不自动改用 backup。

### 15.4 覆盖

- 目标图片已存在时默认拒绝覆盖。
- `--overwrite` 才允许替换。
- 提示词或计划变化但旧图片仍存在时，不把旧图伪报为新结果。
- 覆盖失败不得破坏已有有效图片。

## 16. 七步工作流整合

### 第 1 步：读字幕、出策略

- 保持当前字幕解析和 25–35 秒分幕策略。
- 不创建项目、不生成图片。
- 等待用户明确确认。
- 用户确认后创建 D 盘项目、复制 SRT、写 `project.json` 和 generation plan。

### 第 2 步：通过命名供应商生成线稿

1. 选择本次唯一供应商。
2. 执行 `generate_images.py`。
3. 检查退出码和 manifest。
4. 执行 `validate_generated_images.py`。
5. 展示成功线稿并明确列出失败幕。
6. 等待用户确认线稿。

若存在失败幕，保留成功结果并只重试失败幕。未全部完成前不进入整批标注，除非用户明确缩小任务范围。

### 第 3 步：标注并打开预览台

进入条件：

- manifest 重新验证通过。
- 图片 SHA-256 未改变。
- 图片为 1920×1080。
- 用户已经明确确认对应线稿。

满足后执行现有字幕阅读、图片查看、annotation 创建、预览台打开和目录加载流程。预览台加载项目的 `scenes` 目录。

### 第 4–7 步

保持现有区域预览、预览台调整、流式白板渲染和多幕合并行为。输出路径改为 D 盘项目目录；临时文件改为项目 `.work`。

## 17. Skill 文件调整

实现阶段预计新增或修改：

```text
.gitignore
SKILL.md
agents/openai.yaml                  # 仅在界面元数据过时时更新
config/workspace.example.json
config/image-providers.example.json
references/image-generation.md
scripts/create_project.py
scripts/project_workspace.py
scripts/generate_images.py
scripts/image_generation.py
scripts/validate_generated_images.py
scripts/prepare_env.py
scripts/merge_scenes.py
tests/test_project_workspace.py
tests/test_image_generation.py
```

不创建额外 README、安装指南或变更日志。

## 18. 测试设计

### 18.1 工作区和项目

- 默认解析 `D:\SRTWhiteboard`。
- 缺少本地配置时失败且不回退 C 盘。
- D 盘不可写时失败。
- 项目只在策略确认后的工作流步骤创建。
- 项目名清理和冲突拒绝。
- `--resume` 校验项目和 SRT 哈希。
- 所有项目路径阻止目录穿越。
- 临时文件进入项目 `.work`，不进入 `%TEMP%`。
- `prepare_env.py` 返回 D 盘解释器路径。
- pip 缓存、`TEMP` 和 `TMP` 均进入 D 盘 `runtime` 子目录。

### 18.2 供应商配置

- 多供应商选择。
- `activeProvider` 和 `--provider` 覆盖。
- 未知供应商失败。
- 缺少必填字段失败。
- 不支持的协议失败。
- `extraBody` 覆盖核心字段失败。
- local 配置未被 Git 忽略时拒绝。
- 非 Git 目录产生警告但允许执行。
- 日志和 manifest 不含真实 API Key。

### 18.3 本地协议服务器

- `b64_json` 成功。
- `url` 成功。
- 两者同时存在时优先 Base64。
- 空 `data`。
- 缺失图片字段。
- 非法 Base64。
- URL 返回空内容、HTML、JSON 或超限响应。
- 请求和下载超时。
- 401 不重试。
- 429 和 500 重试。
- 重试耗尽。

### 18.4 图片

- 16:9 图片归一化不产生多余补边。
- 3:2 图片完整缩放并补边。
- 不裁切、不拉伸。
- 极端比例、超大像素和损坏数据失败。
- 临时文件验证后原子落盘。
- 失败不损坏已有图片。

### 18.5 Manifest 和消费

- 全部成功和部分失败。
- 异常中断后的非终态恢复。
- 失败幕定向重试。
- 成功幕不被覆盖。
- generation plan 哈希变化后拒绝旧 manifest。
- 图片缺失、损坏、尺寸变化或手工修改后验证失败。

### 18.6 现有链路回归

至少验证：

```text
本地模拟 /images/generations
→ D 盘测试项目
→ 归一化 PNG
→ manifest validated
→ 消费验证通过
→ 示例 annotation.json
→ render_annotation_preview.py
→ render_stream_whiteboard.py 短视频
```

测试产生的项目使用 `<workspaceRoot>\.test-runs\<测试运行 UUID>`，不用系统临时目录。验证结束后只清理本次测试 UUID 对应的已解析绝对路径；清理前必须确认目标仍位于 `.test-runs` 内。

### 18.7 Skill 校验

```powershell
python C:\Users\MOVER\.codex\skills\.system\skill-creator\scripts\quick_validate.py `
  C:\Users\MOVER\.codex\skills\srt-whiteboard-animation
```

### 18.8 真实供应商冒烟

若用户已经写入有效 `image-providers.local.json` 且安全检查通过，使用一个命名供应商生成一张测试图，并验证到短视频输出。

若没有有效本地配置，不猜测或索取其他项目的凭据；真实冒烟明确标为“未执行”，本地模拟协议和完整消费链路仍需通过。

## 19. 完成标准

只有全部满足以下条件才算第一版完成：

1. 可以配置并选择多个命名 `/images/generations` 供应商。
2. 没有供应商名称硬编码。
3. `url` 和 `b64_json` 均能正确消费。
4. 输出无裁切、无拉伸地归一化到 1920×1080。
5. 项目只在第 1 步策略确认后创建。
6. 项目、运行环境和临时重资产位于 `D:\SRTWhiteboard`。
7. D 盘不可用时不回退 C 盘。
8. 默认不覆盖已有图片。
9. 部分失败保留成功结果，并能只重试失败幕。
10. API Key 不进入命令行、generation plan、manifest 或日志。
11. manifest 能阻止缺失、损坏、尺寸错误、计划不一致或已修改的图片进入标注。
12. 用户确认关卡保持有效。
13. 现有标注、预览、渲染和合并行为没有回归。
14. 自动测试和 Skill 校验通过。
15. 真实供应商冒烟通过，或因缺少有效本地配置被明确标记为未执行。

## 20. 版本管理说明

当前目录 `C:\Users\MOVER\.codex\skills\srt-whiteboard-animation` 不是 Git 仓库，因此本设计文档可以写入和复核，但不能在当前目录提交。实现过程不会擅自执行 `git init`。
