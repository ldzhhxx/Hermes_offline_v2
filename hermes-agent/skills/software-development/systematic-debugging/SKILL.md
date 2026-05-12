---
name: systematic-debugging
description: "4 阶段根因调试：先理解 bug，再动手修复。"
version: 1.1.0
author: Hermes Agent (adapted from obra/superpowers)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [debugging, troubleshooting, problem-solving, root-cause, investigation]
    related_skills: [test-driven-development, writing-plans, subagent-driven-development]
---

# 系统化调试

## 概述

随机乱改只会浪费时间，还会引入新 bug。快速打补丁只会掩盖根本问题。

**核心原则：** 动手修复之前，必须先找到根因。只治症状等于失败。

**违反这个流程的字面规则，就是违反了调试的精神。**

## 铁律

```
NO FIXES WITHOUT ROOT CAUSE INVESTIGATION FIRST
```

如果还没完成第一阶段，就不能提出修复。

## 适用场景

适用于**任何**技术问题：
- 测试失败
- 生产环境 bug
- 非预期行为
- 性能问题
- 构建失败
- 集成问题

**以下情况尤其要用：**
- 时间紧迫时（紧急情况最容易让人想猜答案）
- "只改一个小地方"看起来显而易见时
- 已经尝试过多次修复
- 上一个修复没有生效
- 还没完全理解问题所在

**以下情况不要跳过：**
- 问题看起来很简单（简单的 bug 也有根因）
- 时间很赶（赶工只会导致返工）
- 有人催着要立刻修好（系统化比乱试快）

## 四个阶段

每个阶段必须完成后才能进入下一阶段。

---

## 第一阶段：根因调查

**在尝试任何修复之前：**

### 1. 仔细阅读错误信息

- 不要跳过错误或警告
- 错误信息里往往直接包含解决方案
- 完整阅读 stack trace
- 记录行号、文件路径、错误码

**操作：** 用 `read_file` 读取相关源文件，用 `search_files` 在代码库中搜索错误字符串。

### 2. 稳定复现

- 能否可靠地触发这个问题？
- 确切的复现步骤是什么？
- 每次都会出现吗？
- 无法复现 → 收集更多数据，不要猜测

**操作：** 用 `terminal` 工具运行失败的测试或触发 bug：

```bash
# Run specific failing test
pytest tests/test_module.py::test_name -v

# Run with verbose output
pytest tests/test_module.py -v --tb=long
```

### 3. 检查近期变更

- 什么改动可能导致了这个问题？
- 查看 git diff、近期提交
- 新依赖、配置变更

**操作：**

```bash
# Recent commits
git log --oneline -10

# Uncommitted changes
git diff

# Changes in specific file
git log -p --follow src/problematic_file.py | head -100
```

### 4. 多组件系统中收集证据

**当系统有多个组件时（API → service → database，CI → build → deploy）：**

**在提出修复方案之前，先加入诊断日志：**

对每个组件边界：
- 记录进入该组件的数据
- 记录离开该组件的数据
- 验证环境/配置的传递
- 检查每一层的状态

先运行一次收集证据，确认**在哪里**出了问题。
然后分析证据，定位出问题的组件。
再深入调查那个具体组件。

### 5. 追踪数据流

**当错误深埋在调用栈中时：**

- 错误值从哪里产生？
- 是谁用错误的值调用了这个函数？
- 一直向上追溯，直到找到源头
- 在源头修复，而不是在症状处修复

**操作：** 用 `search_files` 追踪引用：

```python
# Find where the function is called
search_files("function_name(", path="src/", file_glob="*.py")

# Find where the variable is set
search_files("variable_name\\s*=", path="src/", file_glob="*.py")
```

### 第一阶段完成检查清单

- [ ] 错误信息已完整阅读并理解
- [ ] 问题已稳定复现
- [ ] 近期变更已识别并审查
- [ ] 证据已收集（日志、状态、数据流）
- [ ] 问题已定位到具体组件/代码
- [ ] 已形成根因假设

**停止：** 在理解"为什么"发生之前，不要进入第二阶段。

---

## 第二阶段：模式分析

**修复之前先找到规律：**

### 1. 找到可用的参考示例

- 在同一代码库中找类似的可用代码
- 什么类似的东西是正常工作的？

**操作：** 用 `search_files` 找到可比较的模式：

```python
search_files("similar_pattern", path="src/", file_glob="*.py")
```

### 2. 对照参考实现

- 如果在实现某个模式，**完整**阅读参考实现
- 不要略读——每一行都要读
- 完全理解这个模式后再应用

### 3. 找出差异

- 可用的和出问题的有什么不同？
- 列出每一处差异，无论多小
- 不要假设"这个应该没影响"

### 4. 理解依赖关系

- 这个组件需要哪些其他组件？
- 需要什么设置、配置、环境？
- 它做了哪些假设？

---

## 第三阶段：假设与验证

**科学方法：**

### 1. 形成单一假设

- 清晰表述："我认为 X 是根因，因为 Y"
- 写下来
- 要具体，不要模糊

### 2. 最小化测试

- 做**最小**的改动来验证假设
- 每次只改一个变量
- 不要同时修复多个问题

