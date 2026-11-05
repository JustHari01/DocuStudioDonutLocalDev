"""Optional footage: verified provenance, bounded retrieval, durable reuse ledger."""
from __future__ import annotations
from contextlib import contextmanager

from dataclasses import asdict, replace
import hashlib
import io
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import time
import urllib.parse
import urllib.request

from PIL import Image

from .channel_learning import digest, now_iso
from .models import MediaInfo, TransitionPlan
from .scanner import probe_media_file
from .storage import atomic_write_json

_LANE = threading.Lock()
_HOSTS = {"upload.wikimedia.org", "images.pexels.com", "videos.pexels.com", "player.vimeo.com"}


def _remote(url: str) -> None:
    p = urllib.parse.urlsplit(url)
    if p.scheme != "https" or p.hostname not in _HOSTS or p.username or p.password:
        raise ValueError("Unsupported media download host")


def _json(url, *, headers=None):
    request = urllib.request.Request(url, headers={"User-Agent": "DocuRandomStudio/2.4.315 (local video research)", **(headers or {})})
    with urllib.request.urlopen(request, timeout=12) as response:
        return json.loads(response.read(2 * 1024 * 1024))


def search_commons(query: str) -> list[dict]:
    params = {"action": "query", "format": "json", "generator": "search", "gsrnamespace": 6,
              "gsrsearch": query + " filetype:video", "gsrlimit": 6, "prop": "imageinfo",
              "iiprop": "url|extmetadata|size|mime"}
    response = _json("https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode(params))
    results = []
    for page in response.get("query", {}).get("pages", {}).values():
        for item in page.get("imageinfo", []):
            meta = item.get("extmetadata", {})
            field = lambda name: str(meta.get(name, {}).get("value", ""))
            license_name = field("LicenseShortName")
            # More complex licenses require a separately implemented compliance path.
            if license_name not in {"CC0", "Public domain", "CC BY 4.0", "CC BY 3.0", "CC BY 2.0"}:
                continue
            if license_name.startswith("CC BY") and not (field("Artist") and field("LicenseUrl")):
                continue
            if not str(item.get("mime", "")).startswith("video/"):
                continue
            results.append({"provider": "commons", "asset_id": str(page["pageid"]),
                            "title": page["title"], "source_url": item["descriptionurl"],
                            "download_url": item["url"], "license": license_name,
                            "license_url": field("LicenseUrl"), "author": field("Artist"),
                            "credit": field("Credit"), "checked_at": now_iso()})
    return results


def search_pexels(query: str) -> list[dict]:
    key = os.environ.get("DOCURANDOM_PEXELS_API_KEY", "")
    if not key:
        return []
    response = _json("https://api.pexels.com/v1/videos/search?" + urllib.parse.urlencode({"query": query, "per_page": 6}),
                     headers={"Authorization": key})
    results = []
    for video in response.get("videos", []):
        files = [f for f in video.get("video_files", []) if f.get("file_type") == "video/mp4"
                 and max(f.get("width", 0), f.get("height", 0)) <= 1920]
        if not files: continue
        chosen = max(files, key=lambda f: f.get("width", 0) * f.get("height", 0))
        results.append({"provider": "pexels", "asset_id": str(video["id"]), "source_url": video["url"],
                        "download_url": chosen["link"], "title": video["url"].split("/")[-2],
                        "license": "Pexels", "license_url": "https://www.pexels.com/legal-pages/license/",
                        "author": video.get("user", {}).get("name", ""), "credit": video["url"], "checked_at": now_iso()})
    return results


def download(candidate: dict, cache: Path, *, cancel=None, deadline=None) -> Path:
    url = candidate["download_url"]; _remote(url)
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / (digest([candidate["provider"], candidate["asset_id"]]) + Path(urllib.parse.urlsplit(url).path).suffix)
    if target.is_file(): return target
    temp = target.with_suffix(target.suffix + ".partial")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "DocuRandomStudio/2.4.315"})
        with urllib.request.urlopen(req, timeout=10) as response, temp.open("wb") as stream:
            _remote(response.url)
            count = 0
            while chunk := response.read(256 * 1024):
                if (cancel and cancel.is_set()) or (deadline and time.monotonic() >= deadline):
                    raise InterruptedError("Footage retrieval cancelled")
                count += len(chunk)
                if count > 150 * 1024 * 1024: raise ValueError("Footage exceeds per-file budget")
                stream.write(chunk)
        temp.replace(target)
    finally:
        temp.unlink(missing_ok=True)
    return target


def dhash(image: Image.Image) -> str:
    pixels = list(image.convert("L").resize((9, 8)).getdata())
    bits = sum((pixels[y * 9 + x] > pixels[y * 9 + x + 1]) << (y * 8 + x) for y in range(8) for x in range(8))
    return f"{bits:016x}"


def frame_fingerprints(path: Path, ffmpeg: Path, start: float, end: float) -> list[str]:
    hashes = []
    for ratio in (0.1, 0.5, 0.9):
        command = [str(ffmpeg), "-v", "error", "-ss", str(start + (end - start) * ratio),
                   "-i", str(path), "-frames:v", "1", "-vf", "scale=320:-2", "-f", "image2pipe", "-vcodec", "png", "pipe:1"]
        raw = subprocess.run(command, capture_output=True, timeout=12, check=True).stdout
        with Image.open(io.BytesIO(raw)) as image:
            hashes.append(dhash(image))
    return hashes


