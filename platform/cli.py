import argparse


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["ingest-kb"])
    parser.parse_args()
    parser.exit(2, "KB ingestion requires the UC1 P1/P2 corpus and retrieval pipeline.\n")


if __name__ == "__main__":
    main()