### 3. 验证后再继续

- 成功了？→ 进入第四阶段
- 没成功？→ 形成**新的**假设
- **不要**在上面继续叠加修复

### 4. 不知道时

- 说"我不理解 X"
- 不要假装知道
- 向用户寻求帮助
- 继续研究

---

## 第四阶段：实施修复

**修复根因，而不是症状：**

### 1. 创建失败测试用例

- 最简单的复现方式
- 尽可能用自动化测试
- 修复之前必须先有测试
- 使用 `test-driven-development` skill

### 2. 实施单一修复

- 针对已识别的根因
- 每次只改一处
- 不要"顺手"改其他东西
- 不要捆绑重构

### 3. 验证修复

```bash
# Run the specific regression test
pytest tests/test_module.py::test_regression -v

# Run full suite — no regressions
pytest tests/ -q
```

### 4. 修复无效时——三次规则

- **停下来。**
- 数一数：已经尝试了几次修复？
- 少于 3 次：带着新信息回到第一阶段重新分析
- **3 次或以上：停下来，质疑架构（见下方第 5 步）**
- 不要在没有架构讨论的情况下尝试第 4 次修复

### 5. 3 次以上修复失败：质疑架构

**表明存在架构问题的模式：**
- 每次修复都在不同地方暴露新的共享状态/耦合
- 修复需要"大规模重构"才能实施
- 每次修复都在其他地方产生新症状

**停下来，质疑基础：**
- 这个模式从根本上是否合理？
- 我们是否"出于惯性而坚持"？
- 应该重构架构，而不是继续修复症状？

**在尝试更多修复之前，先与用户讨论。**

这不是假设失败——这是架构错误。

---

## 危险信号——停下来，遵循流程

如果你发现自己在想：
- "先快速修一下，之后再调查"
- "试着改一下 X 看看有没有用"
- "加几个改动，跑一下测试"
- "跳过测试，我手动验证"
- "可能是 X，让我修一下"
- "我还没完全理解，但这个可能有用"
- "模式说 X，但我会换个方式适配"
- "主要问题是：[列出修复方案，没有调查]"
- 在追踪数据流之前就提出解决方案
- **"再试一次修复"（已经试了 2 次以上）**
- **每次修复都在不同地方暴露新问题**

**以上所有情况都意味着：停下来，回到第一阶段。**

**3 次以上修复失败：** 质疑架构（第四阶段第 5 步）。

## 常见借口

| 借口 | 现实 |
|--------|---------|
| "问题很简单，不需要流程" | 简单的问题也有根因。流程对简单 bug 来说很快。 |
| "紧急情况，没时间走流程" | 系统化调试**比**乱猜快。 |
| "先试试这个，再调查" | 第一次修复就定下了基调。从一开始就做对。 |
| "确认修复有效后再写测试" | 未经测试的修复不稳固。先写测试才能证明有效。 |
| "同时修复多个问题节省时间" | 无法分离哪个有效。会引入新 bug。 |
| "参考太长了，我来适配模式" | 理解不完整必然产生 bug。完整阅读。 |
| "我看到问题了，让我修一下" | 看到症状 ≠ 理解根因。 |
| "再试一次"（已失败 2 次以上） | 3 次以上失败 = 架构问题。质疑模式，不要再修了。 |

## 快速参考

| 阶段 | 关键活动 | 成功标准 |
|-------|---------------|------------------|
| **1. 根因** | 读错误、复现、检查变更、收集证据、追踪数据流 | 理解是什么以及为什么 |
| **2. 模式** | 找可用示例、对比、找差异 | 知道哪里不同 |
| **3. 假设** | 形成理论、最小化测试、每次一个变量 | 确认或形成新假设 |
| **4. 实施** | 创建回归测试、修复根因、验证 | Bug 解决，所有测试通过 |

## Hermes Agent 集成

### 调查工具

在第一阶段使用这些 Hermes 工具：

- **`search_files`** — 查找错误字符串、追踪函数调用、定位模式
- **`read_file`** — 带行号读取源代码，精确分析
- **`terminal`** — 运行测试、查看 git 历史、复现 bug
- **`web_search`/`web_extract`** — 研究错误信息、查阅库文档

### 配合 delegate_task

对于复杂的多组件调试，可以派发调查子 agent：

```python
delegate_task(
    goal="Investigate why [specific test/behavior] fails",
    context="""
    Follow systematic-debugging skill:
    1. Read the error message carefully
    2. Reproduce the issue
    3. Trace the data flow to find root cause
    4. Report findings — do NOT fix yet

    Error: [paste full error]
    File: [path to failing code]
    Test command: [exact command]
    """,
    toolsets=['terminal', 'file']
)
```

### 配合 test-driven-development

修复 bug 时：
1. 写一个能复现 bug 的测试（RED）
2. 系统化调试找到根因
3. 修复根因（GREEN）
4. 测试证明修复有效并防止回归

## 实际效果

来自调试实践的数据：
- 系统化方法：15-30 分钟解决
- 随机乱试方法：2-3 小时反复折腾
- 一次修复成功率：95% vs 40%
- 引入新 bug：几乎为零 vs 常见

**没有捷径，没有猜测。系统化永远赢。**
