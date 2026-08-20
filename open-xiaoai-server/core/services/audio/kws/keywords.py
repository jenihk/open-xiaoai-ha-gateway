import re
from pathlib import Path


def init_project_context():
    """动态导入父模块"""
    import os
    import sys

    project_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "../../../..")
    )
    if project_root not in sys.path:
        sys.path.insert(0, project_root)


init_project_context()

# Windows: preload the venv's onnxruntime.dll before sherpa_onnx is used.
import core.utils.onnx  # noqa: F401

from core.utils.config import ConfigManager
from core.utils.file import get_model_file_path
from core.utils.logger import logger


def should_generate_keywords():
    """Return whether keyword generation should run."""
    import os

    ha_enabled = os.environ.get("HA_ENABLE", "1").strip().lower() in (
        "1", "true", "yes", "on",
    )
    if not ha_enabled:
        return False, "HA_ENABLE is disabled"

    return True, ""


def get_args():
    config = ConfigManager.instance()
    tokens_type = "cjkchar+bpe"
    tokens = get_model_file_path("tokens.txt")
    bpe_model = get_model_file_path("bpe.model")
    output = get_model_file_path("keywords.txt")
    keywords = config.get_app_config("wakeup.keywords", [])
    texts = [f"{keyword.upper()}" for keyword in keywords]
    return locals()


def main():
    should_run, reason = should_generate_keywords()
    if not should_run:
        logger.debug(f"Keyword generation skipped: {reason}", module="KWS")
        return 0

    required_files = [
        get_model_file_path("tokens.txt"),
        get_model_file_path("bpe.model"),
    ]
    missing_files = [path for path in required_files if not Path(path).is_file()]
    if missing_files:
        logger.debug(
            "Keyword generation failed: missing model files: "
            f"{', '.join(missing_files)}",
            module="KWS",
        )
        return 1

    from sherpa_onnx import text2token

    args = get_args()
    encoded_texts = text2token(
        args["texts"],
        tokens=args["tokens"],
        tokens_type=args["tokens_type"],
        bpe_model=args["bpe_model"],
    )
    with open(args["output"], "w", encoding="utf8") as f:
        for _, txt in enumerate(encoded_texts):
            line = "".join(txt)
            if re.match(r"^[▁A-Z\s]+$", line):
                f.write(" ".join(txt) + "\n")
            else:
                f.write(" ".join(txt) + f" @{line}" + "\n")
    logger.debug(f"Keyword file generated: {args['output']}", module="KWS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
