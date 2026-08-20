"""Windows workaround: preload the venv's onnxruntime.dll.

Windows ships its own onnxruntime.dll in System32 (used by Edge/Office),
which wins the standard DLL search order over the one inside the venv.
Loading the correct DLL by absolute path first ensures the Python
onnxruntime package (and sherpa-onnx) bind to the expected version.

Import this module (or call ``preload_windows_onnxruntime()``) BEFORE
importing onnxruntime / sherpa_onnx on Windows.
"""

import importlib.util
import os
import sys


def preload_windows_onnxruntime() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        spec = importlib.util.find_spec("onnxruntime")
        if spec is None or not spec.submodule_search_locations:
            return
        pkg_dir = next(iter(spec.submodule_search_locations), None)
        if not pkg_dir:
            return
        dll_path = os.path.join(pkg_dir, "capi", "onnxruntime.dll")
        if os.path.isfile(dll_path):
            ctypes.WinDLL(dll_path)
    except Exception:
        # Never block startup because of the preload attempt.
        pass


preload_windows_onnxruntime()
