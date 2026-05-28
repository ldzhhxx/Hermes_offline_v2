#!/usr/bin/env bash
# minio_purge.sh — 彻底清除 MINIO_PREFIX 下的所有对象（包括零字节目录标记）
# 用法: bash scripts/minio_purge.sh [--confirm]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec python3 "$SCRIPT_DIR/minio_sync.py" purge "$@"
