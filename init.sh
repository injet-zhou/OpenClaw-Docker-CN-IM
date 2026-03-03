#!/bin/bash

set -e

echo "=== OpenClaw 初始化脚本 ==="

OPENCLAW_HOME="/home/node/.openclaw"
OPENCLAW_WORKSPACE="${WORKSPACE:-/home/node/.openclaw/workspace}"
NODE_UID="$(id -u node)"
NODE_GID="$(id -g node)"

# 创建必要目录
mkdir -p "$OPENCLAW_HOME" "$OPENCLAW_WORKSPACE"

# 预检查挂载卷权限（避免同样命令偶发 Permission denied）
if [ "$(id -u)" -eq 0 ]; then
    CURRENT_OWNER="$(stat -c '%u:%g' "$OPENCLAW_HOME" 2>/dev/null || echo unknown:unknown)"
    echo "挂载目录: $OPENCLAW_HOME"
    echo "当前所有者(UID:GID): $CURRENT_OWNER"
    echo "目标所有者(UID:GID): ${NODE_UID}:${NODE_GID}"

    if [ "$CURRENT_OWNER" != "${NODE_UID}:${NODE_GID}" ]; then
        echo "检测到宿主机挂载目录所有者与容器运行用户不一致，尝试自动修复..."
        chown -R node:node "$OPENCLAW_HOME" || true
    fi

    # 再次验证写权限，失败则给出明确诊断
    if ! gosu node test -w "$OPENCLAW_HOME"; then
        echo "❌ 权限检查失败：node 用户无法写入 $OPENCLAW_HOME"
        echo "请在宿主机执行（Linux）："
        echo "  sudo chown -R ${NODE_UID}:${NODE_GID} <your-openclaw-data-dir>"
        echo "或在启动时显式指定用户："
        echo "  docker run --user \$(id -u):\$(id -g) ..."
        echo "若宿主机启用了 SELinux，请在挂载卷后添加 :z 或 :Z"
        exit 1
    fi
fi

# 全量同步配置逻辑
sync_config_with_env() {
    local config_file="/home/node/.openclaw/openclaw.json"
    
    # 如果文件不存在，创建一个基础骨架
    if [ ! -f "$config_file" ]; then
        echo "配置文件不存在，创建基础骨架..."
        cat > "$config_file" <<EOF
{
  "meta": { "lastTouchedVersion": "2026.2.14" },
  "update": { "checkOnStart": false },
  "browser": {
    "headless": true,
    "noSandbox": true,
    "defaultProfile": "openclaw",
    "executablePath": "/usr/bin/chromium"
  },
  "models": { "mode": "merge", "providers": { "default": { "models": [] } } },
  "agents": {
    "defaults": {
      "compaction": { "mode": "safeguard" },
      "elevatedDefault": "full",
      "maxConcurrent": 4,
      "subagents": { "maxConcurrent": 8 }
    }
  },
  "messages": { "ackReactionScope": "group-mentions", "tts": { "edge": { "voice": "zh-CN-XiaoxiaoNeural" } } },
  "commands": { "native": "auto", "nativeSkills": "auto" },
  "channels": {},
  "plugins": { "entries": {}, "installs": {} },
    "memory": {
      "backend": "qmd",
      "qmd": {
        "command": "/usr/local/bin/qmd",
        "paths": [
          {
            "path": "/home/node/.openclaw/workspace",
            "name": "workspace",
            "pattern": "**/*.md"
          }
        ]
      }
    }
}
EOF
    fi

    echo "正在根据当前环境变量同步配置状态..."
    python3 /usr/local/bin/sync_config.py "$config_file"
}

sync_config_with_env

# 确保所有文件和目录的权限正确（仅 root 可执行）
if [ "$(id -u)" -eq 0 ]; then
    chown -R node:node "$OPENCLAW_HOME" || true
fi

echo "=== 初始化完成 ==="
if [ "${SYNC_MODEL_CONFIG:-true}" = "false" ]; then
    echo "模型配置: 手动模式 (跳过环境变量同步)"
else
    # 简单的 shell 逻辑来处理可能的 provider 前缀
    FINAL_MID="${MODEL_ID:-gpt-4o}"
    [[ "$FINAL_MID" != */* ]] && FINAL_MID="default/$FINAL_MID"
    
    FINAL_IMID="${IMAGE_MODEL_ID:-${MODEL_ID:-gpt-4o}}"
    [[ "$FINAL_IMID" != */* ]] && FINAL_IMID="default/$FINAL_IMID"

    echo "当前主模型: $FINAL_MID"
    echo "当前图片模型: $FINAL_IMID"
    [ -n "$MODEL2_API_KEY" ] && echo "备用提供商: ${MODEL2_NAME:-model2} (已启用)"
fi
echo "API 协议: ${API_PROTOCOL:-openai-completions}"
echo "Base URL: ${BASE_URL}"
echo "上下文窗口: ${CONTEXT_WINDOW:-200000}"
echo "最大 Tokens: ${MAX_TOKENS:-8192}"
echo "Gateway 端口: $OPENCLAW_GATEWAY_PORT"
echo "Gateway 绑定: $OPENCLAW_GATEWAY_BIND"
echo "Gateway 模式: ${OPENCLAW_GATEWAY_MODE:-local}"
echo "Gateway 允许域: ${OPENCLAW_GATEWAY_ALLOWED_ORIGINS:-未设置}"
echo "Gateway 允许不安全认证: ${OPENCLAW_GATEWAY_ALLOW_INSECURE_AUTH:-true}"
echo "Gateway 禁用设备认证: ${OPENCLAW_GATEWAY_DANGEROUSLY_DISABLE_DEVICE_AUTH:-false}"
echo "插件启用: ${OPENCLAW_PLUGINS_ENABLED:-true}"
echo "允许插件列表已由系统自动同步"

# 安装 bun
export BUN_INSTALL="/usr/local"
export PATH="$BUN_INSTALL/bin:$PATH"

# 启动 OpenClaw Gateway（切换到 node 用户）
echo "=== 启动 OpenClaw Gateway ==="

export DBUS_SESSION_BUS_ADDRESS=/dev/null

# 定义清理函数
cleanup() {
    echo "=== 接收到停止信号,正在关闭服务 ==="
    if [ -n "$GATEWAY_PID" ]; then
        kill -TERM "$GATEWAY_PID" 2>/dev/null || true
        wait "$GATEWAY_PID" 2>/dev/null || true
    fi
    echo "=== 服务已停止 ==="
    exit 0
}

# 捕获终止信号
trap cleanup SIGTERM SIGINT SIGQUIT

# 启动网关
gosu node env HOME=/home/node DBUS_SESSION_BUS_ADDRESS=/dev/null \
    BUN_INSTALL="/usr/local" PATH="/usr/local/bin:$PATH" \
    openclaw gateway run \
    --bind "$OPENCLAW_GATEWAY_BIND" \
    --port "$OPENCLAW_GATEWAY_PORT" \
    --token "$OPENCLAW_GATEWAY_TOKEN" \
    --verbose &
GATEWAY_PID=$!

echo "=== OpenClaw Gateway 已启动 (PID: $GATEWAY_PID) ==="

# 主进程等待子进程
wait "$GATEWAY_PID"
EXIT_CODE=$?

echo "=== OpenClaw Gateway 已退出 (退出码: $EXIT_CODE) ==="
exit $EXIT_CODE
