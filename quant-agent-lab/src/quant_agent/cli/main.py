from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from quant_agent.domain.errors import DomainError
from quant_agent.domain.models import to_dict
from quant_agent.orchestration.service import ApplicationService


def _json(value: object) -> str:
    return json.dumps(to_dict(value), ensure_ascii=False, indent=2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quant-agent", description="Offline quantitative paper-trading MVP"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db", help="initialize the project-local SQLite state")
    seed = commands.add_parser("seed-demo", help="write deterministic offline demo data")
    seed.add_argument(
        "--reset", action="store_true", help="reset project-local runtime state first"
    )
    report = commands.add_parser(
        "generate-report", help="generate a JSON and Markdown daily report"
    )
    report.add_argument("--date", default=None)
    report.add_argument("--request-id", default="cli-generate")
    show = commands.add_parser("report", help="show a stored report")
    show.add_argument("report_id")
    approve = commands.add_parser("approve", help="approve all or selected risk-allowed orders")
    approve.add_argument("report_id")
    approve.add_argument("--all", action="store_true", dest="approve_all")
    approve.add_argument("--order-id", action="append", default=[])
    approve.add_argument("--approver", default="user")
    reject = commands.add_parser("reject", help="reject a pending report")
    reject.add_argument("report_id")
    reject.add_argument("--approver", default="user")
    execute = commands.add_parser("execute", help="execute an approved plan in PaperBroker")
    execute.add_argument("report_id")
    execute.add_argument("--paper", action="store_true", help="required explicit paper mode flag")
    execute.add_argument("--request-id", default=None)
    status = commands.add_parser("status", help="show report, approval, and execution status")
    status.add_argument("report_id")
    kill = commands.add_parser(
        "kill-switch", help="enable or disable the project-local kill switch"
    )
    kill_group = kill.add_mutually_exclusive_group(required=True)
    kill_group.add_argument("--on", action="store_true", dest="enabled")
    kill_group.add_argument("--off", action="store_false", dest="enabled")
    kill.add_argument("--reason", default="operator request")
    kill.add_argument("--actor", default="user")
    demo = commands.add_parser("demo", help="run seed -> report -> approve -> paper execute")
    demo.add_argument("--date", default=None)
    return parser


def run(args: argparse.Namespace, service: ApplicationService) -> object:
    if args.command == "init-db":
        return service.init_db()
    if args.command == "seed-demo":
        return service.seed_demo(reset_runtime=args.reset)
    if args.command == "generate-report":
        report = service.generate_report(args.date, request_id=args.request_id)
        return {
            "report_id": report.report_id,
            "status": report.status.value,
            "plan_hash": report.plan.plan_hash,
            "order_ids": [order.order_id for order in report.plan.orders],
            "json_report": str(service.paths.reports_dir / f"{report.report_id}.json"),
            "markdown_report": str(service.paths.reports_dir / f"{report.report_id}.md"),
        }
    if args.command == "report":
        return service.get_report(args.report_id)
    if args.command == "approve":
        if args.approve_all == bool(args.order_id):
            raise DomainError("choose exactly one of --all or one or more --order-id values")
        if args.approve_all:
            return service.approve_all(args.report_id, args.approver)
        return service.approve_partial(args.report_id, tuple(args.order_id), args.approver)
    if args.command == "reject":
        return service.reject(args.report_id, args.approver)
    if args.command == "execute":
        if not args.paper:
            raise DomainError("execution requires explicit --paper; live mode is disabled")
        return service.execute(args.report_id, mode="paper", request_id=args.request_id)
    if args.command == "status":
        return service.status(args.report_id)
    if args.command == "kill-switch":
        return service.set_kill_switch(args.enabled, args.reason, args.actor)
    if args.command == "demo":
        return service.demo(args.date)
    raise DomainError(f"unknown command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        print(_json(run(args, ApplicationService())))
    except DomainError as exc:
        print(
            json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    except (FileNotFoundError, ValueError) as exc:
        print(
            json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
