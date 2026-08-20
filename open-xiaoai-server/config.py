# 唤醒词 -> HA conversation agent 的路由表。
# 在 Home Assistant 中创建多个 conversation agent（如 extended OpenAI
# Conversation 的多个实例），把各自的 agent_id 填到这里，即可实现
# 不同唤醒词进入不同 agent 的对话，多轮上下文互相隔离。
AGENT_ROUTES = {
    "海绵宝宝": "conversation.hai_mian_bao_bao",
    "你好小薇": "conversation.ni_hao_xiao_wei",
}


async def before_wakeup(speaker, text, source, app):
    """
    处理收到的用户消息，并决定是否唤醒 Home Assistant Assist。

    参数：
        speaker : SpeakerManager，可调用 play/abort_xiaoai/wake_up 等方法
        text    : 识别到的文字内容
        source  : 唤醒来源
                    'kws'     -- 本地关键词唤醒（用户说了唤醒词）
                    'xiaoai'  -- 小爱同学收到用户语音指令
        app     : MainApp 实例，可调用 send_to_ha / send_to_ha_and_play_reply /
                  set_ha_agent_id 等方法

    返回值：
        "ha"  -> 进入 Home Assistant 连续对话流程
        None  -> 不做额外处理（可在函数内自行调用 app.send_to_ha 等）
    """
    if source == "kws":
        from core.ha import HAManager

        # 按唤醒词路由到对应的 HA conversation agent
        for keyword, agent_id in AGENT_ROUTES.items():
            if keyword in text:
                app.set_ha_agent_id(agent_id)
                # 唤醒应答跟随该 agent 的 TTS 音色（session_tts_speakers）
                await HAManager.play_response_with_tts(f"{keyword}来了")
                return "ha"

        # 未匹配到路由表的唤醒词：仍进入 HA 连续对话（默认 agent）
        await HAManager.play_response_with_tts("来了")
        return "ha"

    if source == "xiaoai":
        # 通过小爱原生识别接管对话
        if text == "召唤小爱":
            await speaker.abort_xiaoai()
            return "ha"

        # 单次指令：让小爱把指令转给 HA，回复直接播报
        if "让小爱" in text:
            await speaker.abort_xiaoai()
            await app.send_to_ha_and_play_reply(text.replace("让小爱", ""))
            return None

    return None


async def after_wakeup(speaker, source=None, session_key=None):
    """
    退出唤醒状态时的提示语。
    - source: 退出来源（"ha" 等）
    - session_key: 当前 HA agent id
    """
    if source == "ha":
        # 提示语跟随 ha.tts_speaker 音色（豆包/小爱原生由配置决定）
        from core.ha import HAManager

        await HAManager.play_response_with_tts("再见")


