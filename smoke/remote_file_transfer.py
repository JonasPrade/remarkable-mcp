"""Testclient für remarkable-mcp (läuft im Container, spricht 127.0.0.1:8000 an).

Aufruf auf dem Server:
  docker exec -i remarkable-mcp-test python - <befehl> [optionen] < rmtest.py

Befehle:
  tools                         neue Werkzeuge vorhanden?
  status                        remarkable_status
  b64 NAME                      Test-PDF per Base64 nach /Tagesplan/Test (Tag mcp-test)
  link NAME                     Test-PDF per Einmal-Link nach /Tagesplan/Test
  replace NAME [--force]        Test-PDF ersetzen (replace=True)
  tag NAME [--add T] [--remove T]
  find TAG [--path P]           remarkable_browse(tags=[TAG])
  export PFAD                   Export + Download über download_url
"""

import argparse
import base64
import json
import sys
import urllib.request
from datetime import datetime

BASE = "http://127.0.0.1:8000"
FOLDER = "/Tagesplan/Test"
_session = {}


def _post(payload):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if "sid" in _session:
        headers["Mcp-Session-Id"] = _session["sid"]
    req = urllib.request.Request(
        BASE + "/mcp", data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        sid = resp.headers.get("mcp-session-id")
        if sid:
            _session["sid"] = sid
        body = resp.read().decode()
        if "text/event-stream" in resp.headers.get("content-type", ""):
            data = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
            body = data[-1] if data else ""
        return json.loads(body) if body.strip() else None


def connect():
    _post(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "rmtest", "version": "0"},
            },
        }
    )
    _post({"jsonrpc": "2.0", "method": "notifications/initialized"})


def call(tool, **args):
    reply = _post(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        }
    )
    if "error" in reply:
        raise SystemExit(f"JSON-RPC-Fehler: {reply['error']}")
    text = reply["result"]["content"][0]["text"]
    try:
        return json.loads(text)
    except ValueError:
        return {"_raw": text}


def show(label, data):
    ok = "FEHLER" if "_error" in data else "OK"
    print(f"--- {label}: {ok}")
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def test_pdf(name):
    import pymupdf as fitz

    doc = fitz.open()
    page = doc.new_page(width=447, height=596)  # 157,8 x 210,4 mm
    page.insert_text((40, 60), name, fontsize=18)
    page.insert_text((40, 90), f"Testupload {datetime.now():%Y-%m-%d %H:%M:%S}", fontsize=11)
    page.insert_text((40, 120), "Hier etwas hinschreiben, dann 'replace' testen.", fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


def main():
    p = argparse.ArgumentParser()
    p.add_argument("cmd")
    p.add_argument("arg", nargs="?")
    p.add_argument("--force", action="store_true")
    p.add_argument("--add", action="append")
    p.add_argument("--remove", action="append")
    p.add_argument("--path", default="/")
    a = p.parse_args()

    connect()
    if a.cmd == "tools":
        reply = _post({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        names = sorted(t["name"] for t in reply["result"]["tools"])
        print("\n".join(names))
        for want in ("remarkable_upload_base64", "remarkable_upload_link", "remarkable_tag"):
            print(f"{want}: {'vorhanden' if want in names else 'FEHLT'}")
    elif a.cmd == "status":
        show("status", call("remarkable_status"))
    elif a.cmd in ("b64", "replace"):
        show(
            a.cmd,
            call(
                "remarkable_upload_base64",
                content_base64=base64.b64encode(test_pdf(a.arg)).decode(),
                document_name=a.arg,
                parent_folder=FOLDER,
                tags=["mcp-test"],
                create_folders=True,
                replace=a.cmd == "replace",
                force_replace=a.force,
            ),
        )
    elif a.cmd == "link":
        link = call(
            "remarkable_upload_link",
            document_name=a.arg,
            parent_folder=FOLDER,
            tags=["mcp-test"],
            create_folders=True,
        )
        show("link anfordern", link)
        if "upload_url" in link:
            req = urllib.request.Request(link["upload_url"], data=test_pdf(a.arg), method="PUT")
            try:
                with urllib.request.urlopen(req, timeout=300) as resp:
                    show(f"PUT {resp.status}", json.loads(resp.read()))
            except urllib.error.HTTPError as e:
                show(f"PUT {e.code}", json.loads(e.read()))
            req = urllib.request.Request(link["upload_url"], data=b"%PDF-", method="PUT")
            try:
                urllib.request.urlopen(req, timeout=60)
                print("--- zweiter PUT: FEHLER, Link war noch gültig")
            except urllib.error.HTTPError as e:
                print(f"--- zweiter PUT: OK, abgelehnt mit {e.code}")
    elif a.cmd == "tag":
        show("tag", call("remarkable_tag", document=a.arg, add=a.add, remove=a.remove))
    elif a.cmd == "find":
        show("find", call("remarkable_browse", path=a.path, tags=[a.arg]))
    elif a.cmd == "export":
        data = call("remarkable_export", document=a.arg)
        show("export", data)
        if "download_url" in data:
            with urllib.request.urlopen(data["download_url"], timeout=120) as resp:
                body = resp.read()
            print(f"--- download: {len(body)} Bytes, beginnt mit {body[:5]!r}")
    else:
        sys.exit(__doc__)


main()
