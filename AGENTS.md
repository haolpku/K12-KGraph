# K12-KGraph 工程协作规则

本文件适用于整个 `K12-KGraph/` 项目。子目录中的 `AGENTS.md` 可以增加更具体的规则，但不得放宽本文件的安全、凭据和数据保护要求。

## 项目目标与范围

- 先区分用户目标、当前方案和可选方案，再实施架构、权限、数据模型或主要接口变更。
- 重要改动开始前，简要说明影响范围、可能受影响的模块和验证方式。
- 保持代码、部署配置、OpenAPI 和 `docs/` 文档一致；发生偏离时必须记录偏离点、原因和影响。
- 研究评测结果必须注明数据来源。图谱自动生成的评测集不得描述为教研人工验收结果。

## 目录职责

- `src/retrieval/`：Neo4j 检索后端、API、路由、安全策略和 Provider Adapter。
- `src/kg/`：知识图谱抽取、合并和数据流水线。
- `src/benchmark/`：K12-Bench 构建代码。
- `src/sft_qa/`：训练数据构建代码。
- `src/frontend/`：后续前端代码；不存在时按需创建，不得混入后端模块。
- `tests/`：单元、集成、安全和契约测试。
- `eval/`：可复现评测集、执行器和结果格式。
- `docker/`：容器、Schema、导入和运维脚本。
- `docs/`：架构、部署、评测和接口文档。
- `config/retrieval.env.example`：无真实凭据的配置模板。
- `config/retrieval.env`：本地私有运行配置，不得提交。
- 无论文件位于何处，凡修改检索相关的 `src/retrieval/`、`tests/retrieval/`、`eval/retrieval/`、`docker/`、`config/` 或检索文档，均必须同时遵守 `src/retrieval/AGENTS.md` 中的检索安全不变量。

## Python 与 uv 环境

- 项目统一使用根目录 `.venv`，不得在 `src/`、`src/retrieval/` 或其他源码目录内创建虚拟环境。
- 使用 Python 3.11 创建环境：

  ```bash
  uv venv .venv --python 3.11
  source .venv/bin/activate
  uv sync --frozen
  ```

- 运行 Python、测试和工具时优先使用 `uv run`；不得依赖或修改全局 Python 环境。
- `pyproject.toml` 是直接依赖声明的唯一真值源，`uv.lock` 是完整解析结果并必须提交。修改依赖后运行 `uv lock`，再用 `uv sync --frozen` 验证。
- `requirements.txt` 和 `requirements-dev.txt` 仅作为 Docker 或旧流程的兼容导出文件，不得手工编辑。分别使用 `uv export --frozen --no-dev --no-hashes --no-emit-project -o requirements.txt` 和 `uv export --frozen --all-groups --no-hashes --no-emit-project -o requirements-dev.txt` 重新生成。
- 修改依赖时同步检查 Docker 镜像，并验证兼容导出文件与 `uv.lock` 一致。
- 不得仅为局部跑通而引入新的依赖；确有必要时说明用途、维护成本和替代方案。

## 凭据与配置

- 真实密码和 API Key 只允许存在于未跟踪的 `config/retrieval.env` 或受控外部 Secret Manager。
- 不得把真实凭据写入源码、测试、Markdown、示例配置、Git、日志、命令输出或对话回复。
- 不得读取后完整打印 `.env`；诊断时只检查变量是否存在，不显示变量值。
- `config/retrieval.env.example` 只能包含空值或明显占位符。
- 在对话、日志或提交中暴露过的 Key 必须视为已泄露并建议立即轮换。
- 本地私有配置建议使用 `chmod 600 config/retrieval.env`。

## Neo4j 与数据安全

- 未经用户明确授权，不得清空、替换或批量修改已有 Neo4j 数据、索引、约束和持久化卷。
- E2E 和破坏性验证必须使用独立 Compose 项目、独立卷和非默认宿主机端口。
- 删除测试卷前必须确认卷名属于当前隔离项目；不得对工作区根目录或不明确路径执行递归删除。
- 数据导入、向量重建和索引变更应提供可重复命令及验证结果。

## 文档和接口一致性

- 新增或修改公开 API、请求/响应类型、认证、授权、路由或部署方式时，同步更新 `docs/` 中对应文档。
- 前端对接以 OpenAPI、`src/retrieval/models.py` 和仓库内正式文档 `docs/E2E-backend.md` 的实际契约为准。
- 父目录的 `../docs/E2E-backend.md` 仅作为兼容镜像，不得作为独立真值源；接口变更时先更新仓库内正式文档，再同步镜像或将镜像改为指向正式文档的说明。
- 文档中的状态必须区分：已实现并验证、仅配置存在、计划能力、生产环境待办。

## 最低验证要求

修改检索后端后，至少运行：

```bash
uv run pytest -q tests/retrieval
uv run ruff check src/retrieval tests/retrieval eval/retrieval docker/scripts
uv run python -m compileall -q src/retrieval eval/retrieval docker/scripts
docker compose \
  --env-file config/retrieval.env.example \
  -f docker/docker-compose.neo4j-retrieval.yml \
  config --quiet
```

- 先运行与改动直接相关的测试，再运行完整检索测试。
- 涉及 Docker、Neo4j、GraphRAG 或公开 API 的改动，应在隔离环境执行健康检查和相关 E2E。
- 未实际运行的验证不得描述为通过；无法运行时明确说明原因和剩余风险。

## 工作区与变更纪律

- 工作区可能已有用户改动；不得覆盖、回滚或格式化无关文件。
- 优先沿用现有模块和工具，保持改动小、可审查、可回滚。
- 不使用 `git reset --hard`、`git checkout --` 等破坏性命令，除非用户明确要求并确认目标。
- 完成后报告变更文件、验证证据、已知限制和仍需外部条件的事项。
