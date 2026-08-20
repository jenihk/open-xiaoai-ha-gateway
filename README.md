<div align="center">

# Open-XiaoAI HA Gateway

**小爱音箱 → Home Assistant 的语音网关**

自定义唤醒词唤醒后，语音经本地 ASR 转成文本发送给 HA 的 conversation agent
控制智能设备，回复再通过 TTS（小爱原生或豆包音色）合成音频流推给小爱播放。

</div>

---

## ✨ 功能

- 🎙️ 自定义唤醒词（KWS 本地识别，中英文均可）
- 🧠 多 Agent 路由（不同唤醒词 → 不同 HA conversation agent，上下文隔离）
- 💬 连续对话（一次唤醒多轮对话，可随时打断）
- 🗣️ 按 agent 指定 TTS 音色（小爱原生 / 豆包多音色）
- 🏠 通过 HA Assist 自然语言控制家中智能设备
- 🐳 Docker 部署 + HAOS add-on（可选）

## 📦 仓库结构

| 目录 | 说明 |
|------|------|
| [open-xiaoai-server](open-xiaoai-server/README.md) | 服务端（语音网关核心，含 Docker 部署） |
| [open-xiaoai-client](open-xiaoai-client/README.md) | 客户端（运行在小爱音箱上的 Rust 补丁程序） |

> 💡 HAOS add-on 仓库：[ha-addon-open-xiaoai](https://github.com/jenihk/ha-addon-open-xiaoai)
> （验证中，稍后发布）

## 🚀 快速开始

完整部署说明见 [open-xiaoai-server/README.md](open-xiaoai-server/README.md)：

1. 在小爱音箱上安装 Client（刷机 + 运行 Rust 客户端，参考
   [idootop/open-xiaoai](https://github.com/idootop/open-xiaoai) 的教程）；
2. 准备模型文件（VAD + KWS + Paraformer ASR，见服务端 README「模型文件」）；
3. 编辑服务端 `config.py`，填写 `ha.token` 与豆包 `api_key`；
4. `docker compose up -d --build` 或本地 `uv run main.py` 启动。

## 🙏 致谢

本项目由以下开源项目启发/改造而来：

- [open-xiaoai-bridge](https://github.com/coderzc/open-xiaoai-bridge)（MIT）—
  服务端基础，本项目移除了 OpenClaw / OpenAI 兼容 / QwenPaw / 小智 AI 等
  后端，仅保留 Home Assistant 语音对接；
- [open-xiaoai](https://github.com/idootop/open-xiaoai)（MIT）—
  `open-xiaoai-client` 客户端源码来源，负责音箱端音频采集/播放；
- [extended_openai_conversation](https://github.com/jekalmin/extended_openai_conversation)
  — 推荐的 HA conversation agent。

## 📄 License

本项目基于 [open-xiaoai-bridge](https://github.com/coderzc/open-xiaoai-bridge)
（MIT License）精简改造，遵循 [MIT License](LICENSE)。

MIT 版权声明（原项目）已保留在 [LICENSE](LICENSE) 中：

```text
Copyright (c) 2024 Del Wang
Copyright (c) 2025-present coderzc
```

`open-xiaoai-client` 目录源码来自
[idootop/open-xiaoai](https://github.com/idootop/open-xiaoai)（MIT License），
版权归原作者所有，完整许可文本见上游仓库。
