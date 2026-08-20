# 正式 sceneRender 性能基准决策

## 结论

目标机器建议将 `sceneRender=4` 作为本机实测的性能配置；本轮不修改仓库或用户工作区的任何默认配置。`sceneRender=5` 相比 4 的耗时收益很小，但峰值内存明显增加，不作为目标机器建议值。

本轮不实施 ProcessPool worker initializer，也不实施自适应串行阈值。没有 worker 优化落地，因此 before/after 对比标记为“不适用”；本报告中的数据全部是现有正式渲染实现的 before 基准。

原始机器报告：`benchmarks/reports/fixture-medium-before.json`。

## 目标机与 fixture

- 系统：Windows 10 `10.0.26200`，12 个逻辑 CPU。
- Python：3.11.9，`D:\SRTWhiteboard\runtime\.venv\Scripts\python.exe`。
- FFmpeg / FFprobe：8.1.1 essentials build。
- Git：`main@f642d5a9cfc3915bc1da5af48e44065821715b77`。
- fixture：`fixture-medium`，8 幕，每幕 650ms，1920×1080、60fps、固定 annotation、固定 hand；不含真实用户内容或外部 provider。
- fixture SHA-256：`3cc91afb3df682e81d8be0ae157bfdea94867d7441872fcb79d71652810bb1d4`。
- hand SHA-256：`dc054d4eedeb206fa61cd0ec5836647ec99e98d16cdecd0c622d7ee7e9b3b070`。
- cold：从同一只读基线复制出的新项目第一次正式渲染；不声称清空 OS 缓存。
- warm：同一项目、同一参数紧接着第二次正式渲染。

## fixture-medium 原始测量

表中的 RSS 为 runner 对当前进程树采样到的峰值；“峰值 FFmpeg”是采样到的同时在途 FFmpeg 子进程数；FFmpeg 累计启动数在每次运行中均为 8。candidate bytes 是正式 batch 报告的成功 candidate 总字节数，每次均为 590,260 bytes，运行结束后 `.work` 残留均为 0 bytes。

| sceneRender | cold/warm | effective | peak worker | wallMs | wall 秒 | 峰值 RSS | 峰值 FFmpeg | candidate bytes | 状态 |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | cold | 1 | 1 | 79,262.521 | 79.263 | 672.0 MiB | 1 | 590,260 | PASS |
| 1 | warm | 1 | 1 | 76,981.253 | 76.981 | 672.4 MiB | 1 | 590,260 | PASS |
| 2 | cold | 2 | 2 | 46,946.720 | 46.947 | 900.5 MiB | 2 | 590,260 | PASS |
| 2 | warm | 2 | 2 | 45,820.870 | 45.821 | 899.4 MiB | 2 | 590,260 | PASS |
| 4 | cold | 4 | 4 | 29,832.748 | 29.833 | 1,242.7 MiB | 4 | 590,260 | PASS |
| 4 | warm | 4 | 4 | 29,796.284 | 29.796 | 1,243.4 MiB | 4 | 590,260 | PASS |
| 5 | cold | 5 | 5 | 29,119.731 | 29.120 | 1,622.2 MiB | 5 | 590,260 | PASS |
| 5 | warm | 5 | 5 | 29,409.166 | 29.409 | 1,619.9 MiB | 5 | 590,260 | PASS |

关键比较：

- `sceneRender=4` 相比 1，cold wall time 缩短 62.4%，warm 缩短 61.3%。
- `sceneRender=5` 相比 4，cold 只再缩短 2.4%，warm 只再缩短 1.3%。
- 5 相比 4 的 cold 峰值 RSS 增加约 30.5%（1,242.7 MiB → 1,622.2 MiB），FFmpeg 同时在途数也由 4 增为 5。
- 因此 4 是这台 12 逻辑 CPU 目标机上更稳妥的性能/内存平衡点；这是目标机器实测建议，不是跨机器默认值。

## fixture-small 两幕冒烟

small 结果来自 runner/Windows ProcessPool/真实 FFmpeg 链路冒烟；它用于验证短 batch 行为，不替代 medium 决策基准。

| sceneRender | cold wallMs | 峰值 RSS | 峰值 FFmpeg | candidate bytes | identity/SHA/顺序 |
|---:|---:|---:|---:|---:|---|
| 1 | 20,911.582 | 670.6 MiB | 1 | 147,243 | 稳定 |
| 2 | 12,489.975 | 895.4 MiB | 2 | 147,243 | 与串行一致 |

两幕下并发 2 相比串行缩短 40.3%，峰值 RSS 增加 33.5%。这说明当前固定短 fixture 即使只有 2 幕也存在真实并发收益。

## 稳定性与失败策略

- 8 次 medium 运行全部 PASS；每次 effective/peak worker 均未超过 configured。
- 以 `sceneRender=1 cold` 为基线，全部 1/2/4/5 cold/warm 的作品 identity 集合一致。
- 全部正式 MP4 SHA 集合一致，并发配置只改变运行审计，不改变正式输出字节。
- 全部 sceneOrder 与 generation plan 一致。
- failure-policy probe 为 PASS，且明确不是性能样本：注入 `scene-01` worker 失败后，旧 current 保留，其他 7 幕按 generation plan 顺序发布，批次仍为 `FAIL`、`partialSuccess=true`、`approvalWritten=false`，全部正式输出字节保持稳定。

## 优化取舍

### Worker initializer：不实施

数据没有证明 initializer 是本 Cycle 必须承担的优化复杂度：

- medium 的 worker prepare 累计耗时约为 1.11–2.06 秒，而 worker render 累计耗时约为 74.42–112.70 秒，主要成本仍是实际帧渲染与编码。
- cold/warm 在 `sceneRender=4` 时几乎相同（29,832.748ms 与 29,796.284ms，相差约 0.1%），未观察到 warm 能揭示出的显著重复初始化瓶颈。
- 现有实现已经在并发 4 达到相对串行 61%–62% 的 wall time 缩短；继续引入 worker-local hand/config/profile 缓存缺少单独的、可归因的收益证据。

因此不为“完成计划”硬加 initializer，保留当前 `sceneRender=1` 安全降级和既有 worker 合同。

### 自适应串行阈值：不实施

数据反而反对在当前 fixture 范围内自动退化为串行：

- 2 幕 small 的 `sceneRender=2` 比 1 快 40.3%。
- 8 幕 medium 的 2/4/5 都比串行明显更快。
- 当前没有测到“scene 数过少或预计帧数较低时 ProcessPool 启动成本超过并发收益”的交叉点。

因此没有可审计阈值可写入运行策略；不增加未经测量的 scene/frame 门槛。

## Before / after

- Before：`benchmarks/reports/fixture-medium-before.json`，即本报告表格中的现有正式实现数据。
- After：不适用。本轮依据 benchmark 决定不实施 initializer 和自适应串行阈值，因此不存在 worker 优化后的第二组可比较数据。
- 默认配置：未修改。目标机如需性能配置，可显式选择 `sceneRender=4`；其他机器仍须按各自 CPU、内存和负载重新测量。
