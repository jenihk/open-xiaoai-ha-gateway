class EventType:
    """事件类型"""
    SCHEDULE_EVENT = "schedule_event"

class AudioConfig:
    """音频配置"""
    FORMAT = 8
    SAMPLE_RATE = 16000
    CHANNELS = 1
    FRAME_DURATION = 60  # ms
    FRAME_SIZE = int(SAMPLE_RATE * (FRAME_DURATION / 1000))
