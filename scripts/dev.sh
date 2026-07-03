#!/usr/bin/env bash
# 一鍵啟動 React 交易終端（FastAPI 後端 + Vite 前端）
# 用法：bash scripts/dev.sh
set -e
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

echo "▶ 啟動 FastAPI 後端 (http://localhost:8000)…"
arch -arm64 .venv/bin/uvicorn api.main:app --reload --port 8000 &
API_PID=$!

echo "▶ 啟動 Vite 前端 (http://localhost:5173)…"
cd "$ROOT/frontend" && npm run dev &
VITE_PID=$!

trap "echo '停止…'; kill $API_PID $VITE_PID 2>/dev/null" INT TERM
echo ""
echo "✅ 交易終端：http://localhost:5173"
echo "   後端 API 文件：http://localhost:8000/docs"
echo "   按 Ctrl+C 停止"
wait
