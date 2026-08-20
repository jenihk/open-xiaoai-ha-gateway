# AGENTS.md

> 小爱音箱 ↔ Home Assistant 的语音网关。
>
> 接管音箱音频输入输出，把唤醒后识别的文本发送给 HA 的
> `conversation/process`（如 extended OpenAI Conversation agent），并把
> HA 的语音回复通过 TTS（小爱原生或豆包）播放出来。

## 系统架构

```
小爱音箱 (open-xiaoai-client, Rust)
   │  WebSocket :4399（PCM 音频流 + 事件 + RPC）
   ▼
open-xiaoai-server (本项目)
   ├─ native/  Rust PyO3 扩展：WebSocket 服务端、TTS 播放、shell RPC
   ├─ core/xiaoai.py  设备接入：on_input_data / on_event / on_output_data
   ├─ core/services/audio/  VAD(KWS) / KWS / ASR（local_asr）
   ├─ core/wakeup_session.py  唤醒状态机
   ├─ core/ha_conversation.py 连续对话循环（VAD → ASR → HA → TTS）
   ├─ core/ha.py  HA Assist 客户端（conversation/process）
   └─ core/services/api_server.py  HTTP API（可选）
        │
        ▼  HTTP POST /api/conversation/process（Bearer token）
Home Assistant（extended OpenAI Conversation 等 conversation agent）
```

## 项目结构

```
open-xiaoai-server/
├── main.py                        # 入口：解析环境变量，启动 MainApp
├── config.py                      # 用户配置（唤醒词、路由钩子、HA、TTS 等）
├── core/
│   ├── app.py                     # MainApp 主控制器（单例，管理生命周期）
│   ├── xiaoai.py                  # XiaoAI 设备接入 / 事件桥接
│   ├── ha.py                      # HA Assist 客户端（conversation/process）
│   ├── ha_conversation.py         # HA 连续对话控制器（VAD→ASR→HA→TTS）
│   ├── xiaoai_conversation.py     # 小爱原生连续对话策略
│   ├── wakeup_session.py          # 唤醒会话状态机
│   ├── ref.py                     # 全局引用注册表（get/set 依赖注入）
│   ├── assets/sounds/             # 音效（tts_notify.mp3 等）
│   ├── services/
│   │   ├── speaker.py             # SpeakerManager 音箱硬件控制
│   │   ├── api_server.py          # HTTP REST API（aiohttp）
│   │   ├── audio/                 # stream/VAD/KWS/ASR
│   │   ├── protocols/typing.py    # 常量定义
│   │   └── tts/doubao.py          # 豆包 TTS 客户端
│   └── utils/                     # config、logger、base、file
├── native/                        # Rust PyO3 扩展（maturin 编译）
│   └── src/
│       ├── lib.rs                 # 模块入口：start_server、on_output_data、
│       │                          #   start/stop_recording、start/stop_playing、
│       │                          #   run_shell、TTS 播放
│       ├── server.rs              # WebSocket 服务端（TCP :4399）
│       ├── python.rs              # Python 回调注册中心
│       └── tts/                   # 豆包 TTS 音频处理（流式 / PCM / MP3 解码）
├── skills/xiaoai-tts/             # Agent 工具：通过 HTTP API 控制小爱播放
├── scripts/                       # start.sh、generate_tts.py 等
└── tests/                         # 测试脚本
```

## 核心组件

### MainApp (core/app.py)

应用主控制器，单例模式，管理全部服务生命周期。

- `instance(enable_ha)` → 单例获取
- `run(enable_api_server)` → 启动 XiaoAI、HAManager、VAD/KWS、可选 API Server
- `schedule(callback)` → 主线程任务队列
- `send_to_ha(text, wait_response)` → 发送文本到 HA
- `send_to_ha_and_play_reply(text)` → 发送并 TTS 播报回复
- `set_ha_agent_id(agent_id)` → 运行时切换 HA agent（多 Agent 路由）
- `shutdown()` → 优雅关闭

### XiaoAI (core/xiaoai.py)

小爱音箱交互接口，classmethod 风格。

- `init_xiaoai()` → 初始化原生服务，注册事件处理
- `on_event(event)` → 处理小爱事件（RecognizeResult / AudioPlayer / playing）
- `on_input_data(data)` / `on_output_data(data)` → 麦克风 / 扬声器音频回调
- `run_shell(script, timeout)` → 远端 shell 执行

### HAManager (core/ha.py)

Home Assistant Assist 客户端。

- `process(text)` → 调用 `POST /api/conversation/process`，返回
  `(reply_text, continue_conversation)`
