import importlib
import importlib.util
import os
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


class WakeupKeywordStartupTest(unittest.TestCase):
    def test_keyword_generation_enabled_for_ha(self):
        spec = importlib.util.spec_from_file_location(
            "kws_keywords_for_test",
            ROOT / "core/services/audio/kws/keywords.py",
        )
        keywords = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(keywords)

        with mock.patch.dict(
            os.environ,
            {"HA_ENABLE": "1"},
            clear=False,
        ):
            should_run, reason = keywords.should_generate_keywords()

        self.assertTrue(should_run)
        self.assertEqual(reason, "")

    def test_keyword_generation_disabled_when_ha_off(self):
        spec = importlib.util.spec_from_file_location(
            "kws_keywords_for_test",
            ROOT / "core/services/audio/kws/keywords.py",
        )
        keywords = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(keywords)

        with mock.patch.dict(
            os.environ,
            {"HA_ENABLE": "0"},
            clear=False,
        ):
            should_run, reason = keywords.should_generate_keywords()

        self.assertFalse(should_run)
        self.assertIn("HA_ENABLE", reason)

    def test_startup_entrypoints_prepare_keywords_for_ha(self):
        start_sh = (ROOT / "scripts/start.sh").read_text(encoding="utf8")
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf8")

        self.assertIn("HA_ENABLE_VALUE", start_sh)
        self.assertIn('[[ "$HA_ENABLE_VALUE" =~ ^(1|true|yes|on)$ ]]', start_sh)
        self.assertIn('${HA_ENABLE:-1}', dockerfile)
        self.assertIn("HA_VAL", dockerfile)
        self.assertIn("python core/services/audio/kws/keywords.py", dockerfile)


if __name__ == "__main__":
    unittest.main()
