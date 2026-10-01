"""Node and edge types of the K12 knowledge graph.

The text pipeline (``src/kg``) builds the first group, the multimodal pipeline
(``src/mm``) adds the second. Keeping both lists here means every stage --
extraction, merging, QA generation, checks -- validates against one definition.
"""

from __future__ import annotations

from typing import Final, FrozenSet

# ---- text side ------------------------------------------------------------- #

TEXT_NODE_TYPES: Final[FrozenSet[str]] = frozenset(
    {"Book", "Chapter", "Section", "Concept", "Skill", "Experiment", "Exercise"}
)

TEXT_EDGE_TYPES: Final[FrozenSet[str]] = frozenset(
    {
        "is_a",
        "prerequisites_for",
        "relates_to",
        "verifies",
        "tests_concept",
        "tests_skill",
        "appears_in",
        "leads_to",
        "is_part_of",
    }
)

# ---- multimodal side ------------------------------------------------------- #

MM_NODE_TYPES: Final[FrozenSet[str]] = frozenset({"Figure", "VisualElement", "Edge"})

MM_EDGE_TYPES: Final[FrozenSet[str]] = frozenset(
    {
        "illustrates",
        "refers_to",
        "supports_edge",
        "requires_figure",
        "contains_visual_element",
    }
)

# The knowledge-point labels a figure / visual element may point at.
MM_TARGET_LABELS: Final[FrozenSet[str]] = frozenset({"Concept", "Skill", "Experiment", "Exercise"})

# How a figure is used in the textbook.
FIGURE_ROLES: Final[tuple[str, ...]] = (
    "说明概念",
    "引入生活情境",
    "展示实验",
    "辅助解题",
    "总结归纳",
    "呈现数据",
)

# A relation is only written into the graph above its threshold.
EDGE_CONFIDENCE_THRESHOLDS: Final[dict[str, float]] = {
    "refers_to": 0.85,
    "illustrates": 0.85,
    "requires_figure": 0.95,
    "supports_edge": 0.95,
}

# A bounding box is only kept above this confidence.
BBOX_CONFIDENCE_THRESHOLD: Final[float] = 0.9

ALL_NODE_TYPES: Final[FrozenSet[str]] = TEXT_NODE_TYPES | MM_NODE_TYPES
ALL_EDGE_TYPES: Final[FrozenSet[str]] = TEXT_EDGE_TYPES | MM_EDGE_TYPES
