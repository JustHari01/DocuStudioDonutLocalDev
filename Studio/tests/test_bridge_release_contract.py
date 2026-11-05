import json
import re
import unittest
from pathlib import Path
from docurandom import content_ai

class BridgeReleaseContractTests(unittest.TestCase):
    def test_studio_accepts_version_of_bundled_bridge_source(self):
        root = Path(__file__).resolve().parents[1]
        package = json.loads((root / "chat_bridge/package.json").read_text(encoding="utf-8"))
        main = (root / "chat_bridge/src/main.cjs").read_text(encoding="utf-8")
        release = re.search(r'const VERSION = "([0-9.]+)";', main)
        self.assertIsNotNone(release)
        self.assertEqual(package["version"], release.group(1))
        self.assertEqual(content_ai.CHATGPT_WEB_BRIDGE_VERSION, package["version"])
