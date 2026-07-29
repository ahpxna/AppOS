#!/bin/bash

echo "===== DOCKER VERSION FULL ====="
docker version

echo ""
echo "===== DOCKER INFO ====="
docker info

echo ""
echo "===== DOCKER CONTEXT ====="
docker context ls

echo ""
echo "===== DOCKER COMPOSE CHECK ====="
docker compose version
docker-compose version

echo ""
echo "===== DOCKER PLUGINS ====="
docker --help | grep -i compose || true
ls -la ~/.docker/cli-plugins 2>/dev/null || echo "no ~/.docker/cli-plugins"
ls -la /Applications/Docker.app/Contents/Resources/cli-plugins 2>/dev/null || echo "no Docker.app cli-plugins"

echo ""
echo "===== BASIC DOCKER TEST ====="
docker ps -a

echo ""
echo "===== DONE ====="
