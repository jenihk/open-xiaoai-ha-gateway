import argparse
import os
import signal
import sys
import time

# Windows: preload the venv's onnxruntime.dll before importing onnxruntime,
# otherwise Windows may bind to the System32 copy (1.17.x from Edge/Office).
import core.utils.onnx  # noqa: F401

# Fix: Add onnxruntime library path for sherpa_onnx on macOS/Linux
# This ensures sherpa_onnx can find libonnxruntime at runtime
if sys.platform in ("darwin", "linux"):
    try:
        import onnxruntime as ort
        ort_lib_dir = os.path.join(os.path.dirname(ort.__file__), "capi")
        if os.path.exists(ort_lib_dir):
            if sys.platform == "darwin":
                os.environ.setdefault("DYLD_LIBRARY_PATH", "")
                if ort_lib_dir not in os.environ["DYLD_LIBRARY_PATH"]:
                    os.environ["DYLD_LIBRARY_PATH"] = ort_lib_dir + ":" + os.environ["DYLD_LIBRARY_PATH"]
            else:  # linux
                os.environ.setdefault("LD_LIBRARY_PATH", "")
                if ort_lib_dir not in os.environ["LD_LIBRARY_PATH"]:
                    os.environ["LD_LIBRARY_PATH"] = ort_lib_dir + ":" + os.environ["LD_LIBRARY_PATH"]
    except ImportError:
        pass

from core.utils.config_loader import ensure_config_module_loaded

config_path = ensure_config_module_loaded()

from core.app import MainApp
from core.utils.logger import logger


main_app_instance = None

# 启动配置（从环境变量读取）
enable_api_server = False  # 是否开启 API Server
enable_ha = True  # 是否启用 Home Assistant 集成


def setup_config():
    """解析命令行参数和环境变量"""
    global enable_api_server, enable_ha

    parser = argparse.ArgumentParser(description="小爱音箱接入 Home Assistant")
    parser.parse_args()

    # 从环境变量读取配置
    enable_api_server = os.environ.get("API_SERVER_ENABLE", "").lower() in (
        "1", "true", "yes",
    )
    ha_env = os.environ.get("HA_ENABLE", "1")
    enable_ha = ha_env.strip().lower() in ("1", "true", "yes", "on")

    # 计算 AUDIO_INPUT_ENABLE 实际生效的值（默认 1/true）
    audio_input_enabled = os.environ.get(
        "AUDIO_INPUT_ENABLE", "1"
    ).strip().lower() in ("1", "true", "yes", "on")

    logger.info(
        f"[Main] ENV: HA_ENABLE={enable_ha}, "
        f"API_SERVER_ENABLE={enable_api_server}, "
        f"AUDIO_INPUT_ENABLE={1 if audio_input_enabled else 0}"
    )
    logger.info(f"[Main] Using config file: {config_path}")

    # 打印模块启用情况
    logger.info("[Main] 模块启用情况:")
    logger.info("小爱指令拦截器启用", module="Main")
    logger.info(
        f"Home Assistant: {'启用' if enable_ha else '禁用'}",
        module="Main",
    )
    logger.info(
        f"API Server: {'启用' if enable_api_server else '禁用'}",
        module="Main",
    )


def run_services(ha_mode: bool = True):
    """统一的服务启动入口。

    Args:
        ha_mode: 是否启用 Home Assistant 集成。
    """
    global main_app_instance

    # 统一使用 MainApp 管理所有服务
    main_app_instance = MainApp.instance(enable_ha=ha_mode)
    main_app_instance.run(enable_api_server=enable_api_server)

    # 主线程保持运行
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


def main():
    global enable_ha
    run_services(ha_mode=enable_ha)
    return 0


def setup_graceful_shutdown():
    def signal_handler(_sig, _frame):
        global main_app_instance

        # 关闭 MainApp（包含 API Server）
        if main_app_instance:
            main_app_instance.shutdown()

        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)


if __name__ == "__main__":
    setup_config()
    setup_graceful_shutdown()
    sys.exit(main())
