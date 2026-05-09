---
name: offline-document-drafting
description: "起草、润色、摘要、改写本地 Markdown/TXT/HTML 文档，不依赖在线服务。"
version: 1.0.0
author: Hermes Offline
license: MIT
metadata:
  hermes:
    tags: [offline, office, local, chinese]
---

# 离线文档起草与改写

## 适用场景
- 起草通知、方案、制度、周报、邮件草稿、会议材料。
- 将零散要点整理成正式文档。
- 对本地 Markdown/TXT/HTML 文档做摘要、润色、改写。

## 工作流程
1. 明确目标读者、用途、语气和篇幅。若用户已提供文件路径，优先读取文件内容。
2. 先给出结构，再填充正文；长文档按“标题、背景、目标、方案、风险、下一步”组织。
3. 输出时优先使用 Markdown 标题和列表，避免宽表格。
4. 如需保存文件，写入 workspace 或用户指定路径，并说明路径。

## 离线约束
- 不调用搜索、网页、云文档、在线翻译或外部 API。
- 缺少事实依据时明确标注“基于用户提供材料整理”。

