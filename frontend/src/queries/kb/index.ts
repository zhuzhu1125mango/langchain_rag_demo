/**
 * 知识库相关 API Query/Mutation 封装（CODE-1 拆分后为 barrel）。
 *
 * 基于 Vue Query 管理知识库、文档、分类、标签、搜索、推荐、质量评估与知识图谱
 * 等接口的缓存与变更。按领域拆为 8 个子模块，本文件统一再导出，
 * `@/queries/kb` 的既有导入路径全部保持不变。
 */
export * from './documents'
export * from './knowledgeBases'
export * from './taxonomies'
export * from './learning'
export * from './feedback'
export * from './experiments'
export * from './processingConfig'
export * from './wiki'
