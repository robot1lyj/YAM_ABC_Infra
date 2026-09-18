"""Independent local stop/status when the upper session is unavailable."""
import argparse
import json

from .remote_station import RemoteStationIO


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True)
    parser.add_argument("operation", choices=("status", "stop", "reset_stop", "release"))
    parser.add_argument("--supported", action="store_true")
    args = parser.parse_args()
    if args.operation == "release" and not args.supported:
        parser.error("release removes torque; confirm arms supported with --supported")
    # No constructor: this administrative client must never attach/energize arms.
    client = object.__new__(RemoteStationIO)
    client.path, client.lease = args.socket, None
    print(json.dumps(client._rpc({"op": args.operation, "supported": args.supported}, timeout=10),
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
