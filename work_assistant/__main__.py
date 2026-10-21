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
    work = sub.add_parser("work", help="継続タスクと実行結果"); works = work.add_subparsers(dest="action", required=True)
    add = works.add_parser("add"); add.add_argument("goal"); add.add_argument("--agent", default="main"); add.add_argument("--document", action="append", default=[]); add.add_argument("--key"); add.add_argument("--at"); add.add_argument("--deadline")
    for name in ("list", "metrics", "run-next"): works.add_parser(name)
    for name in ("show", "cancel", "retry"):
        item = works.add_parser(name); item.add_argument("id")
    feedback = works.add_parser("feedback"); feedback.add_argument("id"); choice = feedback.add_mutually_exclusive_group(required=True)
    choice.add_argument("--helpful", action="store_true"); choice.add_argument("--unnecessary", action="store_true")
    feedback.add_argument("--intervened", action="store_true"); feedback.add_argument("--relevant", type=int); feedback.add_argument("--retrieved", type=int)
    roles = sub.add_parser("role", help="役割と利用できるツール・資料").add_subparsers(dest="action", required=True)
    roles.add_parser("list")
    role = roles.add_parser("set"); role.add_argument("id"); role.add_argument("name"); role.add_argument("--instructions", default=""); role.add_argument("--tools", default=""); role.add_argument("--documents"); role.add_argument("--projects")
    memories = sub.add_parser("memory", help="役割ごとの保存した記憶").add_subparsers(dest="action", required=True)
    mem = memories.add_parser("add"); mem.add_argument("text"); mem.add_argument("--agent", default="main"); mem.add_argument("--kind", choices=["semantic", "episodic", "procedural"], default="semantic"); mem.add_argument("--document"); mem.add_argument("--importance", type=float, default=.5)
    mem = memories.add_parser("search"); mem.add_argument("query"); mem.add_argument("--agent", default="main")
    mem = memories.add_parser("forget"); mem.add_argument("id"); mem.add_argument("--agent", default="main")
    routines = sub.add_parser("routine", help="保存したスケジュール・イベント起動").add_subparsers(dest="action", required=True)
    routines.add_parser("list")
    routine = routines.add_parser("add"); routine.add_argument("name"); routine.add_argument("instructions"); routine.add_argument("--agent", default="main"); routine.add_argument("--every-minutes", type=int, default=0); routine.add_argument("--at"); routine.add_argument("--event", choices=["document", "bridge", "user"]); routine.add_argument("--event-key", default=""); routine.add_argument("--enabled", action="store_true")
    for name in ("enable", "disable"):
        item = routines.add_parser(name); item.add_argument("id")
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
        elif args.command == "work":
            if args.action == "add":
                if not rt.cfg.local_data_approved: raise AssistantError("LOCAL_DATA_NOT_APPROVED")
                for did in args.document: rt.ingestor.validate_live_source(did)
                output = {"job_id": rt.work.submit(args.goal, agent_id=args.agent, document_ids=args.document, idempotency_key=args.key, run_after=args.at, deadline=args.deadline)}
            elif args.action == "list": output = rt.work.jobs()
            elif args.action == "metrics": output = rt.work.metrics()
            elif args.action == "run-next": output = rt.work_runner.run_one()
            elif args.action == "show":
                rt.store.expire(); job = rt.work.job(args.id); artifacts = rt.work.artifacts(args.id)
                for source in job["result"].get("citations", []) + [s for a in artifacts for s in a["citations"]]: rt.ingestor.validate_live_source(source["id"])
                output = {k: v for k, v in job.items() if k not in {"token", "checkpoint", "input_hash"}}
                output["artifacts"] = artifacts
            elif args.action in ("cancel", "retry"):
                getattr(rt.work, args.action)(args.id); output = {"status": rt.work.job(args.id)["status"]}
            else:
                rt.work.feedback(args.id, helpful=args.helpful, intervened=args.intervened, retrieval_relevant=args.relevant, retrieval_total=args.retrieved)
                output = {"status": "recorded"}
        elif args.command == "role":
            if args.action == "list": output = [{**p, "capability": p["capability"].as_dict()} for p in rt.work.profiles()]
            else:
                from .policy import READ_TOOLS
                split = lambda value: None if value is None else [s.strip() for s in value.split(",") if s.strip()]
                rt.work.set_profile(args.id, args.name, args.instructions, tools=split(args.tools) if args.tools else READ_TOOLS, document_ids=split(args.documents), project_ids=split(args.projects))
                output = {"status": "saved", "agent_id": args.id}
        elif args.command == "memory":
            if not rt.cfg.local_data_approved: raise AssistantError("LOCAL_DATA_NOT_APPROVED")
            profile = rt.work.profile(args.agent)
            if args.action == "add": output = {"memory_id": rt.agent.memory.put(args.text, namespace=args.agent, kind=args.kind, document_id=args.document, importance=args.importance)}
            elif args.action == "search": output = rt.agent.memory.search(args.query, profile["capability"])
            else: rt.agent.memory.forget(args.id, args.agent); output = {"status": "deleted"}
        elif args.command == "routine":
            if args.action == "list": output = rt.work.routines()
            elif args.action == "add": output = {"routine_id": rt.work.add_routine(args.name, args.instructions, agent_id=args.agent, interval_seconds=args.every_minutes * 60, at=args.at, event_kind=args.event, event_key=args.event_key, enabled=args.enabled)}
            else: rt.work.enable_routine(args.id, args.action == "enable"); output = {"status": args.action + "d"}
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except (AssistantError, OSError, ValueError) as error:
        print(json.dumps({"error": safe_code(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