APP_CONFIG = {
    "wakeup": {
        # 自定义唤醒词列表（英文字母要全小写）
        # 建议使用与「小爱同学」不同的自定义词，避免和音箱原生唤醒冲突
        "keywords": [
            "海绵宝宝",
            "你好小薇",
        ],
        # 连续对话中静音多久后自动退出（秒）：
        # 用户说完一句后，在这个时间内没有继续说话就自动结束对话。
        "timeout": 12,
        # 语音识别结果回调
        "before_wakeup": before_wakeup,
        # 退出唤醒时的提示语（设置为空可关闭）
        "after_wakeup": after_wakeup,
        # 唤醒后额外执行的"停止播放"命令（可留空）。
        # 默认 stop_device_audio 只停本地 TTS/媒体播放器（mphelper pause、
        # killall miplayer），覆盖不到蓝牙 A2DP 音乐。把在音箱上实测有效
        # 的蓝牙停止命令填在这里，唤醒时会先执行再进入对话。
        # 例如："ubus call mediaplayer player_play_control '{\"action\":\"pause\"}'"
        # 或  "bluetoothctl disconnect"
        "extra_stop_command": "",
    },
    "kws": {
        # 唤醒词置信度加成（越高越难触发，越低越灵敏）
        "keywords_score": 0.8,
        # 唤醒词检测阈值（越低越灵敏，越高越难触发）
        "keywords_threshold": 0.08,
        # 唤醒词链路的 VAD 阈值（只影响 KWS，不影响对话监听）
        "vad_threshold": 0.02,
        # 唤醒词检测时的最小静默时长（ms），静默超过该时长则判定为说完
        "min_silence_duration": 480,
    },
    "vad": {
        # 连续对话语音检测阈值（0-1，越小越灵敏）。
        # 仅用于 HA 连续对话的 VAD；唤醒词链路使用 kws.vad_threshold。
        "threshold": 0.20,
        # 最小语音时长（ms）
        "min_speech_duration": 250,
        # 最小静默时长（ms）
        "min_silence_duration": 500,
    },
    "audio_input": {
        # 麦克风输入增益（仅作用于唤醒词 KWS 链路，不影响对话 VAD/ASR）。
        # 用于提升自定义唤醒词的远场识别距离；对话识别始终使用原始音频。
        "gain": 5.0,
        # 连续对话增益（仅作用于 HA 连续对话的 VAD + ASR，不影响唤醒词）。
        # 1.0 = 不放大；如果远场对话听不清，可逐步调到 1.5~2.0。
        # 不建议超过 2.5，否则噪声放大/削波会让 ASR 识别率下降。
        "conversation_gain": 1.5,
    },
    "asr": {
        # 本地离线识别模型（当前仅支持 paraformer，中文识别准确）
        "model": "paraformer",
        # 是否优先使用 INT8 量化模型（仅本地模型生效）
        "int8": True,
        # 解码线程数：调高可加快识别速度（对准确率无影响）
        "num_threads": 4,
        # 同音字/易错词替换表：把 ASR 经常听错的词替换成正确写法。
        # 例如 {"石头苏米桶": "石头污水桶", "科厅灯": "客厅灯"}
        "replacements": {},
        # 可选：显式指定 core/models/ 下的模型目录名（仅本地模型生效）
        # "model_dir": "",
    },
    "xiaoai": {
        "continuous_conversation_mode": True,
        "exit_command_keywords": ["停止", "退下", "退出", "下去吧", "没叫你"],
        "max_listening_retries": 2,  # 最多连续重试唤醒次数
        "exit_prompt": "再见，主人",
        "continuous_conversation_keywords": ["开启连续对话", "启动连续对话", "我想跟你聊天"]
    },
    # Home Assistant Assist 配置
    "ha": {
        # HA 实例地址（HAOS 默认 http://homeassistant.local:8123）
        "base_url": "http://homeassistant.local:8123",
        # Long-Lived Access Token：HA 设置 -> 安全 -> 长期访问令牌
        "token": "YOUR_LONG_LIVED_ACCESS_TOKEN",
        # 对话 agent 实体 ID，留空使用 HA 默认 agent。
        # extended_openai_conversation 的 agent_id 可在
        # HA「开发者工具 -> 动作 -> conversation.process」中查询。
        "agent_id": "conversation.extended_openai_conversation",
        # 可选：默认 conversation_id（通常留空，由 HA 生成）
        "conversation_id": "",
        # 可选：对话语言（如 "zh-CN"，留空使用 HA 配置）
        "language": "",
        # 输入模式：
        #   - "local_asr": 使用本地 VAD + SherpaASR（需要 ASR 模型）
        #   - "xiaoai_asr": 接管小爱原生 ASR 结果（无需 ASR 模型）
        "input_mode": "local_asr",
        # 退出连续对话的关键词（命中后立即结束，不发给 HA）
        "exit_keywords": ["退出", "停止", "再见", "没事了", "不打扰了", "退下吧", "先这样吧", "拜拜", "没叫你"],
        # 等待 HA 回复的超时时间（秒）
        "response_timeout": 60,
        # 回答播报结束后到开始监听的"残响吸收窗"（秒）：
        # 用于吸收音箱残响避免误触发；此窗口内用户开口说话仍会被完整
        # 捕获，不会丢失开头几个字。调大更稳（防误触发），调小响应更快。
        "listen_settle_seconds": 0.3,
        # TTS 音色："xiaoai" = 小爱原生 TTS；填写豆包音色 ID 则用豆包 TTS
        "tts_speaker": "zh_male_liangsangmengzai_uranus_bigtts",
        # 可按 agent_id 单独覆盖音色，优先级高于 tts_speaker
        # 示例：{"conversation.assistant": "zh_female_vv_uranus_bigtts"}
        "session_tts_speakers": {
            "conversation.hai_mian_bao_bao": "zh_male_liangsangmengzai_uranus_bigtts",
            "conversation.ni_hao_xiao_wei": "zh_female_vv_uranus_bigtts",
        },
        # 追加到每条发送给 HA 的消息末尾的提示词
        "rule_prompt": "注意：将结果处理成纯文字版，不要返回任何 markdown 格式，也不要包含任何代码块，并将字数控制在100字以内",
    },
    # TTS (Text-to-Speech) Configuration
    "tts": {
        "doubao": {
            # 豆包语音合成 API 配置（火山引擎新版控制台）
            # 认证方式：X-Api-Key 单头鉴权
            # API Key 获取：火山引擎控制台 -> API Key 管理
            # 文档: https://www.volcengine.com/docs/6561/1871062
            "api_key": "YOUR_DOUBAO_API_KEY",
            "default_speaker": "zh_male_liangsangmengzai_uranus_bigtts",  # 音色 https://www.volcengine.com/docs/6561/1257544?lang=zh
            "audio_format": "pcm",  # 推荐默认值：局域网稳定环境下首音更快、播放更顺
            "stream": True,  # 推荐默认值：边合成边播放，首音延迟更优
        }
    },
}
