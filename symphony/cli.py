"""CLI entry point for Symphony."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from symphony.logging_config import configure_logging
from symphony.orchestrator import Orchestrator
from symphony.server import SymphonyServer
from symphony.workflow import resolve_workflow_path

logger = logging.getLogger("symphony.cli")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
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
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    """Run the orchestrator (async entry point)."""
    workflow_path = resolve_workflow_path(args.workflow_path)

    orch = Orchestrator(workflow_path=workflow_path, port=args.port)
    server: SymphonyServer | None = None

    try:
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
    args = _parse_args(argv)
    configure_logging(args.log_level)

    try:
        exit_code = asyncio.run(_run(args))
    except KeyboardInterrupt:
        exit_code = 0

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
