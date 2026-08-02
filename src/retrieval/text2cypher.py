"""LLM generation for guarded teacher-side Text2Cypher."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

from retrieval.graphrag_adapter import K12_SCHEMA
from retrieval.models import RetrievalRequest
from utils.llm_client import OpenAIClient


@dataclass
class OpenAIText2CypherGenerator:
    client: OpenAIClient

    def __call__(self, request: RetrievalRequest) -> str:
        prompt = f"""
你是小学数学知识图谱的只读 Cypher 生成器。
只输出一条 Cypher，不要解释，不要 Markdown。

安全要求：
- 只能使用 MATCH、OPTIONAL MATCH、WHERE、WITH、RETURN、ORDER BY、SKIP、LIMIT。
- 禁止 CREATE、MERGE、DELETE、SET、REMOVE、DROP、LOAD CSV 和任何写操作。
- 关系路径必须限定为 1..3 跳。
- 必须包含不超过 {request.top_k} 的数字 LIMIT。
- 返回节点时使用 id、labels(node)[0] AS label、name、properties(node) AS properties。

图谱 Schema：
{K12_SCHEMA}

用户问题：{request.question}
年级：{request.grade or "不限"}
册次：{request.semester or "不限"}
教材版本：{request.edition or "不限"}
""".strip()
        response = self.client.generate(prompt, temperature=0.0, max_tokens=800)
        fenced = re.search(r"```(?:cypher)?\s*(.*?)\s*```", response, re.DOTALL | re.IGNORECASE)
        return (fenced.group(1) if fenced else response).strip()


def generator_from_env() -> Optional[OpenAIText2CypherGenerator]:
    enabled = os.environ.get("K12_TEXT2CYPHER_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not enabled or not api_key:
        return None
    return OpenAIText2CypherGenerator(
        OpenAIClient(
            model=os.environ.get("K12_TEXT2CYPHER_MODEL", "gpt-4.1-mini"),
            api_key=api_key,
            base_url=os.environ.get("OPENAI_BASE_URL") or None,
        )
    )