- `set_agent_id(agent_id)` → 切换对话 agent（多 Agent 路由）
- `reset_session()` → 每次唤醒前重置路由并开启新会话
- `send(text)` / `send_and_play_reply(text)` → 供 before_wakeup 钩子调用
- `play_response_with_tts(text)` → 豆包 TTS 或小爱原生 TTS 播报

内部机制：
- 每个 agent 独立保存 HA 返回的 `conversation_id`，多轮上下文由 HA 维护
- `continue_conversation=true` 时控制器保持监听，等待用户追问
- 使用 `aiohttp` 懒创建会话，超时由 `ha.response_timeout` 控制

### HAAssistConversationController (core/ha_conversation.py)

连续对话控制器。唤醒词触发后进入 VAD → ASR → HA → TTS 循环。

- `start()` → 进入对话模式
- `stop()` → 退出对话
- `is_active()` → 状态查询

对话循环（`_run_one_turn`）：
1. VAD 检测语音开始（`_wait_for_speech`）
2. 录制完整语音（VAD 静音 hook）
3. SherpaASR 离线识别（`local_asr`）或小爱原生识别（`xiaoai_asr`）
4. 退出关键词检测
5. 发送到 HA `conversation/process`
6. TTS 播放回复（阻塞等待完成）
7. 恢复监听

回声防护：
- `stop_recording` → kill 远端 arecord → 麦克风物理静音
- TTS 和提示音都在关键期播放，开麦后 VAD 从干净状态开始检测
- `VAD.resume()` 会 `_reset_state()` + `clear_input()`，清除旧帧

### WakeupSessionManager (core/wakeup_session.py)

唤醒会话状态机，协调 KWS → `before_wakeup` 钩子 → HA 连续对话。

- `wakeup(text, source)` → 处理唤醒（调用 `before_wakeup`，路由到 HA）
- `on_interrupt()` → 喊「小爱同学」时：取消 HA 任务、停止设备播放、
  恢复录音、停止 XiaoAI conversation
- `consume_xiaoai_asr_result()` → 把原生 ASR 结果路由给 HA 控制器

路由规则（`before_wakeup` 返回值）：
- `"ha"` → 走 HA 连续对话
- `None` → 不处理（用户自行处理，如单次指令）

### SpeakerManager (core/services/speaker.py)

音箱硬件控制。

- `play(text, url, buffer, blocking, timeout)` → 播放文字 / URL / PCM 缓冲
- `stop_device_audio()` → 停止设备上的播放链路（阻塞 TTS / 非阻塞 TTS / PCM）
- `wake_up(awake, silent)` → 唤醒 / 休眠小爱
- `abort_xiaoai()` → 中断小爱当前操作（重启 mico_aivs_lab，恢复需 1-2s）
- `ask_xiaoai(text, silent)` → 让小爱执行指令
- `run_shell(command, timeout)` → RPC shell

### APIServer (core/services/api_server.py)

HTTP REST API（aiohttp），端口可配（默认 9092）。

| 端点 | 方法 | 功能 |
|------|------|------|
| `/api/play/text` | POST | 播放文本 |
| `/api/play/url` | POST | 播放 URL |
| `/api/play/file` | POST | 播放本地文件 |
| `/api/status` | GET | 获取设备状态 |
| `/api/wakeup` | POST | 唤醒设备 |
| `/api/interrupt` | POST | 中断播放 |
| `/api/health` | GET | 健康检查 |
| `/api/tts/doubao` | POST | 豆包 TTS 合成 |
| `/api/tts/doubao_voices` | GET | 获取音色列表 |

## 配置

### 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `HA_ENABLE` | 启用 Home Assistant 集成 | `1` |
| `API_SERVER_ENABLE` | 启用 HTTP API | 禁用 |
| `API_SERVER_HOST` / `API_SERVER_PORT` | API 监听地址/端口 | `127.0.0.1` / `9092` |
| `AUDIO_INPUT_ENABLE` | 启用音频输入（KWS 需要） | `1` |
| `OPEN_XIAOAI_TOKEN` | Client 鉴权 token，与音箱端一致 | 不鉴权 |
| `CONFIG_PATH` | 自定义 config.py 路径 | `./config.py` |
| `LOGLEVEL` | 日志级别 | `INFO` |

### config.py

核心是 `APP_CONFIG`：

- `wakeup`：唤醒词列表、`before_wakeup` / `after_wakeup` 钩子（按关键词
  路由到不同 HA agent 或会话）
