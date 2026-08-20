# 正式 sceneRender 性能基准

本目录只提供 Phase 3 的固定合成 fixture、正式渲染 runner 和原始 JSON
报告约定。fixture 不含真实用户内容，不调用图片、TTS 或其他外部 provider。
runner 会创建一次基线项目，再为每个并发值复制相同字节的项目。因此并发值之间
可以比较正式 MP4 SHA 与 render identity；项目准备耗时不计入渲染 `wallMs`。

目标机器上运行 1/2/4/5 的 cold + warm：

```powershell
$envPy = "D:\SRTWhiteboard\runtime\.venv\Scripts\python.exe"
& $envPy benchmarks/run_scene_render_benchmark.py `
  --fixture fixture-medium `
  --concurrency 1 2 4 5 `
  --output benchmarks/reports/fixture-medium-before.json `
  --keep-workspace
```

快速确认 2 幕 fixture：

```powershell
$envPy = "D:\SRTWhiteboard\runtime\.venv\Scripts\python.exe"
& $envPy benchmarks/run_scene_render_benchmark.py `
  --fixture fixture-small `
  --concurrency 1 2 4 `
  --output benchmarks/reports/fixture-small.json
```

## cold / warm 定义

- `cold`：从同一只读基线复制出的新项目第一次正式渲染。
- `warm`：同一项目、同一参数紧接着第二次正式渲染。

这是可复现的“首次运行/紧接重跑”定义，不声称能够清空 Windows 文件系统缓存、
FFmpeg 内部缓存或 CPU 动态频率。若需要跨机器比较，应保留完整环境字段和原始 JSON。

## 指标边界

报告记录 configured/effective/peak/taskCount、runner 与正式 batch 的 wall time、
进程树可测的 peak RSS、运行时观察到的 FFmpeg 子进程峰值、正式 batch 报告的
FFmpeg 启动次数代理、candidate bytes、`.work` 残留字节、正式输出 SHA/identity，
以及 cold/warm 和 concurrency-only 的稳定性比较。当前平台无法测量的指标写成
`null` 并在 `measurementSupport`/`resourceWarnings` 说明，不用 0 冒充已测量值。

runner 默认执行一次受控失败策略 probe：复用已 deep 验证的 fixture candidate，注入
一个 worker 失败并让另一个 candidate 成功，由正式 coordinator 验证 `FAIL`、部分成功、
旧 current 保留、顺序发布以及不写批准。该 probe 不计入性能数据，也不伪装成真实
ProcessPool/FFmpeg 失败。可用 `--no-failure-probe` 明确关闭。

报告不包含固定毫秒 PASS/FAIL 门槛，也不会据未测量的 5/8/16 修改默认并发。
