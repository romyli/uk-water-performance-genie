"""Job task entry point: `run_stage.py <stage> [--source-id ID] --catalog ... --run-id ...`.

It only reads arguments, puts the water_performance package on the path and calls the stage.
All behaviour lives in the tested package.
"""

import argparse
import os
import sys

sys.dont_write_bytecode = True


def _src_dir() -> str:
    here = globals().get("__file__") or sys.argv[0]
    return os.path.dirname(os.path.dirname(os.path.abspath(here)))


sys.path.insert(0, _src_dir())

from water_performance import runtime, tasks  # noqa: E402

STAGES = ("setup", "discover", "fetch", "inventory", "parse", "conform", "publish", "quality", "finalize", "rollback")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=STAGES)
    parser.add_argument("--source-id", default=None)
    parser.add_argument("--release-id", default=None, help="rollback only: the release to publish")
    runtime.add_target_args(parser)
    args = parser.parse_args(argv)
    runtime.configure_logging()
    target = runtime.target_from_args(args)
    spark = runtime.get_spark()
    fn = getattr(tasks, args.stage)
    kwargs = {}
    if args.stage in ("fetch", "parse"):
        if not args.source_id:
            parser.error(f"{args.stage} needs --source-id")
        kwargs["source_id"] = args.source_id
    if args.stage == "rollback":
        kwargs["release_id"] = args.release_id
    result = fn(spark, target, args.run_id, **kwargs)
    print(f"{args.stage} {args.source_id or ''}: {result}")


if __name__ == "__main__":
    main()
