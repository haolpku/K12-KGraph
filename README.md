<div align="center">

<a href="https://haolpku.github.io/K12-KGraph-page/">
  <img src="docs/img/kgraph-overview.png" alt="K12-KGraph construction pipeline" width="860"/>
</a>

<h1>K12-KGraph</h1>

<p><b>A Curriculum-Aligned Knowledge Graph for Benchmarking &amp; Training Educational LLMs</b></p>

<p>
  <a href="https://huggingface.co/datasets/lhpku20010120/K12-KGraph">
    <img alt="Dataset on Hugging Face" src="https://img.shields.io/badge/%F0%9F%A4%97%20Dataset-K12--KGraph-ffbd44?style=for-the-badge"/>
  </a>
  <a href="#-released-checkpoints">
    <img alt="Models on Hugging Face" src="https://img.shields.io/badge/%F0%9F%A4%97%20Models-3%20Checkpoints-8b5cf6?style=for-the-badge"/>
  </a>
  <a href="https://haolpku.github.io/K12-KGraph-page/">
    <img alt="Project Page" src="https://img.shields.io/badge/Project-Page-6b5df5?style=for-the-badge&logo=github-pages&logoColor=white"/>
  </a>
  <a href="#-citation">
    <img alt="Paper" src="https://img.shields.io/badge/Paper-NeurIPS%202026%20D%26B-1f6feb?style=for-the-badge&logo=readthedocs&logoColor=white"/>
  </a>
  <a href="LICENSE">
    <img alt="License" src="https://img.shields.io/badge/License-CC%20BY--NC--SA%204.0-2ea043?style=for-the-badge&logo=creativecommons&logoColor=white"/>
  </a>
</p>

<p>
  <img alt="Nodes" src="https://img.shields.io/badge/KG%20Nodes-10%2C685-4f46e5"/>
  <img alt="Edges" src="https://img.shields.io/badge/KG%20Edges-23%2C278-0891b2"/>
  <img alt="Bench" src="https://img.shields.io/badge/Bench%20Questions-23%2C640-ec4899"/>
  <img alt="Train" src="https://img.shields.io/badge/Train%20Pairs-2%2C267-10b981"/>
  <img alt="Subjects" src="https://img.shields.io/badge/Subjects-Math%20%C2%B7%20Phys%20%C2%B7%20Chem%20%C2%B7%20Bio-64748b"/>
  <img alt="Stage" src="https://img.shields.io/badge/Grade-K1%E2%80%93K12-f59e0b"/>
</p>

<p>
  <sub>
    Built from the official People's Education Press (PEP) Chinese K–12 textbooks,
    K12-KGraph aligns the same scientific concept across <b>definition</b>, <b>formula</b>, <b>experiment</b>,
    <b>exercise</b>, <b>structural location</b>, and <b>relational neighborhood</b>.
  </sub>
</p>

<p>
  🌐 <a href="https://haolpku.github.io/K12-KGraph-page/"><b>Explore the interactive project page →</b></a>
</p>

</div>

---

## 🏆 News

- **July 2026** — K12-KGraph, developed by the **PKU-DCAI team**, received **Second Prize** in the **AGI4S Frontier Corpus Track** of the **MinerU Frontier Corpus and Data Intelligence Challenge**. [Award certificate](docs/awards/k12-kgraph-mineru-2026-second-prize.pdf).

---

## 🌟 Why K12-KGraph?

Modern LLMs can answer "what is the Pythagorean theorem?" but struggle with **curriculum cognition** — the structured understanding of:

- 🧭 *What are the prerequisites of a concept?*
- 🔬 *Which experiment verifies it?*
- 📝 *Which exercises test it?*
- 📚 *Where does it live in the textbook?*
- 🕸 *What are its taxonomic and relational neighbors?*

K12-KGraph is the first open, multi-subject, official-textbook-grounded knowledge graph that explicitly aligns all five dimensions around each STEM concept, yielding two ready-to-use AI assets:

