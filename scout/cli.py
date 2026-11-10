"""Command-line entry point: one sub-command per pipeline stage."""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

from . import db
from .config import load_config


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="scout", description="Screen new arXiv papers and cross-reference authors with startup founders.")
    p.add_argument("-c", "--config", default=None, help="path to config.toml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the database")

    s = sub.add_parser("ingest", help="pull new arXiv submissions")
    s.add_argument("--since", help="ISO date/datetime (UTC). Default: last run's end, else yesterday")
    s.add_argument("--until", help="ISO date/datetime (UTC). Default: now")
    s.add_argument("--limit", type=int, help="stop after N papers (testing)")

    s = sub.add_parser("enrich", help="look up authors in Semantic Scholar / OpenAlex")
    s.add_argument("--limit", type=int, help="max papers to enrich this run")
    s.add_argument("--since", help="only papers submitted on/after this date")

    s = sub.add_parser("screen", help="score papers (deterministic + LLM)")
    s.add_argument("--limit", type=int, help="max papers to send to the LLM this run")
    s.add_argument("--since", help="only papers submitted on/after this date")
    s.add_argument("--no-llm", action="store_true", help="compute deterministic criteria only")
    s.add_argument("--rescore", action="store_true", help="re-score papers that already have scores")

    s = sub.add_parser("founders", help="refresh the founders/companies tables from enabled sources")
    s.add_argument("--source", action="append", help="run only this source (repeatable)")

    s = sub.add_parser("match", help="cross-reference authors against founders")
    s.add_argument("--since", help="only authors of papers submitted on/after this date")
    s.add_argument("--rematch", action="store_true", help="recompute existing matches")
    s.add_argument("--review", nargs=2, metavar=("MATCH_ID", "accepted|rejected"), help="record a manual decision")

    s = sub.add_parser("report", help="write the weekly report")
    s.add_argument("--since", help="start of the week (default: 7 days ago)")
    s.add_argument("--until", help="end of the week (default: now)")
    s.add_argument("--format", choices=["md", "html", "both"], default="md")
    s.add_argument("--out", help="output path (default: reports/<year>-W<week>.<ext>)")

    s = sub.add_parser("run", help="ingest -> enrich -> screen -> founders -> match -> report")
    s.add_argument("--since")
    s.add_argument("--limit", type=int)
    s.add_argument("--no-llm", action="store_true")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = load_config(args.config)
    conn = db.connect(cfg["general"]["db_path"])

    if args.cmd == "init":
        print(f"database ready at {cfg['general']['db_path']}")
        return 0
    if args.cmd == "ingest":
        from . import ingest
        stats = ingest.run(cfg, conn, _dt(args.since), _dt(args.until), args.limit)
        print(f"ingest: {stats}")
        return 0
    if args.cmd == "enrich":
        from . import enrich
        print(f"enrich: {enrich.run(cfg, conn, limit=args.limit, since=_dt(args.since))}")
        return 0
    if args.cmd == "screen":
        from . import screen
        print(f"screen: {screen.run(cfg, conn, limit=args.limit, since=_dt(args.since), use_llm=not args.no_llm, rescore=args.rescore)}")
        return 0
    if args.cmd == "founders":
        from . import founders
        print(f"founders: {founders.run(cfg, conn, only=args.source)}")
        return 0
    if args.cmd == "match":
        from . import match
        if args.review:
            match.review(conn, int(args.review[0]), args.review[1])
            print("recorded")
            return 0
        print(f"match: {match.run(cfg, conn, since=_dt(args.since), rematch=args.rematch)}")
        return 0
    if args.cmd == "report":
        from . import report
        for path in report.run(cfg, conn, since=_dt(args.since), until=_dt(args.until), fmt=args.format, out=args.out):
            print(f"wrote {path}")
        return 0
    if args.cmd == "run":
        from . import enrich, founders, ingest, match, report, screen
        print(f"ingest: {ingest.run(cfg, conn, _dt(args.since), None, args.limit)}")
        print(f"enrich: {enrich.run(cfg, conn, limit=args.limit)}")
        print(f"screen: {screen.run(cfg, conn, limit=args.limit, use_llm=not args.no_llm)}")
        print(f"founders: {founders.run(cfg, conn)}")
        print(f"match: {match.run(cfg, conn)}")
        for path in report.run(cfg, conn):
            print(f"wrote {path}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
