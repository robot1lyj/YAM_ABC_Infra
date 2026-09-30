"""Offline PARTS construction/validation/publication. Never connects hardware."""

import argparse
import json
import subprocess
import threading


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    mock = sub.add_parser("mock")
    mock.add_argument("output")
    mock.add_argument("--mode", choices=("shadow", "collect", "eval"), default="shadow")
    check = sub.add_parser("validate")
    check.add_argument("run")
    finish = sub.add_parser("finalize")
    finish.add_argument("run")
    finish.add_argument("--episode", action="append", required=True)
    send = sub.add_parser("publish")
    send.add_argument("run")
    send.add_argument("--outbox", required=True)
    send.add_argument("--destination", required=True)
    send.add_argument("--serve", action="store_true")
    args = p.parse_args()
    from .recording import finalize, validate_package

    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if args.command == "mock":
        from .mock import produce

        result = produce(args.output, producer_sha=sha, mode=args.mode)
    elif args.command == "validate":
        errors = validate_package(args.run)
        result = dict(errors=errors, valid=not errors)
    elif args.command == "finalize":
        result = finalize(args.run, episodes=args.episode, producer_sha=sha)
    else:
        from .outbox import DirectoryTransport, Outbox

        outbox = Outbox(args.outbox, DirectoryTransport(args.destination))
        pid = outbox.enqueue(args.run)
        if args.serve:
            try:
                outbox.serve(threading.Event())
            except KeyboardInterrupt:
                pass
        else:
            outbox.drain_once()
        result = dict(publication_id=pid, outbox=args.outbox)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command == "validate" and not result["valid"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
