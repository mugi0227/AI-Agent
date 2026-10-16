"""python -m work_assistant --help"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path

from .common import AssistantError, atomic_json, now_utc, safe_code, stamp
from .config import Settings, default_home, initialize, load
from .runtime import Runtime


def synthetic_envelope(kind="transcript", source_id="demo:meeting") -> dict:
    now = now_utc()
    return {"schema_version": 1, "event_id": str(uuid.uuid4()), "kind": kind, "source_id": source_id,
            "title": "架空データ: 塗工条件の検討", "content": "田中: 塗工幅を測定しました。\n佐藤: 次回までに測定条件を確認します。",
            "source_url": "https://example.invalid/demo", "source_modified_at": stamp(now),
            "observed_at": stamp(now), "expires_at": stamp(now + timedelta(hours=24)),
            "metadata": {"coverage": "synthetic_example"}}


def main(argv=None) -> int:
    # JSON output must stay UTF-8 even when Windows redirects it through a legacy codepage.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Windows向けローカル業務秘書。既定では推論・収集とも無効です。")
    parser.add_argument("--home", type=Path, default=default_home(), help="同期フォルダ外の私有データ領域")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "gui", "run", "scan", "demo"):
        sub.add_parser(name)
    doctor = sub.add_parser("doctor"); doctor.add_argument("--live", action="store_true")
    query = sub.add_parser("search"); query.add_argument("query")
    ask = sub.add_parser("ask"); ask.add_argument("text")
    ingest = sub.add_parser("import"); ingest.add_argument("path", type=Path); ingest.add_argument("--envelope", action="store_true")
    project = sub.add_parser("project"); project.add_argument("name"); project.add_argument("--goal", default=""); project.add_argument("--keywords", default="")
    proposal = sub.add_parser("proposal"); proposal.add_argument("id"); proposal.add_argument("--approve", action="store_true"); proposal.add_argument("--reject", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            initialize(args.home); print(str(args.home / "config.json")); return 0
        if args.command == "demo":
            # Never alter the real config or call a model in demo mode.
            with tempfile.TemporaryDirectory(prefix="work-assistant-demo-") as directory:
                home = Path(directory); initialize(home)
                cfg = Settings(local_data_approved=True)
                rt = Runtime(home, cfg)
                rt.ingestor.envelope(synthetic_envelope())
                print(json.dumps({"mode": "offline_synthetic_demo", "results": rt.store.search("塗工 測定条件")}, ensure_ascii=False, indent=2))
            return 0
        initialize(args.home)
        if args.command == "doctor":
            from .doctor import diagnose
            report = diagnose(args.home, args.live)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return int(any(c["status"] == "FAIL" for c in report["checks"]))
        rt = Runtime(args.home)
        if args.command == "gui":
            from .ui import Desktop
            Desktop(rt).run(); return 0
        if args.command == "run":
            rt.start()
            try:
                while not rt.stop_event.wait(1): pass
            except KeyboardInterrupt: rt.close()
            return 0
        if args.command == "scan": output = rt.scan()
        elif args.command == "search":
            output = []
            for hit in rt.store.search(args.query, 12):
                try: rt.ingestor.validate_live_source(hit["id"])
                except AssistantError: continue
                output.append(hit)
        elif args.command == "ask":
            result = rt.ask(args.text)
            output = {"text": result.text, "citations": result.citations, "trace": result.trace}
        elif args.command == "import":
            if args.envelope:
                if args.path.stat().st_size > 1_000_000: raise AssistantError("ENVELOPE_FILE_TOO_LARGE")
                output = {"result": rt.ingestor.envelope(json.loads(args.path.read_text(encoding="utf-8-sig")))}
            else: output = {"result": rt.ingestor.file(args.path)}
        elif args.command == "project":
            if not rt.cfg.local_data_approved: raise AssistantError("LOCAL_DATA_NOT_APPROVED")
            output = {"project_id": rt.store.add_project(args.name, args.goal, [k.strip() for k in args.keywords.split(",") if k.strip()])}
        elif args.command == "proposal":
            if args.approve == args.reject: raise AssistantError("CHOOSE_APPROVE_OR_REJECT")
            item = next((p for p in rt.store.proposals() if p["id"] == args.id), None)
            if not item: raise AssistantError("PROPOSAL_NOT_PENDING")
            rt.ingestor.validate_live_source(item["document_id"])
            rt.store.decide_proposal(args.id, args.approve)
            output = {"status": "approved" if args.approve else "rejected"}
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except (AssistantError, OSError, ValueError) as error:
        print(json.dumps({"error": safe_code(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
