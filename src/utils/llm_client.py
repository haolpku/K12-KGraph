"""Shared LLM client helpers used by pipeline modules."""

from __future__ import annotations

import base64
import json
import mimetypes
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


def image_to_data_url(path: Path) -> str:
    """Encode a local image as a ``data:`` URL for multimodal chat requests."""
    mime, _ = mimetypes.guess_type(path.name)
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime or 'image/jpeg'};base64,{data}"


class LLMClient(ABC):
    @abstractmethod
    def generate(self, prompt: str, **kwargs: Any) -> str:
        raise NotImplementedError

    @abstractmethod
    def parse_response(self, response: str) -> Dict[str, Any]:
        raise NotImplementedError

    def generate_with_images(
        self,
        system_prompt: str,
        user_prompt: str,
        image_paths: Sequence[Path],
        **kwargs: Any,
    ) -> str:
        """Generate from a system prompt, a user prompt and zero or more images."""
        raise NotImplementedError


class OpenAIClient(LLMClient):
    """Thin wrapper around the OpenAI chat completions API."""

    def __init__(self, model: str, api_key: str, base_url: Optional[str] = None) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ImportError("Install the OpenAI SDK: pip install openai") from exc

        if not api_key.strip():
            raise ValueError("missing llm api_key")

        self.model = model
        self.client = OpenAI(api_key=api_key, base_url=base_url.rstrip("/") if base_url else None)

    @staticmethod
    def _check_content(content: str) -> str:
        if not isinstance(content, str):
            raise ValueError("LLM returned empty content")
        if content.strip().startswith("<!") or "<html" in content.lower():
            raise ValueError("LLM API returned HTML instead of model output; please check base_url")
        return content

    def generate(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        max_tokens: int = 4000,
        **kwargs: Any,
    ) -> str:
        return self._chat(
            [{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=False,
            **kwargs,
        )

    def _chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        temperature: float,
        max_tokens: int,
        json_mode: bool,
        **kwargs: Any,
    ) -> str:
        request: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            **kwargs,
        }
        if json_mode:
            request["response_format"] = {"type": "json_object"}
        response = self.client.chat.completions.create(**request)
        content = response.choices[0].message.content if response.choices else ""
        return self._check_content(content)

    def generate_chat(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.0,
        max_tokens: int = 4000,
        json_mode: bool = False,
        **kwargs: Any,
    ) -> str:
        """Text-only request with separate system and user messages."""
        return self._chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
            **kwargs,
        )

    def generate_with_images(
        self,
        system_prompt: str,
        user_prompt: str,
        image_paths: Sequence[Path],
        *,
        temperature: float = 0.0,
        max_tokens: int = 4000,
        json_mode: bool = False,
        **kwargs: Any,
    ) -> str:
        """Send a multimodal chat request.

        Used by the figure extraction stage (``src/mm``): the model receives the
        textbook text plus the figure image(s), and answers with JSON.
        """
        content: List[Dict[str, Any]] = [{"type": "text", "text": user_prompt}]
        for path in image_paths:
            content.append({"type": "image_url", "image_url": {"url": image_to_data_url(Path(path))}})

        return self._chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
            **kwargs,
        )

    def parse_response(self, response: str) -> Dict[str, Any]:
        json_text = response
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", response, re.DOTALL)
        if fenced:
            json_text = fenced.group(1)
        else:
            brace = re.search(r"\{.*\}", response, re.DOTALL)
            if brace:
                json_text = brace.group(0)

        try:
            data = json.loads(json_text)
        except json.JSONDecodeError as exc:
            raise ValueError("LLM response is not valid JSON") from exc

        nodes = data.get("nodes", [])
        edges = data.get("edges", [])
        if not isinstance(nodes, list):
            nodes = []
        if not isinstance(edges, list):
            edges = []

        for extra_key in ("exercises", "nodes_additional"):
            extra_nodes = data.get(extra_key)
            if isinstance(extra_nodes, list):
                nodes.extend(item for item in extra_nodes if isinstance(item, dict))

        normalized_edges = []
        recovered_nodes = []
        for item in edges:
            if not isinstance(item, dict):
                continue
            has_edge_shape = all(key in item for key in ("source", "target", "type"))
            if item.get("label") == "Exercise" and not has_edge_shape:
                recovered_nodes.append(item)
            else:
                normalized_edges.append(item)

        nodes.extend(recovered_nodes)
        return {"nodes": nodes, "edges": normalized_edges}


def create_llm_client(
    *,
    provider: str,
    model: str,
    api_key: str,
    base_url: Optional[str] = None,
) -> LLMClient:
    if provider != "openai":
        raise ValueError(f"unsupported llm provider: {provider}")
    return OpenAIClient(model=model, api_key=api_key, base_url=base_url)
