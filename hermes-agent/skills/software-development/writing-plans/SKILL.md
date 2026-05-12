---
name: writing-plans
description: "编写实施计划：细粒度任务、文件路径、完整代码。"
version: 1.1.0
author: Hermes Agent (adapted from obra/superpowers)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [planning, design, implementation, workflow, documentation]
    related_skills: [subagent-driven-development, test-driven-development, requesting-code-review]
---

# 编写实施计划

## 概述

编写全面的实施计划，假设实施者对代码库零了解、品味存疑。记录他们需要的一切：要改哪些文件、完整代码、测试命令、要查的文档、如何验证。给他们细粒度的任务。DRY、YAGNI、TDD、频繁提交。

假设实施者是有经验的开发者，但对工具集或问题领域几乎一无所知。假设他们不太擅长好的测试设计。

**核心原则：** 好的计划让实施变得显而易见。如果有人需要猜测，计划就是不完整的。

## 适用场景

**以下情况始终使用：**
- 实施多步骤功能之前
- 拆解复杂需求时
- 通过 subagent-driven-development 委托给子 agent 时

**以下情况不要跳过：**
- 功能看起来很简单（假设会导致 bug）
- 你打算自己实施（未来的你需要指引）
- 独自工作时（文档很重要）

## 细粒度任务

**每个任务 = 2-5 分钟的专注工作。**

每一步都是一个动作：
- "写失败的测试" — 一步
- "运行它确认失败" — 一步
- "写最少的代码让测试通过" — 一步
- "运行测试确认通过" — 一步
- "提交" — 一步

**太大：**
```markdown
### Task 1: Build authentication system
[50 lines of code across 5 files]
```

**合适的大小：**
```markdown
### Task 1: Create User model with email field
[10 lines, 1 file]

### Task 2: Add password hash field to User
[8 lines, 1 file]

### Task 3: Create password hashing utility
[15 lines, 1 file]
```

## 计划文档结构

### 头部（必须）

每个计划必须以此开头：

```markdown
# [Feature Name] Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** [One sentence describing what this builds]

**Architecture:** [2-3 sentences about approach]

**Tech Stack:** [Key technologies/libraries]

---
```

### 任务结构

每个任务遵循此格式：

````markdown
### Task N: [Descriptive Name]

**Objective:** What this task accomplishes (one sentence)

**Files:**
- Create: `exact/path/to/new_file.py`
- Modify: `exact/path/to/existing.py:45-67` (line numbers if known)
- Test: `tests/path/to/test_file.py`

**Step 1: Write failing test**

```python
def test_specific_behavior():
    result = function(input)
    assert result == expected
```

**Step 2: Run test to verify failure**

Run: `pytest tests/path/test.py::test_specific_behavior -v`
Expected: FAIL — "function not defined"

**Step 3: Write minimal implementation**

```python
def function(input):
    return expected
```

**Step 4: Run test to verify pass**

Run: `pytest tests/path/test.py::test_specific_behavior -v`
Expected: PASS

**Step 5: Commit**

```bash
git add tests/path/test.py src/path/file.py
git commit -m "feat: add specific feature"
```
````

## 编写流程

### 步骤 1：理解需求

阅读并理解：
- 功能需求
- 设计文档或用户描述
- 验收标准
- 约束条件

### 步骤 2：探索代码库

用 Hermes 工具了解项目：

```python
# Understand project structure
search_files("*.py", target="files", path="src/")

# Look at similar features
search_files("similar_pattern", path="src/", file_glob="*.py")

# Check existing tests
search_files("*.py", target="files", path="tests/")

# Read key files
read_file("src/app.py")
```

### 步骤 3：设计方案

决定：
- 架构模式
- 文件组织
- 所需依赖
- 测试策略

### 步骤 4：编写任务

按顺序创建任务：
1. 基础设施/搭建
2. 核心功能（每个都用 TDD）
3. 边界情况
4. 集成
5. 清理/文档

### 步骤 5：添加完整细节

每个任务包含：
- **精确文件路径**（不是"配置文件"，而是 `src/config/settings.py`）
- **完整代码示例**（不是"添加验证"，而是实际代码）
- **精确命令**及预期输出
- **验证步骤**证明任务有效

### 步骤 6：审查计划

检查：
- [ ] 任务顺序合理
- [ ] 每个任务细粒度（2-5 分钟）
- [ ] 文件路径精确
- [ ] 代码示例完整（可直接复制粘贴）
- [ ] 命令精确且有预期输出
- [ ] 没有缺失的上下文
- [ ] 应用了 DRY、YAGNI、TDD 原则

### 步骤 7：保存计划

```bash
mkdir -p docs/plans
# Save plan to docs/plans/YYYY-MM-DD-feature-name.md
git add docs/plans/
git commit -m "docs: add implementation plan for [feature]"
```

## 原则

### DRY（不要重复自己）

**坏：** 在 3 个地方复制粘贴验证逻辑
**好：** 提取验证函数，到处使用

### YAGNI（你不会需要它）

**坏：** 为未来需求添加"灵活性"
**好：** 只实现现在需要的

```python
# Bad — YAGNI violation
class User:
    def __init__(self, name, email):
        self.name = name
        self.email = email
        self.preferences = {}  # Not needed yet!
        self.metadata = {}     # Not needed yet!

# Good — YAGNI
class User:
    def __init__(self, name, email):
        self.name = name
        self.email = email
```

### TDD（测试驱动开发）

每个产出代码的任务都应包含完整的 TDD 循环：
1. 写失败测试
2. 运行验证失败
3. 写最少代码
4. 运行验证通过

详见 `test-driven-development` skill。

### 频繁提交

每个任务后提交：
```bash
git add [files]
git commit -m "type: description"
```

## 常见错误

### 任务描述模糊

**坏：** "添加认证"
**好：** "创建带 email 和 password_hash 字段的 User 模型"

### 代码不完整

**坏：** "步骤 1：添加验证函数"
**好：** "步骤 1：添加验证函数"后跟完整的函数代码

### 缺少验证

**坏：** "步骤 3：测试它能用"
**好：** "步骤 3：运行 `pytest tests/test_auth.py -v`，预期：3 passed"

### 缺少文件路径

**坏：** "创建模型文件"
**好：** "创建：`src/models/user.py`"

## 执行交接

保存计划后，提供执行方案：

**"计划已完成并保存。准备使用 subagent-driven-development 执行——我将为每个任务派发一个新的子 agent，进行两阶段审查（规格合规性，然后代码质量）。是否继续？"**

执行时，使用 `subagent-driven-development` skill：
- 每个任务一个新的 `delegate_task`，带完整上下文
- 每个任务后进行规格合规性审查
- 规格通过后进行代码质量审查
- 两个审查都通过后才继续

## 记住

```
Bite-sized tasks (2-5 min each)
Exact file paths
Complete code (copy-pasteable)
Exact commands with expected output
Verification steps
DRY, YAGNI, TDD
Frequent commits
```

**好的计划让实施变得显而易见。**
