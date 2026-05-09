---
name: meeting-minutes-offline
description: "把本地会议记录、转写文本或要点整理成纪要、待办和决议。"
version: 1.0.0
author: Hermes Offline
license: MIT
metadata:
  hermes:
    tags: [offline, office, local, chinese]
---

# 离线会议纪要整理

## 适用场景
- 根据本地转写文本整理会议纪要。
- 从聊天记录、要点列表中提取决议和待办。
- 生成适合内部流转的纪要模板。

## 输出结构
- 会议主题
- 时间/参会人（未知则留空）
- 背景
- 关键讨论
- 决议事项
- 待办清单：负责人、事项、截止时间、状态
- 风险与待确认问题

## 离线约束
- 不调用在线语音转写；只处理已有文本或本地文件。
- 不臆造参会人、时间和结论。

