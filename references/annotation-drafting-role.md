# annotationDrafting frozen role contract

- 一个宿主 child 可能收到 1–3 个彼此独立、已冻结的 annotation task；必须按 bundle 中的 task sequence 顺序逐个处理，不能把多幕合并为一个 candidate 或 result。
- 宿主 prompt 只是冻结定位器：只接受每个 task/role 的绝对路径与 SHA、允许的 attempt 目录和固定返回格式。不得要求或接收完整主对话、完整 SRT/正文、所有 scene 数组、provider 凭据/配置、批准信息、长工具日志或未冻结状态；这些内容即使宿主能够取得也不得作为判断依据或写入 `agent.log`。
- 每个 task 只读取其 `task.json` 列出的场景图片、`scene-brief.json` 和本 role contract；不得从主对话、其他 scene 或未冻结文件补取业务内容。
- 每幕都必须实际查看原图并读取该幕 brief，先提炼字幕叙事事件，再把叙事顺序和字幕语义映射到实际可见的视觉簇；禁止按叙事名词、对象清单或坐标机械拆分、排序。
- `candidate.annotation.json` 的最小合法结构固定如下；`protectedRegions` 只能位于 `elements[i].reveal.protectedRegions`，不得放在 element 顶层。空保护区统一省略 `protectedRegions`；确有保护区时才写该数组：

```json
{
  "contractVersion": "whiteboard-annotation-visual-elements-v1",
  "elements": [
    {
      "sequence": 1,
      "region": {"x": 120, "y": 140, "width": 760, "height": 620},
      "reveal": {
        "startMs": 0,
        "durationMs": 3200,
        "direction": "top-to-bottom",
        "protectedRegions": [
          {"x": 700, "y": 420, "width": 120, "height": 90}
        ]
      }
    }
  ]
}
```

以下结构非法，必须在交付 candidate 前修正：`elements[i].protectedRegions`、`elements[i].reveal.speed`、未知顶层字段，以及位于画布外的 region/protectedRegions。
- 标注单元按视觉上连续的墨迹簇划分，不按叙事名词或字幕概念数量拆分。同一不可分割主体、共享背景或贯穿性连接结构必须合并为一个 `element`，并在该元素中整体揭示；能够按语义和空间独立呈现的墨迹簇应分别标注。
- 一幕允许只有 1 个元素；当图片实际包含多个可依次呈现的墨迹簇时，优先使用 2–3 个元素，首版不得超过 3 个，也不得为了凑数量强行拆分。
- 元素的 reveal 时间必须严格串行且不得重叠，这是硬约束。空间上的 `region` 不要求绝对没有交集：真实遮挡、主体交界或连续构图需要时可以适度重叠；但任一矩形边界不得横穿另一个视觉簇的有效墨迹，也不得用大框把多个独立簇重新合并。渲染器会从前一元素的允许掩码中扣除后续 region。
- 每幕只写该 attempt 的 `candidate.annotation.json` 和可选 `agent.log`；`result.json` 由 coordinator 在候选 artifact ready 后确定性生成，child 不得创建、修改或补写 result。`candidate.annotation.json` 首选固定为 `{"contractVersion":"whiteboard-annotation-visual-elements-v1","elements":[...]}`，不得手抄 sceneId、duration、timeline SHA、frame range 或 render binding。不得写 `scenes/` 中的正式 annotation、materialized candidate、manifest、identity、stale、checkpoint 或任何人工批准。
- annotation 必须使用 1920×1080 左上角原点整数像素区域；`sequence` 从 1 连续递增，元素使用本幕局部毫秒时钟且彼此串行，最后元素至少在 scene 尾部前 500ms 完成。
- `protectedRegions` 只保护正确分区后，本元素已揭示但仍会被后续区域覆盖的局部主体；它不能替代正确分区，不能用来补救横穿有效墨迹、错误大框或本应合并的连续墨迹。所有 region/protectedRegions 必须位于画布内，reveal 的 `startMs`/`durationMs` 必须为合法整数并与 scene timing 一致。
- sceneId、canvas、sceneDurationMs、timingPlan/renderProfile SHA、sceneFrameRange 与 timingSource 全部由 coordinator 从 current evidence 确定性注入；child 只负责 `elements` 中的视觉区域、顺序、`protectedRegions` 与局部 reveal 判断。
- 每个 `result.json` 使用 `whiteboard-agent-result-v1`（由 coordinator 生成），完整回显该 task 的 identity、task SHA、role SHA、sequence 与全部 frozen inputs；`completed` 时只列出本幕 authored candidate 的 attempt 相对路径及 SHA。child 不需要返回 result 路径。
- child 写完每个 candidate 后必须立即停止该 task；coordinator 必须先执行该 task descriptor 中的 `candidateLint.command`，只有 lint `PASS` 才可让同一 dispatch unit 进入下一 task。lint 失败只补正当前 JSON，不重新附带原图；若错误涉及视觉区域而必须重新看图，只重新加载当前一幕。
- 一个 child 的图片上下文生命周期不得跨 dispatch unit。补正优先 followup 原 child，但 prompt 仍只包含 task/role/candidate 路径、SHA 与精简 validator 错误，不重复嵌入图片或长日志。遇到 `413`/payload too large/context length 后，原 child 视为本次补正不可用；不得原样重试，必须换短上下文 child，并先走 JSON-only 补正。只有 validator 明确指出视觉判断不足时，才给新 child 单独加载当前一幕图片。
- 某一 task 失败时，child 不写 result；coordinator 为该 task 生成 `status=failed` 与合法 error 审计且不伪造 candidate，然后继续 bundle 中后续 task；不得因单幕失败跳过其他独立幕。
- 不得调用图片/文本/语音 provider、FFmpeg、浏览器、正式发布或其他 worker pool；candidate 是否可发布只由 coordinator 的确定性 validator 和 current binding 复核决定。
- candidate 写入并通过本地 JSON 可读性检查后立即返回 `candidate_ready`；普通返回只报告 bundle status、按 sequence 排列的 candidate JSON 绝对路径和不超过 240 字的摘要。自然语言完成声明不能替代 candidate artifact，result 由 coordinator 生成。
