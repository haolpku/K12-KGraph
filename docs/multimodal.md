# Multimodal extension

The multimodal branch extends the text knowledge graph with figure-grounded nodes and
relations, then derives image-conditioned training data. It reuses the text pipeline
outputs and adds a small number of new node and edge types.

## Pipeline

```
MinerU output (markdown + images/) ─┐
workspace/segmentation/<book>/sections/*.md ─┤─► align_figures.py ─► aligned_sections.json
                                   │
aligned_sections.json + data/book_kg/<book>.json ─► extract_figures.py ─► data/mmkg/<book>.json
                                   │  1. is the figure worth keeping
                                   │  2. figure role + visual elements
                                   └─ 3. bounding boxes + relations to knowledge points

data/mmkg/<book>.json ─► vqa_generate.py ─► sft_vqa.jsonl (4 edge types)
                      ─► vqa_assets.py   ─► figures/ + ve_boxed/
                      ─► export_alpaca.py─► LLaMA-Factory alpaca + images
```

Run everything with one command:

```bash
python run_pipeline.py mm --filter-prefix math_7a_rjb   # align + extract
python run_pipeline.py qa --filter-prefix math_7a_rjb   # generate + assets + export
```

## Node and edge types

New node types: `Figure`, `VisualElement`, and `Edge` (a reference node standing for a
text-side relation that a figure provides evidence for).

| Edge | Direction | Meaning |
| --- | --- | --- |
| `illustrates` | Figure → Concept / Skill / Experiment | the whole figure visually explains the knowledge point |
| `refers_to` | VisualElement → Concept / Skill / Experiment | one region of the figure corresponds to the knowledge point |
| `supports_edge` | Figure → Edge | the figure is visual evidence for an existing text-side relation |
| `requires_figure` | Exercise → Figure | the exercise cannot be understood or answered without the figure |
| `contains_visual_element` | Figure → VisualElement | structural containment |
| `appears_in` | Figure → Section | structural location |

`VisualElement` carries an optional `bbox_2d`, normalised to 0–1000 as
`[ymin, xmin, ymax, xmax]`, plus `bbox_confidence` and `bbox_rationale`. Boxes below the
confidence threshold are dropped. All thresholds and the role vocabulary live in
`src/utils/schema.py`.

## One graph per book

`extract_figures.py` starts from the text graph (`data/book_kg/<book>.json`) and writes
the complete graph to `data/mmkg/<book>.json`: text nodes, afterclass exercise nodes and
figure nodes in one place, with every edge resolving inside that file. Candidate
knowledge points for a section are selected through the `appears_in` edges, so the model
only ever chooses between ids that already exist in the graph.

## Paths

Everything is resolved from `config/default.yaml` (see `src/utils/config.py`):

| Purpose | Default |
| --- | --- |
| section markdown | `workspace/segmentation/<book>/sections/` |
| MinerU output | `workspace/pdf_to_md/<book>/mineru_output/**/hybrid_auto/` |
| text-side book graph | `data/book_kg/<book>.json` |
| afterclass exercises | `data/afterclass_exercises/<book>.json` |
| alignment + partials | `workspace/mm/<book>/` |
| per-book graph | `data/mmkg/<book>.json` |
| VQA data | `data/mmkg/vqa/{sft_vqa,assets,alpaca}/` |

`workspace/` and `data/` are git-ignored, so nothing generated is committed.

## Setup

```bash
pip install -r requirements.txt          # includes Pillow for the bounding-box images
cp config/.env.example config/.env       # OPENAI_API_KEY / OPENAI_BASE_URL
export OPENAI_MODEL=<your vision model>
```

Any OpenAI-compatible multimodal endpoint works. Model, base URL and key variable can
also be given per run with `--model` / `--api-base` / `--api-key-env`.

## Prompts

| Stage | Files |
| --- | --- |
| figure filtering (1), role + visual elements (2), boxes + relations (3) | `src/mm/prompts/stage{1,2,3}_{system,user}.txt` |
| one prompt per VQA edge type | `src/qa/prompts/vqa/*.txt` |

All prompt files use Python `str.format` placeholders (`{name}`); literal braces must be
doubled.

## Notes

- Long runs are resumable: `extract_figures.py` writes one partial file per section under
  `workspace/mm/<book>/partials/`, and `src/mm/run_pipeline.py` always passes `--resume`.
- `--dry-run` builds the graph without calling the model, which is useful to check paths
  and the alignment result.
- After writing, the extractor verifies that every edge endpoint exists in the graph and
  prints a warning if any do not.
