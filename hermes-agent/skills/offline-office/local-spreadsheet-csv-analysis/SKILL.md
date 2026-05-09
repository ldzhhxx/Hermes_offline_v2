---
name: local-spreadsheet-csv-analysis
description: "用本地 Python/标准库分析 CSV/TSV/Excel 导出文本，生成摘要和清洗结果。"
version: 1.0.0
author: Hermes Offline
license: MIT
metadata:
  hermes:
    tags: [offline, office, local, chinese]
---

# 本地表格与 CSV 分析

## 适用场景
- 汇总 CSV/TSV 数据、统计行列、缺失值、重复值。
- 生成透视口径、分组统计、排序和异常值清单。
- 清洗编码、列名、空白字符，并导出新 CSV。

## 工作流程
1. 用文件工具确认文件存在、大小和编码；大文件先抽样。
2. 优先使用 Python 标准库 `csv`；若环境已有 pandas，可用于复杂分析，但不要启动阶段安装依赖。
3. 输出摘要：字段、样本行、行数、缺失、重复、关键指标。
4. 清洗或导出时写入新文件，不覆盖原文件，除非用户明确要求。

## 离线约束
- 不联网查询字段含义；字段解释只能基于用户说明或文件内容推断。

