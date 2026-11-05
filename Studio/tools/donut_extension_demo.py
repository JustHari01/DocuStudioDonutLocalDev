from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from docurandom.browser_extension_bridge import BrowserExtensionBridgeError, get_browser_extension_bridge


DEFAULT_TEST_PROFILE = "4df23bcd-b166-4865-9b9e-d6805e11370f"
MIN_EXTENSION_VERSION = (1, 1, 0)


def _version_tuple(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in value.split("."))
    except (TypeError, ValueError):
        return ()


def main() -> int:
    parser = argparse.ArgumentParser(description="Demo DocuRandom Donut Browser Bridge")
    parser.add_argument("--profile-id", default=DEFAULT_TEST_PROFILE)
    parser.add_argument("--wait", type=float, default=180.0)
    args = parser.parse_args()

    bridge = get_browser_extension_bridge()
    bridge.begin_pairing(args.profile_id, ttl=args.wait)
    print("PAIRING_READY", flush=True)
    print("Mở popup extension trong profile Test và bấm 'Ghép profile này'.", flush=True)
    if not bridge.wait_for_profile(args.profile_id, timeout=args.wait):
        print("PAIRING_TIMEOUT", flush=True)
        return 2

    deadline = time.monotonic() + args.wait
    while time.monotonic() < deadline:
        status = bridge.status(args.profile_id)
        if status.connected and _version_tuple(status.extension_version) >= MIN_EXTENSION_VERSION:
            break
        print(
            f"WAIT_EXTENSION_UPDATE current={status.extension_version or 'unknown'} required=1.1.0",
            flush=True,
        )
        time.sleep(1)
    else:
        print("EXTENSION_UPDATE_TIMEOUT", flush=True)
        return 4

    print("EXTENSION_CONNECTED", flush=True)
    bridge.command(
        args.profile_id,
        "navigate",
        {"url": "https://example.com/?docurandom-bridge-demo=1", "new_tab": True},
        timeout=20,
    )
    try:
        expression = """
        (() => {
          document.title = 'DocuRandom Bridge Demo';
          const old = document.getElementById('docurandom-bridge-demo');
          if (old) old.remove();
          const banner = document.createElement('div');
          banner.id = 'docurandom-bridge-demo';
          banner.textContent = 'DOCURANDOM BROWSER BRIDGE HOẠT ĐỘNG';
          Object.assign(banner.style, {
            position: 'fixed', inset: '24px 24px auto 24px', zIndex: '2147483647',
            padding: '20px', background: '#111', color: '#fff', border: '3px solid #55c878',
            font: '700 24px Segoe UI, sans-serif', textAlign: 'center'
          });
          document.documentElement.appendChild(banner);
          return {title: document.title, url: location.href};
        })()
        """
        result = bridge.command(
            args.profile_id,
            "script_eval",
            {
                "expression": expression,
            },
            timeout=15,
        )
        print("DEMO_OK", json.dumps(result, ensure_ascii=False), flush=True)
        time.sleep(15)
    except BrowserExtensionBridgeError as exc:
        print(f"DEMO_ERROR {exc}", flush=True)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
