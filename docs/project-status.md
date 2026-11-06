# 项目状态（已归档）

> **本台账于 2026-11-07 结项冻结，不再更新。**
>
> - 全文（417 行，含 P0-1~P2-3 全部条目、DEPLOY 记录、红线表）：[archive/project-status-2026-11-07.md](archive/project-status-2026-11-07.md)
> - 结论（做到了什么 / 没做什么 / 遗留风险）：[FINAL-REPORT.md](FINAL-REPORT.md)

## 为什么归档而不是继续维护

这份台账是**开发过程账本**，不是对外说明：它按「一次一批修复」的顺序累积了 400+ 行，
每行都带提交号与验收证据。它的价值在于「追溯某个决定是什么时候、因为什么做的」，
而这个价值随时间只增不减——所以冻结保留，不删不改。

结项时点的事实：P0-1 ~ P2-3 全部 ✅，无「未完成」项。剩下的三类内容不在这份台账里：

| 类别 | 去哪看 |
|---|---|
| 有意不做的事（多租户/HA/桌面端/企业级） | [FINAL-REPORT.md §3](FINAL-REPORT.md) |
| 诚实的负面结论与遗留风险 | [FINAL-REPORT.md §4](FINAL-REPORT.md) |
| 不再排期的冻结清单 | [FINAL-REPORT.md §6](FINAL-REPORT.md) |

`docs/design/` 下的历史设计文档里若有指向本页的链接（`../project-status.md`），
按上面的归档地址理解即可——那些文档描述的是当时的方案，台账描述的是当时的进度，
两者都不该在结项后被追改。

## 归档前的最后状态（2026-11-07 00:30）

| 维度 | 状态 |
|---|---|
| 测试 | 后端 1691 passed / 117 skipped；前端 154 passed |
| CI | 7/7 绿 |
| 生产 | 11 容器全 healthy、`/health/ready` 四项 ok、117 路由 |
| 数据 | 4 篇文档 / 532 chunk；三方一致性巡检 `issues 0` |
| 上线版本 | DEPLOY-5（UX-5 搜索/筛选/导出） |
| 回滚源 | `rag-backend:rollback-20261107` / `rag-frontend:rollback-20261107` |
