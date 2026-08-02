# Design System Specification (Full Scenario Platform)

## Source of truth

- Status: Active Specification (Updated for P0/P1/P2 Full Scenario Matrix)
- Last refreshed: 2026-08-02
- Primary product surfaces: K12 知识图谱全场景 Web 平台（学生端、教师端、治理端）
- Evidence reviewed: `docs/E2E-backend.md`、`K12-KGraph/src/retrieval/`、`docs/E2E-PRD.md`、`docs/E2E-frontend.md`

## Scenario Matrix & Priorities (业务场景矩阵)

| 优先级 | 核心场景 | 目标用户 | 核心价值与 UX 目标 |
| :--- | :--- | :--- | :--- |
| **P0** | **1. 学生错题诊断与补弱路径** | 学生 / 辅导家长 | 输入错题关联知识点，通过前置拓扑查找根因，生成精准补弱链 |
| **P0** | **2. 个性化学习路径规划** | 学生 | 根据目标知识点自动生成起点到终点的关卡式拓扑学习路线图 |
| **P1** | **3. 探究式伴学导师** | 学生 | 启发式提问引导与概念交互探索，不直接抛答案，引导高阶思维 |
| **P1** | **4. 教师备课与教学设计** | 教师 / 教研员 | 梳理跨年级教学拓扑树、教学顺序推演与教案素材选编 |
| **P1** | **5. 自适应测评与组卷** | 教师 / 测评专家 | 基于知识点覆盖率与梯度难度进行智能试题选编与试卷导出 |
| **P2** | **6. 图谱质量治理助手** | 数据管理员 / 图谱专家 | 监控知识节点孤立率、关系缺失预警与教材版本对齐健康度 |

---

## Brand & Personas

- **Personality**: 科学、结构化、启发性、专业高效
- **Trust Signals**: 权威教材章节精准对应、结构化拓扑推导证据、可追溯根因链、透明的算法推理边界
- **Avoid**: 无依据的幻觉回答、低幼卡通化堆砌、臃肿无序的图形碰撞

## Information Architecture & Routes

- **Primary Navigation**:
  - 🎓 **学生空间 (Student Portal)**: 错题诊断 (`/diagnostic`) | 学习路径 (`/pathway`) | 伴学导师 (`/mentor`)
  - 🍎 **教师空间 (Teacher Workbench)**: 备课设计 (`/workbench`) | 自适应组卷 (`/assessment`)
  - 🛠️ **治理空间 (Governance Portal)**: 图谱健康仪表盘 (`/governance`)
- **Core Screens**: `/` (统一门户)、`/diagnostic`、`/pathway`、`/mentor`、`/workbench`、`/assessment`、`/governance`

## Visual Language & Design Tokens

- **Color System**:
  - 主品牌蓝: `#1E3A8A` / `#2563EB`（科学、结构感）
  - P0 补弱/诊断警示色: `#DC2626` / `#F59E0B`（错题根因高亮）
  - 学习路径完成绿: `#059669`（成长反馈）
  - 教师教研橙: `#EA580C`（教学拓扑链点缀）
- **Typography**:
  - 学生端/伴学/投屏模式: 正文 `16px`～`18px`（清晰易读）
  - 教师工作台/组卷/治理面板: 正文 `14px`～`16px`（高信息密度）
- **Layout Rhythm**: 4px 网格，针对不同场景支持：
  - **单列引导卡片布局**（错题补弱/伴学导师）
  - **拓扑关卡 Stepper 布局**（学习路径）
  - **三栏式工作台布局**（教师备课/组卷/图谱治理）

## Component System (核心场景组件)

### P0 场景组件
1. `ErrorDiagnosticCard`: 展示错题对应的底层知识点、直接原因与前置依赖根因。
2. `RootCauseTopologyFlow`: 展示“你错在 X，根本原因在于未掌握前置节点 Y”的关系链图。
3. `LearningPathSteppers`: 节点式关卡路径，支持高亮已掌握、推荐学习与终点概念。

### P1 场景组件
4. `ExploreMentorPanel`: 探究式伴学面板，提供图谱启发性追问（如“试试想一想这个图形与长方形有什么关系？”）。
5. `TeacherWorkbench`: 教师教学顺序推演与素材卡片组装。
6. `AdaptivePaperBuilder`: 知识点覆盖矩阵与梯度组卷控件。

### P2 场景组件
7. `KGHealthDashboard`: 图谱连通性、孤立节点预警与节点覆盖率图标。

---

## Interaction States & Responsive Rules

- **Interaction States**: `idle` (空闲/待检测)、`loading` (拓扑计算中)、`success` (根因生成/路径推荐完成)、`empty` (无关联错题)、`clarify` (概念澄清)、`error` (数据服务异常)。
- **Responsive Adaptations**:
  - 桌面端 (1024px+): 支持完整三栏工作台与交互式 React Flow 全屏拓扑图。
  - 平板/电子白板: 支持触摸缩放图谱与备课投屏演示。
  - 移动端 (360px+): 自动收拢拓扑图为纵向时序流（Timeline/Steppers）与 Bottom Sheet 诊断抽屉。
