from __future__ import annotations

import argparse
import logging

from submission.dispatcher import dispatch_cycle, run_forever


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the jobhunt submit dispatcher daemon")
    parser.add_argument("--once", action="store_true", help="run exactly one dispatch cycle")
    parser.add_argument("--dry-run", action="store_true", help="execute adapters in dry-run mode")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.once:
        results = dispatch_cycle(dry_run=args.dry_run)
        for result in results:
            logging.info("dispatch result: %s", result)
        return 0
    run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
