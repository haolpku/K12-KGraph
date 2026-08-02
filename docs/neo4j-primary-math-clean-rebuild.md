# Neo4j 小学数学干净重建记录

执行日期：2026-07-29

## 范围

- Neo4j：Community 5.26.0
- 数据库：`neo4j`
- 唯一数据源：
  `/Users/xiexiaodong/mykg0728/data/K12-KGraph/K12-KGraph/subject_specific_KG/math.json`
- 过滤条件：`subject=数学`、`stage=小学`
- 源文件 SHA-256：
  `00ed25179bb4ad096c53b3965fe84a32afa708850de85db5ea4c9e0bacec8f14`

未混入 `global_KG`、K12-Bench、K12-Train、SFT-Baselines 或演示图。
发布数据的 `afterclass_exercises` 目录没有小学数学文件，因此也未并入。

## 清理与恢复点

清理前数据库导出到：

`/tmp/k12-neo4j-preclean-backup-20260729/neo4j.dump`

备份 SHA-256：

`ca14e5d432f0954ecff1310ec2becaa22793dcd22995515ac558a09d33f8aef7`

随后执行 `MATCH (n) DETACH DELETE n`，并在重新导入前验证数据库为
`0` 节点、`0` 关系。

## 规范化

稳定教材 ID 是教材范围字段的规范来源。例如 `math_4b_rjb` 统一生成：

- `subject=数学`
- `stage=小学`
- `grade=4`
- `semester=下册`
- `edition=人教版`
- `book_id=math_4b_rjb`

这避免了源 Book 节点使用“四年级下册”、知识节点使用 `"4"` 的字段口径
不一致。

生成文件：

- `data/retrieval/primary_math_graph.json`
- `data/retrieval/primary_math_baseline.json`

规范图 SHA-256：

`2d4d37ca8089e4bd161c7dad55d2c89d38aa284e93e1d3b670d6d9396b4b777d`

向量使用 FastEmbed `BAAI/bge-small-zh-v1.5`，共 1,062 个 512 维真实语义
向量。

## 导入后验证

| 检查项 | 结果 |
| --- | ---: |
| 节点 | 1,182 |
| 关系 | 2,303 |
| Book | 12 |
| Chapter | 103 |
| Section | 5 |
| Concept | 551 |
| Skill | 231 |
| Exercise | 280 |
| 非“小学/数学”节点 | 0 |
| 重复 ID | 0 |
| 非 ONLINE 索引 | 0 |
| 向量节点 | 1,062 |
| 向量维度 | 512 |

再次执行同一导入后，节点和关系仍为 `1,182 / 2,303`，证明导入幂等，
不会因重复执行产生重复数据。

检索冒烟验证：

- “乘法分配律在哪里”：命中 `math_4b_rjb_cpt19`，教材位置为四年级
  下册第三章“运算律”。
- “哪些题考察乘法分配律”：命中 `math_4b_rjb_exe7`。
- 学生响应未包含答案、解析、向量、检索文本或内部 Cypher。
- Ruff 和 68 项自动化测试通过。
