#!/bin/bash

echo "===== CURRENT USER ====="
whoami
echo "HOME=$HOME"
echo "PWD=$(pwd)"

echo ""
echo "===== MAC INFO ====="
sw_vers 2>/dev/null || true
uname -m

echo ""
echo "===== DOCKER ====="
docker --version 2>/dev/null || echo "docker: NOT FOUND"
docker compose version 2>/dev/null || echo "docker compose: NOT FOUND"

echo ""
echo "===== EXISTING DOCKER CONTAINERS ====="
docker ps -a --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null || echo "docker ps: FAILED"

echo ""
echo "===== PORTS WE CARE ABOUT ====="
for port in 5432 5433 5678 9222 9223; do
  echo "--- checking port $port ---"
  lsof -nP -iTCP:$port -sTCP:LISTEN 2>/dev/null || echo "port $port free or not visible"
done

echo ""
echo "===== DONE ====="
