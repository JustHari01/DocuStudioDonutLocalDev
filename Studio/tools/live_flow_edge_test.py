"""One real Flow job, pinned to the verified Edge extension client."""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from docurandom.flow_agent import FlowAgentManager
from docurandom.storage import atomic_write_json

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--client-id', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    started = time.monotonic()
    prompt = (
        'A realistic documentary photograph of a yellow ceramic mug beside a small '
        'potted green plant on a wooden desk, soft morning window light, beautiful '
        'natural detail, landscape composition, no text, no logos.'
    )
    results = FlowAgentManager().generate_images(
        [prompt], args.output / 'Media', checkpoint_path=args.output / 'checkpoint.json',
        client_id=args.client_id, retry_failed=True,
        on_progress=lambda progress, message: print(json.dumps(
            {'progress': progress, 'message': message}, ensure_ascii=True), flush=True),
    )
    assert len(results) == 1
    with Image.open(results[0].path) as image:
        image.load()
        assert image.width >= 256 and image.height >= 256
        size = image.size
    report = {'client_id': args.client_id, 'browser': 'Microsoft Edge', 'prompt': prompt,
              'path': str(results[0].path), 'size': size,
              'elapsed_seconds': round(time.monotonic() - started, 2)}
    atomic_write_json(args.output / 'result.json', report)
    print(json.dumps(report, ensure_ascii=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
