# Neo4j 检索后端规则

本文件适用于 `src/retrieval/`。同时遵守项目根目录和 `src/` 的上级规则。

## 架构边界

- `api.py` 负责 HTTP、认证依赖、请求观察和状态映射，不承载具体检索算法。
- `service.py` 负责统一编排、合并、去重和回退。
- `router.py` 与 `intent_classifier.py` 负责意图识别，不直接访问 Neo4j。
- `cypher.py` 只承载受控参数化模板。
- `retrievers.py` 与 `graphrag_adapter.py` 负责全文、向量、GraphRAG 和图扩展。
- `store.py` 是 Neo4j 驱动边界；公开请求不得直接获得 Driver 或 Session。
- `policy.py`、`auth.py` 和 `security.py` 是安全边界，不得为方便测试而绕过。
- `teacher_analysis.py` 只接受结构化计划并静态编译参数化 Cypher。

## V1 API 不变量

- 规范入口为 `POST /v1/retrieval/search`。
- V1 请求使用严格 Schema，未知字段返回 422。
- 客户端不得提交或控制 `user_type`、`include_answers`、`route` 或内部工具选择。
- 身份、角色和权限只来自可信认证上下文。
- 一个请求最多处理两个意图；路径深度最多 3 跳；`top_k` 最大 20。
- 无结果、需要澄清、安全拒绝和后端不可用必须使用不同的原因码，不得伪造知识点或证据。

## Cypher 和教师分析安全

- 用户文本只能通过参数传入 Cypher，不得拼接进查询结构。
- 公开检索只允许参数化模板、受控 GraphRAG 查询和静态编译的教师分析计划。
- V1 运行时不得执行模型自由生成的 Cypher。
- 禁止写入、删除、Schema 修改、外部加载、未明确允许的 `CALL`、无界路径和超过 3 跳的路径。
- TeacherAnalysisPlan 必须先经过严格 Pydantic Schema，再由静态编译器生成查询。
- 教师分析必须同时满足可信教师角色和显式权限；能力缺失时失败关闭。

## 数据泄露防护

- 学生端不得返回答案、解析、解题过程、Embedding、`search_text`、模型提示词、内部 Cypher 或内部工具信息。
- 响应字段使用“角色 × 节点类型”允许列表；未知图属性默认丢弃。
- 练习题的学生检索文本不得包含答案和解析。
- 审计日志不得记录原始问题、原始主体标识、Authorization Header 或 API Key；使用哈希或 HMAC 标识。

## 全文、向量与 GraphRAG

- Concept、Skill、Exercise 使用独立全文和向量索引。
- 同一向量索引内不得混用不同模型、维度或 Provider 的向量空间。
- 切换 Embedding 模型时必须重建全部可检索节点向量，并验证数量、维度和索引状态。
- `hash` Embedding 只用于契约和离线 E2E，不得用于证明语义相关性或生产检索质量。
- GraphRAG 异常可以回退到本地全文/向量融合，但必须记录结构化警告和指标。
- Neo4j 5.26 的范围过滤采用受控过召回后过滤；调整候选倍率前必须重新评测召回率和延迟。

## 修改后的验证

根据改动范围运行对应测试，并至少覆盖：

- 请求和响应 Schema；
- 单意图与多意图路由；
- 参数化 Cypher 与 3 跳上限；
- 学生字段泄露防护；
- 教师分析非法计划和无权限路径；
- GraphRAG 回退与 Neo4j 不可用；
- Embedding 维度和 Provider Adapter；
- Docker 隔离环境 HTTP E2E。

不得使用图数据自动生成的评测结果证明方案已经通过独立教研验收。
