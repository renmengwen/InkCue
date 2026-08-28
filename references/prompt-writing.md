# 提示词与视觉拓扑合同

合同版本：`whiteboard-prompt-writing-v2`

本文件是白板动画视觉拓扑和提示词写作的唯一规范来源。阶段 0 的
`contentDrafting` 使用 `imagePrompt`，传统 SRT 分镜和正式图片计划使用
`prompt`；`references/content-input.md`、`references/image-generation.md` 以及
各 role contract 只保留阶段绑定和 schema 摘要，出现冲突时以本文件为准。

## 1. 适用范围与不变量

这些规则约束内容草案、SRT storyboard candidate、正式 generation plan 以及
后续 annotation 可分区性。它们不增加 JSON 字段，不改变 provider 协议，不改变
线稿、annotation 联合或 scene review 的既有 Gate 与批准动作，也不授权 child 写正式文件。

- 每幕只表达一个核心视觉命题；scene 边界由视觉状态、因果阶段、构图中心或
  必须独立呈现的结果决定，不按名词数量或固定秒数机械切幕。
- 一幕可以包含多个可依次揭示的视觉簇；只有确实不可分割的连续构图才合并。
- 视觉簇必须能由后续 annotation 以完整、局部的矩形 region 表达，不能预先制造
  交错、无法分区或依赖全局遮罩才能成立的构图。
- 规则描述空间关系，不硬编码固定对象清单、人物类别或“每幕必须出现”的物件。
  具体主体、造型锚点和动作由该幕已确认语义决定。

## 2. 视觉拓扑

每个 scene 的 prompt 必须明确画布、纸张、线稿/配色、该幕语义主体、构图、留白、
画内文字策略和禁水印要求，并且在本条内自包含。provider 请求彼此独立，不共享对话或前一张
图片；不得使用“延续”“沿用”“同上”“上一幕”“参照前图”等跨请求指代。

### 2.1 独立视觉簇

- 独立簇之间保留真实、连续、干净的纸面留白；局部接近不等于必须合并。
- 一个簇可以是一个主体及其不可分割的共享背景、底面或连接结构。
- 当字幕语义包含多个可以先后出现的状态/主体时，优先规划 2–3 个可独立揭示
  的簇；不得为了凑数量强拆，也不得因为边界接近把本可独立呈现的簇全部合并。
- prompt 中可以描述静态阅读方向，但不得把绘制顺序、`sequence`、坐标或
  `protectedRegions` 写成生图 provider 的控制指令。

### 2.2 贯穿性结构

禁止用跨区域贯穿性结构把本来可以独立揭示的视觉簇连接成一个整体。道路、河流、
山脉、箭头、光束、长线、连续背景和共同底面只是解释性示例，不是全局固定禁用对象。
如果该结构本身就是本幕不可分割的核心语义，可以保留，并将其与相关主体作为一个
整体簇规划。不能用大框、连续底色或长线掩盖本应分开的有效墨迹。

### 2.3 annotation 可消费性

规划完成后应能回答：每个视觉簇的边界在哪里、是否有连续墨迹跨越其他簇、是否需要
局部保护。annotation 仍由实际图片决定：按连续墨迹簇划分而不是按叙事名词逐项建框；
一幕允许 1 个元素，实际有多个簇时优先 2–3 个，首版最多 3 个。`protectedRegions`
只保护正确分区后不可避免的遮挡，不能补救横穿墨迹、错误大框或应合并的连续构图。

## 3. 字段与确定性映射

| 所处合同 | 字段 | 谁生成/修改 | 下游用途 |
| --- | --- | --- | --- |
| `whiteboard-content-draft-v1` | `scenes[].imagePrompt` | `contentDrafting` candidate | 阶段 0 审阅 |
| 传统 SRT storyboard candidate | `scenes[].prompt` | `storyboardPlanning` candidate | 正式 plan 候选 |
| `planning/generation-plan.json` | `scenes[].prompt` | coordinator 确定性派生/发布 | provider 请求 |

topic/text 草案获用户明确确认后，coordinator 对同一 `sceneId` 按原顺序执行唯一映射：

```text
content draft scenes[i].imagePrompt
  → formal generation plan scenes[i].prompt
```

