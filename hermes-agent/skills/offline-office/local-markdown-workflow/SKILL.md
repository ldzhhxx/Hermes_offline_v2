---
name: local-markdown-workflow
description: "创建、校对、拆分、合并和导出 Markdown 文档。"
version: 1.0.0
author: Hermes Offline
license: MIT
metadata:
  hermes:
    tags: [offline, office, local, chinese]
---

# 本地 Markdown 办公流

## 适用场景
- 创建 README、操作手册、SOP、FAQ、知识库条目。
- 合并多个 Markdown 文件，统一标题层级和格式。
- 检查链接、目录、代码块和列表格式。

## 工作流程
1. 读取目标文档，保留原有重要结构。
2. 统一标题层级，修复列表缩进和代码块语言标记。
3. 对超长文档可按章节拆分，并生成索引。
4. 写回前给出变更摘要。

## 离线约束
- 外部链接只做文本保留，不联网验证。

