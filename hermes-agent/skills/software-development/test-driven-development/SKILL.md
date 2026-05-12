---
name: test-driven-development
description: "TDD：强制执行 RED-GREEN-REFACTOR，先写测试再写代码。"
version: 1.1.0
author: Hermes Agent (adapted from obra/superpowers)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [testing, tdd, development, quality, red-green-refactor]
    related_skills: [systematic-debugging, writing-plans, subagent-driven-development]
---

# 测试驱动开发（TDD）

## 概述

先写测试，看它失败，再写最少的代码让它通过。

**核心原则：** 如果你没有亲眼看到测试失败，你就不知道它是否在测试正确的东西。

**违反这个规则的字面要求，就是违反了规则的精神。**

## 适用场景

**始终使用：**
- 新功能
- Bug 修复
- 重构
- 行为变更

**例外情况（先询问用户）：**
- 一次性原型
- 生成的代码
- 配置文件

觉得"这次跳过 TDD 就好"？停下来，那是在给自己找借口。

## 铁律

```
NO PRODUCTION CODE WITHOUT A FAILING TEST FIRST
```

先写了代码再写测试？删掉它，重新开始。

**没有例外：**
- 不要留着当"参考"
- 不要"边写测试边适配"它
- 不要看它
- 删除就是删除

从测试出发重新实现。就这样。

## Red-Green-Refactor 循环

### RED — 写失败的测试

写一个最小的测试，说明应该发生什么。

**好的测试：**
```python
def test_retries_failed_operations_3_times():
    attempts = 0
    def operation():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise Exception('fail')
        return 'success'

    result = retry_operation(operation)

    assert result == 'success'
    assert attempts == 3
```
名称清晰，测试真实行为，只测一件事。

**坏的测试：**
```python
def test_retry_works():
    mock = MagicMock()
    mock.side_effect = [Exception(), Exception(), 'success']
    result = retry_operation(mock)
    assert result == 'success'  # What about retry count? Timing?
```
名称模糊，测试的是 mock 而不是真实代码。

**要求：**
- 每个测试只测一个行为
- 名称清晰描述性（名称里有"and"？拆分它）
- 真实代码，不用 mock（除非真的不可避免）
- 名称描述行为，而不是实现

### 验证 RED — 亲眼看到失败

**必须执行，不得跳过。**

```bash
# Use terminal tool to run the specific test
pytest tests/test_feature.py::test_specific_behavior -v
```

确认：
- 测试失败（不是因为拼写错误导致的报错）
- 失败信息符合预期
- 因为功能缺失而失败

**测试立即通过？** 你在测试已有的行为。修改测试。

**测试报错？** 修复错误，重新运行，直到它正确地失败。

### GREEN — 最少的代码

写最简单的代码让测试通过，不多写一行。

**好的：**
```python
def add(a, b):
    return a + b  # Nothing extra
```

**坏的：**
```python
def add(a, b):
    result = a + b
    logging.info(f"Adding {a} + {b} = {result}")  # Extra!
    return result
```

不要添加功能、重构其他代码，或做超出测试要求的"改进"。

**GREEN 阶段允许作弊：**
- 硬编码返回值
- 复制粘贴
- 重复代码
- 跳过边界情况

REFACTOR 阶段再来修。

### 验证 GREEN — 亲眼看到通过

**必须执行。**

```bash
# Run the specific test
pytest tests/test_feature.py::test_specific_behavior -v

# Then run ALL tests to check for regressions
pytest tests/ -q
```

确认：
- 测试通过
- 其他测试仍然通过
- 输出干净（无错误、无警告）

**测试失败？** 修复代码，不要修改测试。

**其他测试失败？** 现在就修复回归。

### REFACTOR — 清理代码

只在 GREEN 之后：
- 消除重复
- 改善命名
- 提取辅助函数
- 简化表达式

全程保持测试通过，不要添加新行为。

**重构过程中测试失败？** 立即撤销，步子迈小一点。

### 重复

为下一个行为写下一个失败测试，一个循环一个循环地来。

## 为什么顺序很重要

**"我会在之后写测试来验证它能用"**

事后写的测试会立即通过。立即通过什么都证明不了：
- 可能测的是错误的东西
- 可能测的是实现而不是行为
- 可能遗漏了你忘记的边界情况
- 你从来没有看到它捕获 bug

先写测试迫使你看到测试失败，证明它确实在测试某些东西。

**"我已经手动测试了所有边界情况"**

手动测试是临时性的。你以为测了所有情况，但：
- 没有记录测了什么
- 代码变更时无法重新运行
- 压力下容易忘记情况
- "我试过能用" ≠ 全面覆盖

