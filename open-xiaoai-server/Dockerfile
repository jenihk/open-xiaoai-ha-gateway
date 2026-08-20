# syntax=docker/dockerfile:1

# ============ 构建参数 ============
# 国内网络拉取 Docker Hub / ghcr.io 超时时，可通过 build-arg 覆盖为镜像源，例如：
#   docker compose build \
#     --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim \
#     --build-arg UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple \
#     --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG BASE_IMAGE=python:3.12-slim
ARG RUSTUP_DIST_SERVER=https://static.rust-lang.org
ARG RUSTUP_UPDATE_ROOT=https://static.rust-lang.org/rustup
ARG UV_DEFAULT_INDEX=https://pypi.org/simple
ARG APT_MIRROR=mirrors.tuna.tsinghua.edu.cn
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

# ============ 构建阶段 ============
FROM ${BASE_IMAGE} AS builder

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# 在 FROM 之后的构建阶段中重新声明 ARG 以继承顶层值
ARG RUSTUP_DIST_SERVER
ARG RUSTUP_UPDATE_ROOT
ARG UV_DEFAULT_INDEX
ARG APT_MIRROR
ARG PIP_INDEX_URL

ENV RUSTUP_DIST_SERVER=${RUSTUP_DIST_SERVER} \
    RUSTUP_UPDATE_ROOT=${RUSTUP_UPDATE_ROOT} \
    UV_DEFAULT_INDEX=${UV_DEFAULT_INDEX} \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    DEBIAN_FRONTEND=noninteractive

# Debian 源换国内镜像（可通过 --build-arg APT_MIRROR=deb.debian.org 关闭）
RUN sed -i "s|deb.debian.org|${APT_MIRROR}|g; s|security.debian.org|${APT_MIRROR}|g" \
        /etc/apt/sources.list.d/debian.sources 2>/dev/null || true

# 构建依赖：curl（装 Rust）+ gcc（cargo 链接器）+ libc6-dev（glibc 开发文件：
# crt 启动对象与链接脚本，rust-lld 链接必需）+ patchelf（maturin 必需）
# + pkg-config。即使依赖全是纯 Rust crate，cargo 链接 build script 和
# cdylib 时仍需系统 C 工具链；不需要完整的 build-essential。
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gcc libc6-dev patchelf pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Rust 工具链（编译 native 扩展需要）
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y

# uv 包管理器：用 pip 安装（避免 COPY --from 变量在旧版 builder 上不展开）
RUN pip install --no-cache-dir uv

# 国内 Cargo 镜像（与宿主 ~/.cargo/config.toml 一致），拉取 crates 依赖更快
RUN mkdir -p /root/.cargo \
    && printf '[source.crates-io]\nreplace-with = "sjtu"\n\n[source.sjtu]\nregistry = "sparse+https://mirrors.sjtug.sjtu.edu.cn/crates.io-index/"\n' \
        > /root/.cargo/config.toml

ENV BASH_ENV=/root/.bash_env
RUN touch "$BASH_ENV" \
    && echo '. "$BASH_ENV"' >> "$HOME/.bashrc" \
    && echo '[ -s "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"' >> "$BASH_ENV"

WORKDIR /app

# 构建上下文是仓库上一级（code/），同时包含 server 与 client。
# native/Cargo.toml 通过 path = "../../open-xiaoai-client" 引用客户端源码，
# 保持与本地一致的目录结构，构建时无需联网克隆客户端仓库。
COPY open-xiaoai-server ./open-xiaoai-server
COPY open-xiaoai-client ./open-xiaoai-client

WORKDIR /app/open-xiaoai-server

# 安装依赖并构建/安装 native 扩展（maturin 后端，生成 open_xiaoai_server）。
# 不带 --locked：仓库 uv.lock 为 v1 格式，新版 uv 需要 v2，构建时让 uv
# 自动迁移锁文件（仅影响镜像内文件，不修改仓库）。
RUN uv sync --no-editable

# ============ 运行阶段 ============
FROM ${BASE_IMAGE}

WORKDIR /app/open-xiaoai-server

ARG APT_MIRROR

ENV DEBIAN_FRONTEND=noninteractive

# Debian 源换国内镜像（与构建阶段一致）
RUN sed -i "s|deb.debian.org|${APT_MIRROR}|g; s|security.debian.org|${APT_MIRROR}|g" \
        /etc/apt/sources.list.d/debian.sources 2>/dev/null || true

# 运行时依赖：onnxruntime / sherpa-onnx 需要的基础库
RUN apt-get update && apt-get install -y --no-install-recommends \
    libzstd1 ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/open-xiaoai-server/.venv /app/open-xiaoai-server/.venv
# 运行文件从构建阶段复制，避免依赖构建上下文路径（上下文为仓库根，
# server 与 client 同级，直接 COPY main.py 会找不到文件）
COPY --from=builder /app/open-xiaoai-server/main.py /app/open-xiaoai-server/main.py
COPY --from=builder /app/open-xiaoai-server/config.py /app/open-xiaoai-server/config.py
COPY --from=builder /app/open-xiaoai-server/core /app/open-xiaoai-server/core

# 确保 sherpa_onnx 能通过 onnxruntime 共享库名找到运行库
RUN set -eux; \
    ort_dir="/app/open-xiaoai-server/.venv/lib/python3.12/site-packages/onnxruntime/capi"; \
    if [ ! -f "$ort_dir/libonnxruntime.so" ]; then \
      ln -s "$(basename "$(ls "$ort_dir"/libonnxruntime.so.* | head -n 1)")" "$ort_dir/libonnxruntime.so"; \
    fi

ENV PATH=/app/open-xiaoai-server/.venv/bin:$PATH \
    LD_LIBRARY_PATH=/app/open-xiaoai-server/.venv/lib/python3.12/site-packages/onnxruntime/capi \
    PYTHONUNBUFFERED=1

# 音箱 Client WebSocket :4399；HTTP API :9092（可选）
EXPOSE 4399 9092

# 先初始化唤醒词文件（HA 启用时），再启动主程序
CMD ["/bin/bash", "-c", "HA_VAL=\"${HA_ENABLE:-1}\"; if [[ \"$HA_VAL\" =~ ^(1|true|yes|on)$ ]]; then python core/services/audio/kws/keywords.py; fi && exec python main.py"]
