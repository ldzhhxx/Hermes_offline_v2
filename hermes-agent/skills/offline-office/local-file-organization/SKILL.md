---
name: local-file-organization
description: "按规则整理 workspace 内文件，生成目录结构、索引和归档建议。"
version: 1.0.0
author: Hermes Offline
license: MIT
metadata:
  hermes:
    tags: [offline, office, local, chinese]
---

# 本地文件整理归档

## 适用场景
- 整理 workspace、项目资料、合同、报告、图片和临时文件。
- 生成目录树、文件清单、归档方案。
- 批量重命名或移动文件前做预案。

## 工作流程
1. 先只扫描并输出计划，不直接移动/删除。
2. 按类型、日期、项目、版本等规则设计目录。
3. 对危险操作（删除、覆盖、批量移动）必须先给出清单并等待用户确认。
4. 完成后再次列出目录结构验证。

## 离线约束
- 只使用本地文件系统。
- 不上传文件，不调用云盘或在线 OCR。