| | [K12-Bench](https://huggingface.co/datasets/lhpku20010120/K12-KGraph) | [K12-Train](https://huggingface.co/datasets/lhpku20010120/K12-KGraph) |
|---|---|---|
| **Size** | 23,640 multi-select questions | 2,267 instruction–response pairs |
| **Purpose** | Evaluate structural curriculum cognition | Teach it via KG-guided SFT |
| **Task families / sources** | Ground · Prereq · Neighbor · Evidence · Locate | Node-grounded + Edge-grounded + Deterministic templates |
| **Headline result** | Gemini-3-Flash reaches only **57.1%** EM | Beats 8 mainstream SFT corpora on GaokaoBench & EduEval under a strict 2,300-sample budget |

---

## 📊 Leaderboard Snapshot (K12-Bench, zero-shot)

Instance-level macro F1 and exact match, in %.

| Model | Overall EM | Overall F1 |
|---|---|---|
| *Random guess baseline* | 6.7 | 36.4 |
| Meta-LLaMA-3-8B-Instruct | 7.2 | 52.6 |
| GLM-4.7-Flash | 31.7 | 63.9 |
| GPT-4o | 31.1 | 65.9 |
| Qwen3-32B | 42.6 | 69.5 |
| Gemma-4-31B-IT | 46.4 | 69.5 |
| GPT-5.2 | 42.8 | 68.0 |
| Gemini-2.5-Flash | 48.3 | 66.7 |
| **Gemini-3-Flash** | **57.1** | **73.0** |

> Even the strongest proprietary model leaves > 40% of items unsolved on **Prereq** and **Neighbor** — the tasks requiring directed, structural reasoning. See the [project page](https://haolpku.github.io/K12-KGraph-page/) for the full 5-task breakdown.

---

## 🗺️ What's in this Repository?

```
K12-KGraph/
├── src/
│   ├── kg/          # Knowledge-graph construction pipeline
│   ├── benchmark/   # K12-Bench generation from graph queries
│   ├── sft_qa/      # K12-Train synthesis (node & edge grounded)
│   └── utils/       # Shared config / LLM client / IO
├── eval/            # Multiple-choice evaluation runner (OpenAI / vLLM)
├── config/          # Default pipeline configuration
├── demo/            # Trimmed JSON/JSONL samples
├── books.yaml       # Book registry
├── docs/img/        # README figures
└── requirements.txt
```

Pipeline flow:

```
PDF textbooks ─► MinerU parsing ─► Section split ─► GPT-5.2 schema-constrained extraction
               ─► Hierarchical merge (book → subject → global) ─► DAG validation + expert review
               ─► K12-KGraph ─► K12-Bench (queries) + K12-Train (QA synthesis)
```

---

## 🚀 Quick Start

### 1. Install

```bash
git clone https://github.com/haolpku/K12-KGraph.git
cd K12-KGraph
pip install -r requirements.txt
```

> If you will run the graph pipeline from PDFs, also install [**MinerU**](https://github.com/opendatalab/MinerU) and make `magic-pdf` callable from the shell (command name configurable via `config/default.yaml`).

### 2. Load the released dataset

```python
from datasets import load_dataset

kg    = load_dataset("lhpku20010120/K12-KGraph", split="train")
bench = load_dataset("lhpku20010120/K12-KGraph", name="bench", split="test")
train = load_dataset("lhpku20010120/K12-KGraph", name="train", split="train")
```

### 3. Build the graph from scratch

```bash
cp config/.env.example config/.env         # add your OPENAI_API_KEY etc.
python src/kg/run_pipeline.py \
    --config config/default.yaml \
    --filter-prefix <YourBookPrefix>       # e.g. math_7a_rjb
```

### 4. Derive Bench and SFT data

```bash
python src/benchmark/run_pipeline.py --help
python src/sft_qa/run_pipeline.py   --help
```

### 5. Evaluate a model on K12-Bench

```bash
cp eval/configs/.env.example eval/configs/.env
chmod +x eval/run.sh
./eval/run.sh <model-config-stem>          # eval/configs/models/<stem>.yaml
```

---

## 🤗 Released Checkpoints

Three standalone checkpoints trained on **K12-Train** are available on Hugging Face. Each repository contains the tokenizer, chat template, configuration, and merged/full weights; no separate base-model download is required.

| Checkpoint | Parameters | Fine-tuning | Weight size | License |
|---|---:|---|---:|---|
| [Llama3.1-8B-K12KGraph](https://huggingface.co/lhpku20010120/llama3.1-8b-k12kgraph) | 8B | Full SFT | 16.1 GB | Llama 3.1 |
| [Qwen3.5-2B-K12KGraph](https://huggingface.co/lhpku20010120/qwen3.5-2b-k12kgraph) | 2B | LoRA SFT, merged | 4.4 GB | Apache-2.0 |
| [Qwen3-4B-K12KGraph](https://huggingface.co/lhpku20010120/qwen3-4b-k12kgraph) | 4B | Full SFT | 8.0 GB | Apache-2.0 |

Download a checkpoint with the Hugging Face CLI:

```bash
pip install -U huggingface_hub
hf download lhpku20010120/qwen3.5-2b-k12kgraph \
  --local-dir checkpoints/qwen3.5-2b-k12kgraph
```

Or serve one directly from the Hub and evaluate it on K12-Bench:

```bash
# Replace qwen3_5_2b with llama3_1_8b or qwen3_4b as needed.
bash eval/vllm_scripts/qwen3_5_2b_k12kgraph.sh
./eval/run.sh qwen3_5_2b_k12kgraph_vllm
```

The launchers accept the standard `VLLM_HOST`, `VLLM_PORT`, `VLLM_TP_SIZE`, `VLLM_MAX_MODEL_LEN`, `VLLM_GPU_MEM`, and `VLLM_MODEL_ID` overrides.

---

## 🧱 Schema at a Glance

**7 node types** — `Book` · `Chapter` · `Section` · `Concept` · `Skill` · `Experiment` · `Exercise`

**9 edge types** — `is_a` · `prerequisites_for` · `relates_to` · `verifies` · `tests_concept` · `tests_skill` · `appears_in` · `leads_to` · `is_part_of`

Every `Concept` carries `name`, `definition`, `importance`, and optional `formula`, `aliases`, `examples`. Every `Experiment` carries `instruments`, `is_student`, `process`, `phenomena`, `conclusion`. Full schema and attribute specification in [`docs/schema.md`](docs/schema.md) (coming soon) or on the [project page](https://haolpku.github.io/K12-KGraph-page/).

<div align="center">
  <img src="docs/img/example.png" alt="A concrete example: how the same prerequisites_for subgraph yields a K12-Bench item (A) and a K12-Train QA pair (B)" width="780"/>
</div>

---

## 📚 Dataset Composition

<div align="center">
  <img src="docs/img/bench-distribution.png" alt="K12-Bench distribution across subjects, task families, and difficulty." width="780"/>
</div>

| Subject | Books | Concepts | Skills | Experiments | Exercises |
|---|---:|---:|---:|---:|---:|
| Mathematics | 23 | 1,475 | 428 | 0 | 471 |
| Physics | 9 | 1,154 | 197 | 220 | 186 |
| Chemistry | 7 | 2,302 | 451 | 309 | 270 |
| Biology | 9 | 1,648 | 288 | 123 | 244 |
| **Total** | **48** | **6,579** | **1,364** | **652** | **1,171** |

---

## 🧪 Quality Assurance

- **Fleiss' κ = 0.84** overall, from 12 subject-qualified expert annotators (κ by relation: `is_a` 0.91, `prerequisites_for` 0.82, `relates_to` 0.69, `verifies` 0.88)
- **Automatic DAG validation** on `is_a` and `prerequisites_for` subgraphs
- **Per-edge `evidence` field** linking back to textbook source text for auditability
- **98.4%** stratified K12-Bench items verified as "fully correct" in a 3-expert spot-check

---

## 🌈 Explore Interactively

Want to browse nodes, sample bench items, or inspect the training data without cloning the repo? The companion project page offers a rich interactive view:

<p align="center">
  <a href="https://haolpku.github.io/K12-KGraph-page/">
    <img src="https://img.shields.io/badge/OPEN%20PROJECT%20PAGE-haolpku.github.io/K12--KGraph--page-6b5df5?style=for-the-badge&logo=google-chrome&logoColor=white"/>
  </a>
</p>

---

## 🤝 Contribute

Contributions are welcome! We particularly appreciate:

- 🏫 Adding support for other textbook publishers (BNU, Jiangsu, etc.)
- 🧪 New task families that extend beyond the current 5
- 🐛 Bug reports and quality issues on existing graph edges (please cite the specific edge ID)
- 🌍 Translation of the schema/documentation into additional languages

Open an issue or pull request — GitHub Issues are monitored within 48 hours.

---

## 📖 Citation

If you find K12-KGraph useful in your research, please cite:

```bibtex
@misc{k12kgraph2026,
  title        = {K12-KGraph: A Curriculum-Aligned Knowledge Graph for
                  Benchmarking and Training Educational LLMs},
  author       = {Hao Liang and others},
  year         = {2026},
  howpublished = {Submitted to NeurIPS 2026 Evaluations and Datasets Track},
  url          = {https://github.com/haolpku/K12-KGraph}
}
```

---

## 📄 License

- **Dataset** (graph, benchmark, training data): [CC BY-NC-SA 4.0](LICENSE)
- **Code** (this repository): MIT

---

<div align="center">
  <sub>
    Made with care by the K12-KGraph team ·
    <a href="https://haolpku.github.io/K12-KGraph-page/">Project Page</a> ·
    <a href="https://huggingface.co/datasets/lhpku20010120/K12-KGraph">Dataset</a> ·
    <a href="README_zh.md">中文 README</a>
  </sub>
</div>