- `kws` / `vad` / `asr`：唤醒词置信度、VAD 阈值、ASR 模型
- `audio_input`：麦克风增益
- `xiaoai`：小爱连续对话模式、退出关键词
- `ha`：HA 地址、token、agent_id、input_mode、退出词、TTS 音色
- `tts.doubao`：豆包 App ID / Access Key / 音色

配置文件支持热重载（MainApp 每秒轮询 mtime）。

## 运行

```bash
# 本地（uv）
uv run main.py

# 启用 HTTP API
API_SERVER_ENABLE=1 uv run main.py

# Docker
docker compose up -d
```

模型文件：`VAD + KWS` 必需（唤醒词检测）；ASR 模型仅在
`ha.input_mode="local_asr"` 时需要，`"xiaoai_asr"` 模式复用设备原生识别。
模型下载见 README「快速开始」。

## 开发规范

### 代码风格
- 中文注释和文档字符串
- 英文 commit message
- 类型提示：`dict[str, asyncio.Future]`

### 异步编程
- 所有 I/O 使用 `async/await`
- 线程安全使用 `asyncio.run_coroutine_threadsafe()`
- `MainApp.loop` 是业务协程主循环
- `XiaoAI.async_loop` 仅用于原生扩展回调桥接，不挂新业务状态机

### 日志规范
- 所有日志必须带模块标识：通过 `module=` 参数或 `[Module]` 前缀
- 使用 `core.utils.logger.logger`，禁止裸 `print`
- 调试输出用 `DEBUG` 级别
- 唯一允许的裸输出：启动 ASCII banner

### 全局引用 (ref.py)
- `set_app/get_app`, `set_xiaoai/get_xiaoai`
- `set_vad/get_vad`, `set_kws/get_kws`, `set_speaker/get_speaker`

## 测试

```bash
# 无音箱流式冒烟测试
python3 tests/test_tts_stream.py

# 对比长文本 mp3/pcm 流式时延
python3 tests/test_tts_latency.py --formats mp3,pcm --rounds 3 --repeat 8

# 唤醒词生成测试
python3 tests/test_wakeup_keywords.py
```

## 音箱设备控制命令

小爱音箱（LX06 等）基于 OpenWrt + busybox，设备端命令和行为如下：

### 音频播放通道

| 通道 | 进程/服务 | 触发方式 | 中断方式 |
|------|-----------|---------|---------|
| PCM 直通 | `aplay` | `open_xiaoai_server.start_playing()` → WebSocket stream | `open_xiaoai_server.stop_playing()` |
| 阻塞 TTS | `tts_play.sh` → `miplayer -f <file>` | `speaker.play(blocking=True)` | `killall tts_play.sh miplayer` |
| 非阻塞 TTS | `mibrain_service` | `speaker.play(blocking=False)` → `ubus call mibrain text_to_speech` | `mphelper pause` |
| 媒体播放器 | `mediaplayer` | `ubus call mediaplayer player_play_url` | `mphelper pause` |

### tts_play.sh 工作流程

1. `mphelper pause` — 暂停当前播放
2. `ubus call mibrain text_to_speech '{"text":"...","save":1}'` — 生成音频文件
3. `miplayer -f <path>` — 播放音频文件（子进程）
4. `rm <path>` — 清理临时文件

注意：杀掉 `tts_play.sh` 不会自动杀掉子进程 `miplayer`，必须同时
`killall miplayer`。

### 录音通道

| 操作 | 命令 | 说明 |
|------|------|------|
| 停止录音 | `open_xiaoai_server.stop_recording()` | 杀掉设备端 `arecord`，麦克风静音 |
| 恢复录音 | `open_xiaoai_server.start_recording()` | 重启 `arecord`，音频恢复流入 GlobalStream |

HA 连续对话中 TTS 播放时会 `stop_recording` 防回声。若在此期间触发中断
（"小爱同学"），必须在中断处理中调用 `start_recording` 恢复录音，否则 KWS
会因无音频数据而永久失效。

### 不可用的中断方式

- `abort_xiaoai()`（重启 `mico_aivs_lab`）— 会导致小爱整体不可用 1-2 秒
- `pkill miplayer` — busybox `pkill` 无法匹配 `miplayer` 进程名

## 参考资源

- 刷机教程: https://github.com/idootop/open-xiaoai/blob/main/docs/flash.md
- Client 端补丁: https://github.com/idootop/open-xiaoai/blob/main/packages/client-rust/README.md
- Home Assistant Conversation API:
  https://developers.home-assistant.io/docs/intent_conversation_api/
- extended OpenAI Conversation:
  https://github.com/jekalmin/extended_openai_conversation
