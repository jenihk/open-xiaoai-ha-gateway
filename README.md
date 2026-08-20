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

## 🤔 为什么基于 bridge 精简改造

本项目是 [open-xiaoai-bridge](https://github.com/coderzc/open-xiaoai-bridge)
在「只服务 Home Assistant 用户」这个定位下的精简与改造，两者各有取舍：

**本项目的优势（轻量、开箱即用）**

- **更轻量**：只保留「唤醒 → ASR → HA 对话 → TTS」一条链路，移除了
  OpenClaw、OpenAI 兼容、QwenPaw、小智 AI 等多后端代码，代码量和依赖
  都大幅缩减；
- **无需部署龙虾（OpenClaw）+ skill**：不用额外跑 OpenClaw 网关、不用配置
  skill 播报，装好 Home Assistant + 豆包 TTS 即可使用，适合纯家居控制场景；
- **部署维护成本低**：一个 Docker 镜像 + 一个 `config.py`；HAOS 上还有
  add-on 一键安装，手机 UI 直接改配置；
- **链路短、排障简单**：所有环节（唤醒词、ASR、路由、音色）都在自己手里，
  出问题容易定位。

**bridge 项目的优势（功能更全、扩展性更强）**

- **多后端自由组合**：同时接入 OpenClaw / OpenAI 兼容 / QwenPaw / 小智 AI，
  一个音箱可以切换多个 AI 服务；
- **Agent 生态**：配合 OpenClaw + skill 可实现联网检索、工具调用、Agent
  自主播报等复杂任务，适合把音箱接入完整 Agent 工作流的用户；
- **更通用**：不依赖 Home Assistant，没有 HA 的用户也能用。

**怎么选**

- 用 Home Assistant 控制家居、想开箱即用 → 本项目；
- 需要多 AI 后端或 Agent 化能力 → 直接用
  [open-xiaoai-bridge](https://github.com/coderzc/open-xiaoai-bridge)。

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