映射只复制规范化后的提示词文本，不由 child、provider 或人工复制时二次改写；正式
generation plan 不得保留 `imagePrompt`，内容草案也不得提前把字段改名为 `prompt`。
`globalPrompt` 可以按既有实现确定性拼接，但不能替代 scene prompt 的自包含约束。

## 4. 提示词最小合同

一个合格的 scene prompt 至少说明：

1. 1920×1080、暖米黄纸张和极简黑色手绘基调（公共视觉 preset 可由
   `globalPrompt` 提供，但 scene 仍不得依赖跨请求上下文）；
2. 当前旁白对应的单一核心命题、主体和必要的动作/状态；
3. 主体之间的空间关系、独立留白以及可被后续 annotation 分开的墨迹簇；
4. `constraints.forbidText=false`（默认）时允许语义需要的画内文字，并明确其准确内容；
   不得因图片含文字本身判错，但应避免乱码、拼写错误、意外文字、供应商水印和无关品牌标志。
   `constraints.forbidText=true` 只作为旧项目或显式计划的可选要求；正式字幕仍由后期统一烧录，
   不要求图片复刻整句字幕；
5. 不引用其他 scene、完整 SRT、主对话、provider 配置、凭据或长日志。

以下写法不合格：

- 只有“画一个关于拖延的场景”之类的抽象短语；
- 使用“沿用上一幕人物/颜色”“按上一个 prompt 继续”等隐式上下文；
- 把多个独立簇用一条道路、箭头、河流或共同底色连接，除非该结构就是不可分割
  的核心语义；
- 把 `sequence`、时间戳、坐标、批准状态或正式文件路径当作生图指令；
- 将凭据、临时 URL、完整 provider 响应、完整主对话或未经冻结的素材塞进 prompt。

## 5. 与质量 Gate 的边界

提示词通过 schema/确定性 validator 只代表 candidate 技术可读；不代表内容、构图或
审美获批。流程必须保持：

```text
candidate → schema validator PASS → coordinator current publish
→ 当前批准主体真实审阅 current 线稿 identity → 调用现有批准动作 → 才能进入 annotation
```

`agentApprovalEnabled` 缺失或为 `false` 时，当前批准主体是用户；为 `true` 时，
coordinator AI 必须真实查看 current 线稿、决定通过或返工，并仅在通过时调用现有
批准动作。该选择只改变批准主体，不删除 Gate，也不增加 identity、manifest、状态机或
专用恢复协议。

修改 `imagePrompt`/`prompt` 会产生新的 generation-plan identity、图片和视觉下游
状态；旧 review 或旧聊天确认不得用于新 current。技术 PASS、child completed、
visual findings、用户没有反对或 AI 未报告异常都不能单独写线稿批准。

## 6. 角色上下文最小化

child 只读取冻结的 `task.json`、`role-contract.md`、允许的 scene brief/图片和 attempt
目录。prompt 不复制完整主对话、完整 SRT/正文、全部 scene 数组、provider 配置或
凭据、完整日志、批准信息。child 只返回候选路径和短摘要；正式 generation plan、
manifest、identity、stale、checkpoint 与批准仍由 coordinator 单写。

## 7. 复核清单

提交 candidate 前按顺序检查：

1. 每幕只有一个核心命题，scene 边界不是按名词/固定时长机械切分；
2. 每个 prompt 自包含且没有跨请求指代；
3. 独立簇之间有真实纸面留白，贯穿结构只有在不可分割语义下保留；
4. 构图可由 1–3 个连续墨迹簇完整分区；
5. prompt 不含时间轴、坐标控制、凭据、长上下文或批准声明；
6. topic/text 使用 `imagePrompt`，正式 plan 使用 `prompt`，映射由 coordinator 完成；
7. 默认文字策略允许画内文字；复核其语义、准确性和可读性，不把“出现文字”本身列为异常；
8. 候选通过 validator 后仍停在 current/待批准主体真实审阅，不越过质量 Gate。

命令和 provider 失败/恢复语义分别见
[`image-generation.md`](image-generation.md)；stale、identity、retry 和
`unknown_external_outcome` 的唯一规则见
[`recovery-and-identity.md`](recovery-and-identity.md)。
