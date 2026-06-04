#!/usr/bin/env bash
# ============================================================================
# k8sAgent 启动脚本 (Ubuntu/Linux)
# 用法:
#   ./scripts/start.sh          # 前台运行
#   ./scripts/start.sh --bg     # 后台运行
#   ./scripts/start.sh --install # 仅安装依赖
#   ./scripts/start.sh --stop   # 停止后台实例
# ============================================================================

set -e

# --- 配置 -------------------------------------------------
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
PYTHON_BIN="python3.12"
HOST="${K8SAGENT_HOST:-0.0.0.0}"
PORT="${K8SAGENT_PORT:-8080}"
PID_FILE="$PROJECT_DIR/.k8sagent.pid"
LOG_FILE="$PROJECT_DIR/logs/k8sagent.log"
PIP_MIRROR="https://mirrors.aliyun.com/pypi/simple/"

# --- 颜色输出 ----------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; }

# --- 检查 Python -------------------------------------------
check_python() {
    if ! command -v "$PYTHON_BIN" &>/dev/null; then
        err "未找到 $PYTHON_BIN，请先安装 Python 3.12"
        echo "  sudo apt install python3.12 python3.12-dev python3.12-venv -y"
        exit 1
    fi
    info "Python: $($PYTHON_BIN --version)"
}

# --- 创建虚拟环境 ------------------------------------------
setup_venv() {
    if [ ! -f "$VENV_DIR/bin/python" ]; then
        info "创建虚拟环境..."
        "$PYTHON_BIN" -m venv "$VENV_DIR"
    fi
    source "$VENV_DIR/bin/activate"
    info "虚拟环境已就绪: $VIRTUAL_ENV"
}

# --- 安装依赖 ----------------------------------------------
install_deps() {
    setup_venv

    # 检查是否已安装
    if python -c "import k8s_agent" 2>/dev/null; then
        info "k8s-agent 已安装，跳过。如需重新安装: pip install -e ."
        return
    fi

    info "安装项目依赖 (镜像源: $PIP_MIRROR)..."
    pip install --upgrade pip -i "$PIP_MIRROR" --trusted-host mirrors.aliyun.com -q
    pip install -e . -i "$PIP_MIRROR" --trusted-host mirrors.aliyun.com
    info "依赖安装完成"
}

# --- 创建日志目录 ------------------------------------------
mkdir -p "$PROJECT_DIR/logs"

# --- 检查 kubectl ------------------------------------------
check_k8s() {
    if command -v kubectl &>/dev/null; then
        if kubectl cluster-info &>/dev/null 2>&1; then
            info "Kubernetes 连接正常"
        else
            warn "kubectl 已安装但无法连接集群，请检查 kubeconfig"
        fi
    else
        warn "未找到 kubectl，K8s 操作将不可用"
    fi
}

# --- 启动服务 ----------------------------------------------
do_start() {
    local run_bg="${1:-false}"

    check_python
    check_k8s
    install_deps

    info "启动 K8s Agent Web UI 在 http://${HOST}:${PORT}"

    if [ "$run_bg" = "true" ]; then
        # 检查是否已经在运行
        if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
            warn "已有实例在运行 (PID: $(cat "$PID_FILE"))"
            exit 0
        fi

        nohup python -m uvicorn k8s_agent.api.app:create_app --factory \
            --host "$HOST" --port "$PORT" \
            > "$LOG_FILE" 2>&1 &

        echo $! > "$PID_FILE"
        info "后台运行中 (PID: $!)"
        info "日志: tail -f $LOG_FILE"
    else
        info "前台运行，按 Ctrl+C 停止"
        python -m uvicorn k8s_agent.api.app:create_app --factory \
            --host "$HOST" --port "$PORT"
    fi
}

# --- 停止服务 ----------------------------------------------
do_stop() {
    if [ ! -f "$PID_FILE" ]; then
        warn "未找到 PID 文件，没有运行中的实例"
        exit 0
    fi

    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        info "停止 k8sAgent (PID: $PID)..."
        kill "$PID"
        sleep 1
        if kill -0 "$PID" 2>/dev/null; then
            warn "进程未响应，强制终止..."
            kill -9 "$PID"
        fi
        rm -f "$PID_FILE"
        info "已停止"
    else
        warn "进程 $PID 已不存在"
        rm -f "$PID_FILE"
    fi
}

# --- 主入口 ------------------------------------------------
case "${1:-}" in
    --bg|--background)
        do_start true
        ;;
    --install)
        check_python
        install_deps
        ;;
    --stop)
        do_stop
        ;;
    --restart)
        do_stop
        sleep 2
        do_start true
        ;;
    *)
        do_start false
        ;;
esac
