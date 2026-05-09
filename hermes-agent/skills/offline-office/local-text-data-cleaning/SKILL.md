---
name: local-text-data-cleaning
description: "清洗本地 TXT/CSV/日志文本，去重、分段、抽取字段和生成样本。"
version: 1.0.0
author: Hermes Offline
license: MIT
metadata:
  hermes:
    tags: [offline, office, local, chinese]
---

# 本地文本数据清洗

## 适用场景
- 清洗日志、聊天记录、文本语料、问答数据。
- 去重、脱敏、分段、抽取字段、转换 JSONL/CSV。

## 工作流程
1. 先抽样查看格式和编码。
2. 写小脚本处理，保留原文件，输出到新文件。
3. 输出处理统计：输入行数、输出行数、去重数、异常数。
4. 对脱敏规则明确列出，不要删除无法确认的信息。

## 离线约束
- 不上传数据，不调用在线清洗或标注平台。

