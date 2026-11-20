"""Collect structured events from a tool-free Claude Code run, or replay JSONL."""

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
import sys
from uuid import uuid4


MAX_LINE_BYTES = 256 * 1024
MAX_EVENTS = 10_000
USAGE_FIELDS = (
    "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def text(value, limit=200):
    return value[:limit] if isinstance(value, str) else None


def obj(value):
    return value if isinstance(value, dict) else {}


def nonnegative_int(value):
    return value if type(value) is int and value >= 0 else None


def cost(value):
    if type(value) in (int, float) and 0 <= value <= sys.float_info.max and math.isfinite(value):
        return value
    return None


class Collector:
    def __init__(self, output, *, replay=False):
        self.output = output
        self.source = "replay" if replay else "claude_code"
        self.run_id = str(uuid4())
        self.event_count = 0
        self.invalid_lines = 0
        self.oversize_lines = 0
        self.dropped_events = 0
        self.event_types = Counter()
        self.stderr_bytes = 0
        self.session_id = None
        self.model = None
        self.cli_version = None
        self.result = None

    def emit(self, kind, event, **details):
        if self.event_count >= MAX_EVENTS:
            self.dropped_events += 1
            return
        row = {
            "run_id": self.run_id, "seq": self.event_count + 1,
            "source": self.source, "observed_at": utc_now(), "kind": kind,
            "session_id": text(event.get("session_id")), "native_id": text(event.get("uuid")),
            "parent_tool_use_id": text(event.get("parent_tool_use_id")), **details,
        }
        self.output.write(json.dumps(row, allow_nan=False) + "\n")
        self.output.flush()
        self.event_count += 1
        self.event_types[kind] += 1

    def consume(self, line):
        if len(line) > MAX_LINE_BYTES:
            self.oversize_lines += 1
            return
        if not line.strip():
            return
        try:
            event = json.loads(line.decode("utf-8"))
        except (ValueError, RecursionError):
            self.invalid_lines += 1
            return
        if not isinstance(event, dict) or not isinstance(event.get("type"), str) or not event["type"]:
            self.invalid_lines += 1
            return

        kind = event["type"]
        subtype = text(event.get("subtype"))
        if kind == "system" and subtype == "init":
            self.session_id = text(event.get("session_id"))
            self.model = text(event.get("model"))
            self.cli_version = text(event.get("claude_code_version"))
            self.emit("session_started", event, model=self.model, cli_version=self.cli_version)
        elif kind == "system" and subtype in ("api_retry", "permission_denied"):
            self.emit(subtype, event, attempt=nonnegative_int(event.get("attempt")),
                      error_status=nonnegative_int(event.get("error_status")))
        elif kind == "result":
            usage = obj(event.get("usage"))
            self.result = {
                "subtype": subtype,
                "is_error": event.get("is_error") if isinstance(event.get("is_error"), bool) else None,
                "result": text(event.get("result"), 4096),
                "result_truncated": isinstance(event.get("result"), str) and len(event["result"]) > 4096,
                "usage": {key: nonnegative_int(usage.get(key)) for key in USAGE_FIELDS} if usage else None,
                "estimated_cost_usd": cost(event.get("total_cost_usd")),
                "stop_reason": text(event.get("stop_reason")),
            }
            self.session_id = self.session_id or text(event.get("session_id"))
            # Result text is shown in the CLI summary, not saved in the event file.
            meta = {key: val for key, val in self.result.items() if key != "result"}
            self.emit("agent_result", event, **meta)
        elif kind in ("assistant", "user"):
            self.emit(f"{kind}_message", event)
            blocks = obj(event.get("message")).get("content")
            for block in blocks if isinstance(blocks, list) else ():
                block = obj(block)
                if kind == "assistant" and block.get("type") == "tool_use":
                    self.emit("tool_requested", event, tool_use_id=text(block.get("id")),
                              tool_name=text(block.get("name")))
                if kind == "user" and block.get("type") == "tool_result":
                    self.emit("tool_result", event, tool_use_id=text(block.get("tool_use_id")),
                              is_error=block.get("is_error") if isinstance(block.get("is_error"), bool) else None)
        elif kind == "stream_event":
            # Count completed messages, not token fragments. No hidden reasoning is retained.
            return
        else:
            self.emit("other", event, original_type=text(kind), subtype=subtype)

    def summary(self, status, child_exit_code=None, duration_ms=None, error=None):
        return {
            "run_id": self.run_id, "source": self.source, "status": status,
            "child_exit_code": child_exit_code, "duration_ms": duration_ms,
            "error": error, "event_count": self.event_count, "event_types": dict(self.event_types),
            "invalid_line_count": self.invalid_lines, "oversize_line_count": self.oversize_lines,
            "dropped_event_count": self.dropped_events, "stderr_bytes": self.stderr_bytes,
            "session_id": self.session_id, "model": self.model, "cli_version": self.cli_version,
            "terminal": self.result, "event_file": self.output.name,
        }

    def finish(self, exit_code, duration_ms):
        if exit_code != 0:
            error = "nonzero_exit"
        elif self.result is None:
            error = "missing_result"
        elif (self.result["subtype"] != "success" or self.result["is_error"] is not False
              or self.result["stop_reason"] == "refusal"):
            error = "unsuccessful_result"
        else:
            error = None
        return self.summary("failed" if error else "succeeded", exit_code, duration_ms, error)


async def capture_stdout(reader, collector):
    oversize = False
    while True:
        try:
            line = await reader.readuntil(b"\n")
        except asyncio.LimitOverrunError as exc:
            if not oversize:
                collector.oversize_lines += 1
                oversize = True
            await reader.readexactly(exc.consumed)
            continue
        except asyncio.IncompleteReadError as exc:
            if not oversize and exc.partial:
                collector.consume(exc.partial)
            return
        if not oversize:
            collector.consume(line)
        oversize = False


async def drain_stderr(reader, collector):
    while chunk := await reader.read(8192):
        collector.stderr_bytes += len(chunk)


async def supervise(argv, cwd, timeout, collector):
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=MAX_LINE_BYTES,
        )
    except OSError as exc:
        return collector.summary("failed", error=f"launch_error: {exc.strerror}")

    tasks = [asyncio.create_task(coro) for coro in (
        capture_stdout(proc.stdout, collector), drain_stderr(proc.stderr, collector), proc.wait(),
    )]
    timed_out = False
    try:
        _, pending = await asyncio.wait(tasks, timeout=max(0, timeout - (loop.time() - started)),
                                        return_when=asyncio.FIRST_EXCEPTION)
        for task in tasks:
            if task.done():
                task.result()
        timed_out = bool(pending)
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        _, pending = await asyncio.wait(tasks, timeout=1)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    elapsed = round((loop.time() - started) * 1000)
    if timed_out:
        return collector.summary("timed_out", proc.returncode, elapsed, "timeout")
    return collector.finish(proc.returncode, elapsed)


