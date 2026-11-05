"""Read-only inventory, byte preservation checks, and isolated Bridge smoke test."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def snapshot(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    files = sorted(path for path in root.rglob("*") if path.is_file())
    if args.skip_browser_data:
        files = [p for p in files if not p.relative_to(root).as_posix().startswith(f"{args.skip_browser_data}/profile/")]
    def digest(path: Path) -> tuple[str, dict[str, object]]:
        with path.open("rb") as stream:
            value = hashlib.file_digest(stream, "sha256").hexdigest()
        return path.relative_to(root).as_posix(), {"size": path.stat().st_size, "sha256": value}
    entries = {}
    if args.merge_from:
        previous = json.loads(args.merge_from.read_text(encoding="utf-8"))
        if previous["root"] != str(root):
            raise RuntimeError("Snapshot merge requires the same original root")
        entries = previous["files"]
        files = [p for p in files if p.relative_to(root).as_posix() not in entries]
    with ThreadPoolExecutor(max_workers=4) as pool:
        entries.update(pool.map(digest, files))
    write_json(args.output, {"root": str(root), "excluded_live_browser_data": args.skip_browser_data, "files": entries})
    print(json.dumps({"files": len(entries), "bytes": sum(int(v["size"]) for v in entries.values()), "excluded_live_browser_data": args.skip_browser_data}), flush=True)


def compare(args: argparse.Namespace) -> None:
    before = json.loads(args.before.read_text(encoding="utf-8"))["files"]
    after = json.loads(args.after.read_text(encoding="utf-8"))["files"]
    changed = sorted(k for k in before.keys() & after.keys() if before[k] != after[k])
    result = {"unchanged": before == after, "files": len(before), "changed": changed, "added": sorted(after.keys()-before.keys()), "removed": sorted(before.keys()-after.keys())}
    write_json(args.output, result)
    print(json.dumps({k: (len(v) if isinstance(v,list) else v) for k,v in result.items()}), flush=True)
    if not result["unchanged"]:
        raise SystemExit(1)


def probe(args: argparse.Namespace) -> None:
    os.environ["DOCURANDOM_HOME"] = str(args.studio_home.resolve())
    os.environ["DOCURANDOM_DONUT_DATA_ROOT"] = str(args.root.resolve())
    from docurandom.donut_manager import DonutManager
    from docurandom.donut_mcp import DonutMcpClient, load_donut_mcp_endpoint
    endpoint = load_donut_mcp_endpoint(args.saved_endpoint) if args.saved_endpoint else DonutManager.discover_endpoint()
    if endpoint is None:
        raise RuntimeError("The existing encrypted MCP endpoint could not be read")
    client = DonutMcpClient(endpoint, timeout=30, shared_session=False)
    report: dict[str, object] = {"connection_mode":"existing-encrypted-endpoint" if args.saved_endpoint else "discovery"}
    try:
        info = client.connect()
        client.require_docurandom_local()
        profiles = client.list_profiles()
        report.update(server=info.get("serverInfo"), tools=len(client.list_tool_names()), profile_count=len(profiles), running_count=sum(p.running for p in profiles), profile_ids=sorted(p.profile_id for p in profiles), group_count=len(client.list_groups()), extensions=client.call_tool_json("list_extensions"), extension_groups=client.call_tool_json("list_extension_groups"))
        # Extension paths and IDs are public local routing metadata. No tokens are emitted.
        if args.exercise_bridge:
            if "validation" not in str(args.root.resolve()).casefold():
                raise RuntimeError("Bridge exercise is restricted to the validation copy")
            from docurandom.browser_extension_bridge import get_browser_extension_bridge
            from docurandom.youtube_upload import _ensure_youtube_browser_bridge
            class Fixture(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(b'<!doctype html><title>DocuRandom Integration Fixture</title><input id="text"><input id="file" type="file"><button id="click" onclick="this.textContent=\'clicked\'">click</button>')
                def log_message(self, *_args):
                    pass
            server = ThreadingHTTPServer(("127.0.0.1",0),Fixture)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            groups = report["extension_groups"]
            bridge_group = next(g for g in groups if g.get("name") == "DocuRandom Automation")
            test_name = f"Integration verification (copy only) {time.time_ns()}"
            client.call_tool("create_profile", {"name":test_name,"browser":"wayfern","version":"152.0.7977.64"})
            test_profile = next(p for p in client.list_profiles() if p.name == test_name)
            report["test_profile_id"] = test_profile.profile_id
            client.call_tool_json("assign_extension_group_to_profile", {"profile_id":test_profile.profile_id,"extension_group_id":bridge_group["id"]})
            try:
                client.run_profile(test_profile.profile_id, f"http://127.0.0.1:{server.server_port}/")
                report["browser_launch"] = "passed"
                debug = client.get_debug_connection(test_profile.profile_id)
                assert str(debug.get("targets_url", "")).startswith("http://127.0.0.1:")
                report["debug_connection"] = "passed"
                bridge = _ensure_youtube_browser_bridge(client,test_profile.profile_id,timeout=25)
                report["bridge_version"] = bridge.status(test_profile.profile_id).extension_version
                tabs = bridge.command(test_profile.profile_id,"tabs_list")
                tab = next(t for t in tabs if str(t.get("url", "")).startswith(f"http://127.0.0.1:{server.server_port}/"))
                report["bridge_page_state"] = bridge.command(test_profile.profile_id,"page_state",{"tab_id":tab["id"]})
                report["bridge_eval_isolated"] = bridge.command(test_profile.profile_id,"script_eval",{"tab_id":tab["id"],"expression":"1+1"})
                report["bridge_eval_main"] = bridge.command(test_profile.profile_id,"script_eval",{"tab_id":tab["id"],"expression":"1+1","world":"MAIN"})
                params={"tab_id":tab["id"],"expression":"(()=>{document.querySelector('#text').value='bridge-ok';document.querySelector('#click').click();return {title:document.title,text:document.querySelector('#text').value,clicked:document.querySelector('#click').textContent};})()"}
                params["world"] = "MAIN"
                result = bridge.command(test_profile.profile_id,"script_eval",params)
                report["bridge_dom_result"]=result
                if not isinstance(result,dict) or result.get("text") != "bridge-ok" or result.get("clicked") != "clicked":
                    raise AssertionError("Real extension DOM operation failed")
                report["bridge_dom"]="passed"
                bridge.command(test_profile.profile_id,"script_eval",{"tab_id":tab["id"],"expression":"document.querySelector('#click').textContent='before-fixed'","world":"MAIN"})
                clicked = bridge.command(test_profile.profile_id,"youtube_click",{"tab_id":tab["id"],"selector":"#click"})
                report["bridge_fixed_dom_result"]=clicked
                assert clicked.get("pageResult", {}).get("clicked") is True
                verified_click=bridge.command(test_profile.profile_id,"script_eval",{"tab_id":tab["id"],"expression":"document.querySelector('#click').textContent","world":"MAIN"})
                assert verified_click == "clicked"
                report["bridge_fixed_dom_command"]="passed"
                fixture_file=args.studio_home / "bridge-fixture.txt"
                fixture_file.write_text("docurandom-file-transfer-ok",encoding="utf-8")
                shared_url=bridge.share_file(fixture_file)
                attached=bridge.command(test_profile.profile_id,"set_file_url",{"tab_id":tab["id"],"url":shared_url,"name":fixture_file.name,"mime":"text/plain"})
                assert attached["size"]==fixture_file.stat().st_size
                content=bridge.command(test_profile.profile_id,"script_eval",{"tab_id":tab["id"],"expression":"(async()=>await document.querySelector('#file').files[0].text())()","world":"MAIN"})
                assert content==fixture_file.read_text(encoding="utf-8")
                report["bridge_file_transfer"]="passed"
            except Exception as exc:
                report["bridge_error"] = str(exc)[:2000]
                report["bridge_failed_stage"] = "file-or-fixed-dom" if report.get("bridge_dom") == "passed" else "dom"
            finally:
                client.call_tool("kill_profile", {"profile_id":test_profile.profile_id})
                get_browser_extension_bridge().stop()
                server.shutdown()
    finally:
        client.close()
        write_json(args.output,report)
    print(json.dumps({k:v for k,v in report.items() if k not in {"extensions","extension_groups","profile_ids"}},ensure_ascii=False),flush=True)
    if args.exercise_bridge and report.get("bridge_file_transfer") != "passed":
        raise SystemExit(1)


def cookies(args: argparse.Namespace) -> None:
    entries = []
    for path in sorted(args.root.rglob("Cookies")):
        if not path.is_file():
            continue
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            db.execute("PRAGMA query_only=ON")
            integrity = db.execute("PRAGMA quick_check").fetchall()
            if integrity != [("ok",)]:
                raise RuntimeError(f"Cookie database integrity failed: {path}")
            count = db.execute("SELECT count(*) FROM cookies").fetchone()[0]
            checked = 0
            value_hashes = []
            if args.decrypt:
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM
                key_path = next((parent / "os_crypt_key" for parent in path.parents if (parent / "os_crypt_key").is_file()), None)
                rows = db.execute("SELECT host_key,path,name,value,encrypted_value FROM cookies")
                for host, cookie_path, name, value, encrypted in rows:
                    if encrypted:
                        if key_path is None or encrypted[:3] != b"v10":
                            raise RuntimeError(f"Cookie encryption cannot be verified for {path.relative_to(args.root)}")
                        key = key_path.read_bytes()
                        assert len(key) == 32
                        plaintext = AESGCM(key).decrypt(encrypted[3:15],encrypted[15:],None)
                        host_hash = hashlib.sha256(host.encode()).digest()
                        if plaintext[:32] == host_hash:
                            plaintext = plaintext[32:]
                        value = plaintext.decode("utf-8")
                        checked += 1
                    encoded = json.dumps([host,cookie_path,name,value],ensure_ascii=False,separators=(",",":")).encode()
                    value_hashes.append(hashlib.sha256(encoded).hexdigest())
        entry={"path":path.relative_to(args.root).as_posix(),"cookies":count,"integrity":"ok","encrypted_cookies_verified":checked}
        if args.decrypt:
            entry["cookie_value_digest"]=hashlib.sha256("\n".join(sorted(value_hashes)).encode()).hexdigest()
        entries.append(entry)
    write_json(args.output, {"databases":entries,"total_cookies":sum(e["cookies"] for e in entries)})
    print(json.dumps({"databases":len(entries),"total_cookies":sum(e["cookies"] for e in entries),"integrity":"ok","encrypted_cookies_verified":sum(e["encrypted_cookies_verified"] for e in entries)}),flush=True)


def main() -> None:
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest="command",required=True)
    snap=sub.add_parser("snapshot")
    snap.add_argument("--root",type=Path,required=True)
    snap.add_argument("--skip-browser-data",default="")
    snap.add_argument("--merge-from",type=Path)
    snap.add_argument("--output",type=Path,required=True)
    diff=sub.add_parser("compare")
    diff.add_argument("--before",type=Path,required=True)
    diff.add_argument("--after",type=Path,required=True)
    diff.add_argument("--output",type=Path,required=True)
    live=sub.add_parser("probe")
    live.add_argument("--root",type=Path,required=True)
    live.add_argument("--studio-home",type=Path,required=True)
    live.add_argument("--exercise-bridge",action="store_true")
    live.add_argument("--saved-endpoint",type=Path)
    live.add_argument("--output",type=Path,required=True)
    cookie_audit=sub.add_parser("cookies")
    cookie_audit.add_argument("--root",type=Path,required=True)
    cookie_audit.add_argument("--output",type=Path,required=True)
    cookie_audit.add_argument("--decrypt",action="store_true")
    args=parser.parse_args()
    {"snapshot":snapshot,"compare":compare,"probe":probe,"cookies":cookies}[args.command](args)


if __name__ == "__main__":
    main()
