import argparse
import asyncio
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["ingest-kb"])
    parser.add_argument("--folder", type=Path)
    args = parser.parse_args()
    from helpdesk_agent.ingest import SAMPLE_KB, ingest

    print(json.dumps(asyncio.run(ingest(args.folder or SAMPLE_KB))))


if __name__ == "__main__":
    main()