class FootageLedger:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True); self.path = root / "footage.sqlite3"
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS uses(video_id TEXT,channel_id TEXT,sha256 TEXT,provider_id TEXT,start REAL,end REAL,hashes TEXT,provenance TEXT,at TEXT,PRIMARY KEY(video_id,sha256,start,end))")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10); db.row_factory = sqlite3.Row
        try:
            with db: yield db
        finally:
            db.close()

    def recent(self, channel_id: str, video_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM uses WHERE channel_id=? AND video_id!=? "
                              "AND video_id IN (SELECT video_id FROM uses WHERE channel_id=? GROUP BY video_id ORDER BY MAX(at) DESC LIMIT 10)",
                              (channel_id, video_id, channel_id))
            return [dict(row) for row in rows]

    def duplicate(self, *, channel_id, video_id, sha256, provider_id, start, end, hashes) -> bool:
        for row in self.recent(channel_id, video_id):
            overlap = min(end, row["end"]) > max(start, row["start"])
            if (row["sha256"] == sha256 or row["provider_id"] == provider_id) and overlap:
                return True
            old = json.loads(row["hashes"])
            if len(old) == len(hashes) and hashes and sum((int(a,16)^int(b,16)).bit_count() <= 10 for a,b in zip(old,hashes)) >= 2:
                return True
        return False

    def record(self, *, video_id, channel_id, path, source, start, end, hashes):
        source = {k:v for k,v in source.items() if k != "download_url"}
        if not all(source.get(k) for k in ("source_url", "license", "checked_at")):
            raise ValueError("Missing footage provenance")
        with path.open("rb") as stream: sha = hashlib.file_digest(stream,"sha256").hexdigest()
        provider = source["provider"] + ":" + str(source["asset_id"])
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO uses VALUES(?,?,?,?,?,?,?,?,?)",
                       (video_id, channel_id, sha, provider, start, end, json.dumps(hashes), json.dumps(source,ensure_ascii=False),now_iso()))
        return sha


def splice_footage(plan, media: list[MediaInfo], placements: list[dict], settings):
    """Place on exact narration time; preserve total frames, never randomize B-roll."""
    fps = settings.fps; total = round(plan.expected_duration * fps)
    base = []; cursor = 0
    for i, scene in enumerate(plan.scenes):
        overlap = plan.transitions[i-1].duration_frames if i else 0
        length = scene.duration_frames - overlap
        base.append((cursor, cursor + length, scene)); cursor += length
    if cursor != total: raise ValueError("Base timeline does not match narration")
    inserts = sorted(placements, key=lambda p:p["start"])
    used = 0; previous = -1
    for p in inserts:
        start, end = round(p["start"]*fps),round(p["end"]*fps)
        if not (0 <= start < end <= total) or start < previous:
            raise ValueError("Footage placement overlaps or exceeds narration")
        if not 3*fps <= end-start <= 8*fps: raise ValueError("Footage duration outside editorial bounds")
        if p["media"].duration_seconds * fps < end-start+1: raise ValueError("Source too short; do not loop")
        previous=end;used += end-start
    if used > total * 0.25: raise ValueError("Optional footage exceeds 25 percent")
    points = sorted({0,total,*[b for a,b,s in base],*[round(p[k]*fps) for p in inserts for k in ("start","end")]})
    scenes = []; ordered=[]; media_map={str(m.path):m for m in media}
    for a,b in zip(points,points[1:]):
        original=next(s for x,y,s in base if x<=a<y)
        insertion=next((p for p in inserts if round(p['start']*fps)<=a<round(p['end']*fps)),None)
        extra=1 if scenes else 0
        if insertion:
            item=insertion['media']; ordered.append(item)
            scene=replace(original,source_path=str(item.path),relative_path=item.relative_path,
                          media_type='video',duration_frames=b-a+extra,duration_seconds=(b-a+extra)/fps,
                          source_start_seconds=insertion.get('source_start',0)+(a-round(insertion['start']*fps))/fps,
                          source_duration_seconds=item.duration_seconds,playback_rate=1.0,loop_source=False,
                          source_width=item.width,source_height=item.height,
                          effect_name='clean',brightness=0,contrast=1,saturation=1,gamma=1,
                          red_gain=1,green_gain=1,blue_gain=1,hue_shift=0,sharpen=0,
                          chroma_shift=0,lens_k1=0,lens_k2=0,effect_mix=0,curve_preset='none',color_style='Tự nhiên')
        else:
            ordered.append(media_map[original.source_path])
            scene=replace(original,duration_frames=b-a+extra,duration_seconds=(b-a+extra)/fps)
        scenes.append(scene)
    transitions=[TransitionPlan('fade',1,1/fps) for _ in scenes[1:]]
    result=replace(plan,scenes=scenes,transitions=transitions)
    result.manifest_signature=digest({'base':plan.manifest_signature,'scenes':[asdict(s) for s in scenes]})
    return result,ordered
