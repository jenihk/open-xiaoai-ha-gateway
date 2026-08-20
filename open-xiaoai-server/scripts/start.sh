#!/bin/bash
#
# Open-XiaoAI Bridge 启动脚本
# 用法: ./scripts/start.sh
#

set -e

# cd to project root (parent of scripts/)
cd "$(dirname "$0")/.."

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Open-XiaoAI Bridge 启动脚本${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

HA_ENABLE_VALUE=$(printf '%s' "${HA_ENABLE:-1}" | tr '[:upper:]' '[:lower:]')

# 1. 检查 uv
if ! command -v uv &> /dev/null; then
    echo -e "${RED}错误: 未找到 uv 命令${NC}"
    echo "请先安装 uv:"
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi
echo -e "${GREEN}✓ uv 已安装${NC}"

# 2. 同步虚拟环境和依赖
echo ""
echo "正在同步虚拟环境和依赖..."
# CMAKE_POLICY_VERSION_MINIMUM=3.5: cmake 4.x 移除了对 < 3.5 的兼容，
# audiopus_sys 的 CMakeLists.txt 版本声明过旧，需要此变量绕过检查
if CMAKE_POLICY_VERSION_MINIMUM=3.5 uv sync; then
    echo -e "${GREEN}✓ 虚拟环境已就绪，依赖已安装${NC}"
else
    echo -e "${RED}错误: 依赖安装失败${NC}"
    exit 1
fi

# Set DYLD_LIBRARY_PATH so sherpa-onnx can find libonnxruntime from onnxruntime package
ONNX_LIB_DIR="$(uv run python -c "from pathlib import Path; import onnxruntime; print(Path(onnxruntime.__file__).parent / 'capi')" 2>/dev/null)" && \
    export DYLD_LIBRARY_PATH="${ONNX_LIB_DIR}${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"

# 3. 检查 KWS 相关模型和关键词文件
if [[ "$HA_ENABLE_VALUE" =~ ^(1|true|yes|on)$ ]]; then
    MODEL_DIR="core/models"
    REQUIRED_MODELS=("silero_vad.onnx" "encoder.onnx" "decoder.onnx" "joiner.onnx" "tokens.txt" "bpe.model")
    MISSING_MODELS=()

    for model in "${REQUIRED_MODELS[@]}"; do
        if [ ! -f "$MODEL_DIR/$model" ]; then
            MISSING_MODELS+=("$model")
        fi
    done

    # ASR 模型仅在 ha.input_mode="local_asr" 时需要；
    # "xiaoai_asr" 模式复用设备原生识别，无需下载 ASR 模型。
    HA_INPUT_MODE=$(uv run python -c "
import sys
sys.path.insert(0, '.')
from core.utils.config_loader import load_config_module
m = load_config_module()
print((m.APP_CONFIG.get('ha', {}).get('input_mode') or 'local_asr'))
" 2>/dev/null || echo "local_asr")
    if [[ "$HA_INPUT_MODE" == "local_asr" ]]; then
        if ! ls "$MODEL_DIR"/sherpa-onnx-paraformer-*/model.int8.onnx &>/dev/null; then
            MISSING_MODELS+=("sherpa-onnx-paraformer-*/model.int8.onnx")
        fi
    fi

    if [ ${#MISSING_MODELS[@]} -eq 0 ]; then
        echo -e "${GREEN}✓ 模型文件已存在${NC}"
    else
        echo -e "${YELLOW}⚠ 缺少模型文件，正在自动下载...${NC}"
        for model in "${MISSING_MODELS[@]}"; do
            echo "  - $model"
        done
        echo ""

        # 创建模型目录
        mkdir -p "$MODEL_DIR"

        # 下载模型文件
        MODEL_URL="https://github.com/coderzc/open-xiaoai-bridge/releases/download/vad-kws-asr-models/models.zip"
        ZIP_FILE="$MODEL_DIR/models.zip"

        echo -e "${YELLOW}正在下载模型文件...${NC}"
        if command -v curl &> /dev/null; then
            curl -L -o "$ZIP_FILE" "$MODEL_URL"
        elif command -v wget &> /dev/null; then
            wget -O "$ZIP_FILE" "$MODEL_URL"
        else
            echo -e "${RED}错误: 需要 curl 或 wget 来下载模型文件${NC}"
            exit 1
        fi

        # 解压模型文件
        echo -e "${YELLOW}正在解压模型文件...${NC}"
        if command -v unzip &> /dev/null; then
            unzip -o "$ZIP_FILE" -d "$MODEL_DIR"
            rm "$ZIP_FILE"
        else
            echo -e "${RED}错误: 需要 unzip 来解压模型文件${NC}"
            echo "请手动解压: $ZIP_FILE"
            exit 1
        fi

        # 如果解压后有多一层 models 目录，移动文件到正确位置
        if [ -d "$MODEL_DIR/models" ]; then
            echo -e "${YELLOW}整理模型文件...${NC}"
            mv "$MODEL_DIR/models"/* "$MODEL_DIR/"
            rmdir "$MODEL_DIR/models"
        fi

        # 验证模型文件
        for model in "${REQUIRED_MODELS[@]}"; do
            if [ ! -f "$MODEL_DIR/$model" ]; then
                echo -e "${RED}错误: 模型文件 $model 下载或解压失败${NC}"
                exit 1
            fi
        done

        echo -e "${GREEN}✓ 模型文件下载并解压完成${NC}"
    fi

    echo ""
    echo -e "${YELLOW}生成关键词文件...${NC}"
    set +e
    keyword_output=$(uv run python core/services/audio/kws/keywords.py 2>&1)
    keyword_status=$?
    set -e
    if [ $keyword_status -eq 0 ]; then
        echo -e "${GREEN}✓ 关键词文件生成完成${NC}"
        if [ -n "$keyword_output" ]; then
            echo "$keyword_output"
        fi
    else
        echo -e "${RED}✗ 关键词文件生成失败${NC}"
        if [ -n "$keyword_output" ]; then
            echo "$keyword_output"
        fi
        exit 1
    fi
else
    echo -e "${YELLOW}⚠ Home Assistant 未启用，跳过模型检查和关键词预生成${NC}"
fi

# 4. 检查配置
echo ""
echo "检查配置..."

# 使用 config_loader 加载配置，支持 CONFIG_PATH 环境变量
uv run python -c "
import sys
import os
sys.path.insert(0, '.')

# 尝试使用 config_loader 加载配置
try:
    from core.utils.config_loader import load_config_module
    config_module = load_config_module()
    APP_CONFIG = getattr(config_module, 'APP_CONFIG', {})
except Exception as e:
    # 回退到直接导入
    try:
        from config import APP_CONFIG
    except ImportError:
        print('⚠ 无法加载配置，跳过配置检查')
        sys.exit(0)

doubao = APP_CONFIG.get('tts', {}).get('doubao', {})
api_key = doubao.get('api_key', '')

errors = []
if not api_key:
    errors.append('豆包 TTS api_key 未配置')

if errors:
    for e in errors:
        print(f'⚠ 警告: {e}')
    print('   文档: https://www.volcengine.com/docs/6561/1598757')
    print('   提示: 没有配置也可以使用，但 doubao TTS 功能将无法使用')
else:
    print('✓ 豆包 TTS 已配置')
" 2>/dev/null || echo -e "${YELLOW}⚠ 配置检查失败${NC}"

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  启动 Open-XiaoAI Bridge...${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

uv run python main.py "$@"