自动化测试是系统性的，每次以相同方式运行。

**"删除 X 小时的工作是浪费"**

沉没成本谬误。时间已经过去了。你现在的选择是：
- 删除并用 TDD 重写（高置信度）
- 保留并事后加测试（低置信度，可能有 bug）

"浪费"是保留你无法信任的代码。

## 常见借口

| 借口 | 现实 |
|--------|---------|
| "太简单了，不需要测试" | 简单代码也会出问题。测试只需 30 秒。 |
| "我之后再测" | 立即通过的测试什么都证明不了。 |
| "事后测试达到同样目的" | 事后测试 = "这做了什么？"先写测试 = "这应该做什么？" |
| "已经手动测试过了" | 临时性 ≠ 系统性。没有记录，无法重新运行。 |
| "删除 X 小时的工作是浪费" | 沉没成本谬误。保留未验证的代码是技术债。 |
| "留着当参考，先写测试" | 你会去适配它。那就是事后测试。删除就是删除。 |
| "需要先探索一下" | 没问题。扔掉探索代码，从 TDD 开始。 |
| "测试很难写 = 设计不清晰" | 听测试的话。难以测试 = 难以使用。 |
| "TDD 会让我变慢" | TDD 比调试快。务实 = 先写测试。 |
| "手动测试更快" | 手动测试无法证明边界情况。每次改动都要重新测。 |
| "现有代码没有测试" | 你在改进它。为你接触的代码加测试。 |

## 危险信号——停下来，重新开始

如果你发现自己在做以下任何事，删除代码，用 TDD 重新开始：

- 先写代码再写测试
- 实现后才写测试
- 测试第一次运行就通过
- 无法解释为什么测试失败
- 测试"之后再加"
- 合理化"就这一次"
- "我已经手动测试过了"
- "事后测试达到同样目的"
- "留着当参考"或"适配现有代码"
- "已经花了 X 小时，删除是浪费"
- "TDD 太教条了，我在务实"
- "这种情况不一样，因为……"

**以上所有情况都意味着：删除代码，用 TDD 重新开始。**

## 验收检查清单

标记工作完成之前：

- [ ] 每个新函数/方法都有测试
- [ ] 实现之前亲眼看到每个测试失败
- [ ] 每个测试因预期原因失败（功能缺失，而不是拼写错误）
- [ ] 写了最少的代码让每个测试通过
- [ ] 所有测试通过
- [ ] 输出干净（无错误、无警告）
- [ ] 测试使用真实代码（mock 只在不可避免时使用）
- [ ] 边界情况和错误情况已覆盖

无法勾选所有项？你跳过了 TDD。重新开始。

## 遇到困难时

| 问题 | 解决方案 |
|---------|----------|
| 不知道怎么测试 | 写出期望的 API。先写断言。问用户。 |
| 测试太复杂 | 设计太复杂。简化接口。 |
| 必须 mock 所有东西 | 代码耦合太紧。使用依赖注入。 |
| 测试 setup 很庞大 | 提取辅助函数。还是复杂？简化设计。 |

## Hermes Agent 集成

### 运行测试

在每个步骤用 `terminal` 工具运行测试：

```python
# RED — verify failure
terminal("pytest tests/test_feature.py::test_name -v")

# GREEN — verify pass
terminal("pytest tests/test_feature.py::test_name -v")

# Full suite — verify no regressions
terminal("pytest tests/ -q")
```

### 配合 delegate_task

派发子 agent 实现时，在 goal 中强制要求 TDD：

```python
delegate_task(
    goal="Implement [feature] using strict TDD",
    context="""
    Follow test-driven-development skill:
    1. Write failing test FIRST
    2. Run test to verify it fails
    3. Write minimal code to pass
    4. Run test to verify it passes
    5. Refactor if needed
    6. Commit

    Project test command: pytest tests/ -q
    Project structure: [describe relevant files]
    """,
    toolsets=['terminal', 'file']
)
```

### 配合 systematic-debugging

发现 bug？写一个能复现它的失败测试，遵循 TDD 循环。测试证明修复有效并防止回归。

永远不要在没有测试的情况下修复 bug。

## 测试反模式

- **测试 mock 行为而不是真实行为** — mock 应该验证交互，而不是替代被测系统
- **测试实现细节** — 测试行为/结果，而不是内部方法调用
- **只测正常路径** — 始终测试边界情况、错误和边界值
- **脆弱的测试** — 测试应该验证行为，而不是结构；重构不应该破坏测试

## 最终规则

```
Production code → test exists and failed first
Otherwise → not TDD
```

没有用户明确许可，不得例外。
