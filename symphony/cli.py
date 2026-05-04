"""CLI entry point for Symphony."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from symphony.logging_config import configure_logging
from symphony.orchestrator import Orchestrator
from symphony.server import SymphonyServer
from symphony.workflow import resolve_workflow_path

logger = logging.getLogger("symphony.cli")


def _parse_run_args(argv: list[str]) -> argparse.Namespace:
    """Parse arguments for the main orchestrator run mode."""
    parser = argparse.ArgumentParser(
        prog="symphony",
        description="Symphony – orchestration service for coding agents",
    )
    parser.add_argument(
        "workflow_path",
        nargs="?",
        default=None,
        help="Path to WORKFLOW.md (default: ./WORKFLOW.md)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Enable HTTP server on this port (overrides server.port in workflow)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )
    parser.add_argument(
        "--dev",
        action="store_true",
        help="Enable dev mode with mock tracker and mock agent",
    )
    parser.add_argument(
        "--instance",
        type=str,
        default=None,
        help="Dev instance ID (auto-generated if omitted)",
    )
    parser.add_argument(
        "--dev-seed",
        type=int,
        default=0,
        help="Pre-populate mock tracker with N issues on startup",
    )
    return parser.parse_args(argv)


# Backward-compatible alias used by tests
_parse_args = _parse_run_args


def _parse_dev_args(argv: list[str]) -> argparse.Namespace:
    """Parse arguments for the dev subcommand (CLI sidecar)."""
    parser = argparse.ArgumentParser(prog="symphony dev")
    sub = parser.add_subparsers(dest="dev_command")

    add_p = sub.add_parser("add-issue", help="Add an issue to the mock tracker")
    add_p.add_argument("--port", type=int, required=True, help="Port of running instance")
    add_p.add_argument("--title", required=True, help="Issue title")
    add_p.add_argument("--state", default="open", help="Issue state (default: open)")
    add_p.add_argument("--labels", default="", help="Comma-separated labels")
    add_p.add_argument("--body", default="", help="Issue body")
    add_p.add_argument("--number", type=int, default=None, help="Explicit issue number")

    state_p = sub.add_parser("set-state", help="Update an issue's state")
    state_p.add_argument("--port", type=int, required=True)
    state_p.add_argument("--number", type=int, required=True, help="Issue number")
    state_p.add_argument("--state", required=True, help="New state")

    list_p = sub.add_parser("list-issues", help="List all issues in mock tracker")
    list_p.add_argument("--port", type=int, required=True)

    seed_p = sub.add_parser("seed", help="Bulk-create synthetic issues")
    seed_p.add_argument("--port", type=int, required=True)
    seed_p.add_argument("--count", type=int, default=5, help="Number of issues to create")

    err_p = sub.add_parser("inject-error", help="Inject an error response")
    err_p.add_argument("--port", type=int, required=True)
    err_p.add_argument("--key", required=True, help="Error key (e.g. 'list', 'issue:5')")
    err_p.add_argument("--status", type=int, required=True, help="HTTP status code")
    err_p.add_argument("--body", default="error", help="Error body")

    clear_p = sub.add_parser("clear-errors", help="Clear all injected errors")
    clear_p.add_argument("--port", type=int, required=True)

    return parser.parse_args(argv)


async def _dev_command(args: argparse.Namespace) -> int:
    """Execute a dev control command against a running instance."""
    import httpx

    base = f"http://127.0.0.1:{args.port}"

    async with httpx.AsyncClient() as client:
        try:
            if args.dev_command == "add-issue":
                labels = [l.strip() for l in args.labels.split(",") if l.strip()]
                resp = await client.post(f"{base}/dev/issues", json={
                    "title": args.title,
                    "state": args.state,
                    "labels": labels,
                    "body": args.body,
                    "number": args.number,
                })
                print(json.dumps(resp.json(), indent=2))

            elif args.dev_command == "set-state":
                resp = await client.patch(
                    f"{base}/dev/issues/{args.number}",
                    json={"state": args.state},
                )
                print(json.dumps(resp.json(), indent=2))

            elif args.dev_command == "list-issues":
                resp = await client.get(f"{base}/dev/issues")
                print(json.dumps(resp.json(), indent=2))

            elif args.dev_command == "seed":
                resp = await client.post(f"{base}/dev/issues/seed", json={"count": args.count})
                data = resp.json()
                print(f"Created {data.get('created', 0)} issues")

            elif args.dev_command == "inject-error":
                resp = await client.post(f"{base}/dev/errors", json={
                    "key": args.key,
                    "status": args.status,
                    "body": args.body,
                })
                print(json.dumps(resp.json(), indent=2))

            elif args.dev_command == "clear-errors":
                resp = await client.delete(f"{base}/dev/errors")
                print(json.dumps(resp.json(), indent=2))

            else:
                print("Unknown dev command. Use: add-issue, set-state, list-issues, seed, inject-error, clear-errors")
                return 1

        except httpx.ConnectError:
            print(f"Error: Could not connect to dev instance at port {args.port}")
            return 1

    return 0


async def _run(args: argparse.Namespace) -> int:
    """Run the orchestrator (async entry point)."""
    workflow_path = resolve_workflow_path(args.workflow_path)

    orch = Orchestrator(
        workflow_path=workflow_path,
        port=args.port,
        dev_mode=args.dev,
        dev_instance=args.instance,
        dev_seed=args.dev_seed,
    )
    server: SymphonyServer | None = None

    try:
        if args.dev:
            # In dev mode: orchestrator handles the full startup sequence
            await orch.start_dev_mode()
            logger.info(
                "dev_mode_started instance=%s port=%s",
                orch._dev_instance, orch.dev_port,
            )
        else:
            await orch.start()

            # Start HTTP server if configured
            port = args.port
            if port is None and orch.config:
                port = orch.config.server_port
            if port is not None:
                server = SymphonyServer(orch)
                actual_port = await server.start(port)
                logger.info("server_listening port=%d", actual_port)

        # Run until interrupted
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logger.error("startup_failed error=%s", exc)
        return 1
    finally:
        if server:
            await server.stop()
        await orch.stop()

    return 0


def main(argv: list[str] | None = None) -> None:
    """CLI main entry point."""
    raw_argv = argv if argv is not None else sys.argv[1:]

    # Route to dev subcommand handler if first arg is "dev"
    if raw_argv and raw_argv[0] == "dev":
        args = _parse_dev_args(raw_argv[1:])
        # Need log level for dev commands too
        configure_logging("INFO")
        if not args.dev_command:
            print("Usage: symphony dev <command> [options]")
            print("Commands: add-issue, set-state, list-issues, seed, inject-error, clear-errors")
            sys.exit(1)
        try:
            exit_code = asyncio.run(_dev_command(args))
        except KeyboardInterrupt:
            exit_code = 0
        sys.exit(exit_code)

    # Normal orchestrator run
    args = _parse_run_args(raw_argv)
    configure_logging(args.log_level)

    try:
        exit_code = asyncio.run(_run(args))
    except KeyboardInterrupt:
        exit_code = 0

    sys.exit(exit_code)


if __name__ == "__main__":
    main()

