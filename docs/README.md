# 文档索引

> 命名与组织方式参考 Django / Vue / FastAPI 等项目惯例：核心参考置于顶层，其余按性质分目录；文件名使用小写 kebab-case。

## 目录结构

```
docs/
├── README.md            # 本索引
├── architecture.md      # 架构总览：服务拓扑、RAG 决策管线、代码地图
├── api.md               # API 参考：全部接口说明
├── guide/               # 操作指南
│   ├── deployment.md    #   部署（开发/生产环境）
│   ├── development.md   #   开发与测试（含踩坑记录）
│   └── monitoring.md    #   监控运维（指标、告警、处置）
├── design/              # 设计文档（对标 RFC）
│   ├── improvement-roadmap.md      # 后续优化执行计划（当前路线图）
│   ├── rag-enhancement.md          # RAG 检索增强设计
│   ├── intent-router-upgrade.md    # 意图路由升级设计
│   ├── search-optimization.md      # 搜索优化设计
│   ├── price-trustworthiness.md    # 价格类实时数据可信度设计
│   ├── llm-wiki-compile.md         # LLM-Wiki 编译层设计（RAG 前置知识编译增强）
│   ├── frontend-test-plan.md       # 前端测试与 E2E 设计（P2-6）
│   └── severe-fixes.md             # 严重问题修复设计
└── archive/             # 归档（一次性报告）
    └── code-review-2026-08.md      # 代码审查报告（已全部修复）
```

## 按场景查找

| 场景 | 阅读顺序 |
|---|---|
| 新人上手 | 根 [README](../README.md) → [architecture.md](architecture.md) → [guide/deployment.md](guide/deployment.md) |
| 本地开发/跑测试 | [guide/development.md](guide/development.md) |
| 排查线上问题 | [guide/monitoring.md](guide/monitoring.md) → [guide/development.md](guide/development.md) 踩坑记录 |
| 查接口 | [api.md](api.md)（另有 Swagger UI: `http://localhost:8000/docs`） |
| 了解技术决策/规划 | [design/improvement-roadmap.md](design/improvement-roadmap.md) → `design/` 其他文档 |
