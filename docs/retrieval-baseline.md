# Neo4j 检索实施基线

记录日期：2026-07-28

## 仓库现状

- 当前分支：`main`
- 实施前工作树：干净
- 原项目形态：Python 离线知识图谱、Benchmark 与 SFT 数据流水线
- 原项目依赖：OpenAI SDK、PyYAML、NumPy、FastEmbed
- 原项目未提供：Neo4j 驱动、图数据库导入器、检索服务、Web API、Docker 部署和自动化测试

## 可用数据

初始代码仓库只有 `demo/kg/math_7a_rjb.json`。用户随后提供了同级目录
`/Users/xiexiaodong/mykg0728/data/K12-KGraph` 中的数据发布仓库，因此
基线已更新：

- 小学数学节点：1,182
- 小学数学关系：2,303
- 人教版教材：一年级至六年级、上/下册，共 12 本
- Concept / Skill / Exercise：551 / 231 / 280
- Chapter / Section：103 / 5
- 数据许可：CC-BY-NC-SA-4.0；本次仅研究测试

规范化报告保存在 `data/retrieval/primary_math_baseline.json`，图数据保存在
`data/retrieval/primary_math_graph.json`。这两个生成物位于 gitignore
范围，不会把大体积向量数据提交到代码仓库。

## Neo4j 环境

- 目标兼容版本：Neo4j 5.26 LTS
- 实测版本：Neo4j Community 5.26.0
- Java：21
- 在线导入：1,182 节点、2,303 关系
- 约束、范围、CJK 全文和 512 维向量索引：全部 ONLINE
- Docker 客户端已安装，但 Docker daemon 当前不可用；Compose 配置渲染已验证

临时测试密码未写入仓库。Community 测试实例没有验证 Enterprise/Aura
只读角色，实际部署应使用配置文件中的只读账户变量。

## 安全基线

- 仓库中不保存真实数据库密码或模型 API Key。
- 学生检索文本不得包含 `answer` 或 `analysis`。
- Text2Cypher 仅允许只读语句，并必须通过关键字、单语句、超时和结果数量限制。