def replay(input_file, collector):
    with input_file.open("rb") as source:
        while line := source.readline(MAX_LINE_BYTES + 1):
            if len(line) > MAX_LINE_BYTES:
                collector.oversize_lines += 1
                while line and not line.endswith(b"\n"):
                    line = source.readline(MAX_LINE_BYTES + 1)
            else:
                collector.consume(line)
    # A saved event stream cannot attest to the original process exit code.
    return collector.summary("replayed")


def claude_command(prompt, model=None):
    native_name = "claude.exe" if os.name == "nt" else "claude"
    binary = shutil.which(native_name)
    if binary is None:
        raise FileNotFoundError(f"{native_name} was not found on PATH")
    args = [binary, "-p", "--output-format", "stream-json", "--verbose", "--safe-mode",
            "--tools", "", "--permission-mode", "manual", "--no-session-persistence"]
    if model:
        args += ["--model", model]
    return [*args, "--", prompt]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    run = commands.add_parser("run", help="start a real Claude Code run (may incur cost)")
    run.add_argument("--prompt", default="Reply with exactly: DEMO_OK")
    run.add_argument("--model", help="optional Claude Code model selection")
    run.add_argument("--cwd", type=Path, default=Path.cwd(), help="trusted working directory")
    run.add_argument("--timeout", type=int, default=120, help="runtime limit in seconds")
    offline = commands.add_parser("replay", help="parse existing CLI JSONL without model calls")
    offline.add_argument("input", type=Path)
    for command in (run, offline):
        command.add_argument("--output", required=True, type=Path, help="new normalized JSONL file")
    args = parser.parse_args()

    try:
        if args.mode == "run":
            if not args.cwd.is_dir() or not 1 <= args.timeout <= 3600:
                parser.error("run requires an existing --cwd and --timeout in 1..3600")
            if "\0" in args.prompt or (args.model and (args.model.startswith("-") or "\0" in args.model)):
                parser.error("invalid prompt or model argument")
            argv = claude_command(args.prompt, args.model)
        elif not args.input.is_file():
            parser.error("replay requires an existing input file")

        with args.output.open("x", encoding="utf-8", newline="\n") as output:
            collector = Collector(output, replay=args.mode == "replay")
            if args.mode == "run":
                summary = asyncio.run(supervise(argv, args.cwd, args.timeout, collector))
            else:
                summary = replay(args.input, collector)
    except OSError as exc:
        print(f"Collector error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    print(json.dumps(summary, indent=2, allow_nan=False))
    return {"succeeded": 0, "replayed": 0, "timed_out": 124}.get(summary["status"], 1)


if __name__ == "__main__":
    raise SystemExit(main())
