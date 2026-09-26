"""Command line: install the Remote Script, preview templates, check Live, benchmark."""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from . import templates as tpl
from .connection import LiveConnection, LiveError

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_SOURCE = REPO_ROOT / "remote_script" / "AbletonHelper"


def find_user_library() -> Path:
    """The User Library Live is configured to use (Library.cfg), else the default."""
    prefs = Path.home() / "Library" / "Preferences" / "Ableton"
    if sys.platform == "win32":
        prefs = Path.home() / "AppData" / "Roaming" / "Ableton"
    versions = sorted(prefs.glob("Live *"),
                      key=lambda p: [int(n) for n in re.findall(r"\d+", p.name)], reverse=True)
    for version in versions:
        cfg = version / "Library.cfg"
        if not cfg.exists():
            continue
        try:
            root = ET.parse(cfg).getroot()
        except ET.ParseError:
            continue
        for node in root.iter():
            if "userlibrary" not in node.tag.lower():
                continue
            path = name = None
            for sub in node.iter():
                if "projectpath" in sub.tag.lower():
                    path = sub.attrib.get("Value")
                elif "projectname" in sub.tag.lower():
                    name = sub.attrib.get("Value")
            if path and name and (Path(path) / name).is_dir():
                return Path(path) / name
    default = Path.home() / "Music" / "Ableton" / "User Library"
    if sys.platform == "win32":
        default = Path.home() / "Documents" / "Ableton" / "User Library"
    return default


def cmd_install(args) -> int:
    library = Path(args.user_library) if args.user_library else find_user_library()
    if not library.is_dir():
        print(f"User Library not found at {library}. Pass --user-library.")
        return 1
    target = library / "Remote Scripts" / "AbletonHelper"
    target.parent.mkdir(exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(SCRIPT_SOURCE, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    print(f"Installed the Remote Script to {target}")
    print("Next: restart Live (or reopen Preferences), go to Link, Tempo & MIDI, and set a free "
          "Control Surface slot to 'AbletonHelper'. Input and Output can stay 'None'.")
    return 0


def cmd_preview(args) -> int:
    try:
        arr = tpl.compile_arrangement(tpl.load_template(args.genre), args.variant, args.bpm)
    except tpl.TemplateError as e:
        print(e)
        return 1
    print(tpl.describe(arr))
    return 0


def cmd_templates(args) -> int:
    for genre in tpl.available_genres():
        t = tpl.load_template(genre)
        print(f"{genre:15} {t.name} ({t.bpm:g} BPM): {', '.join(t.variants)}")
    return 0


def cmd_status(args) -> int:
    try:
        print(LiveConnection().ping())
        return 0
    except LiveError as e:
        print(e)
        return 1


def cmd_bench(args) -> int:
    from .bench import run_benchmark
    return run_benchmark(args)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ableton-helper")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("install", help="Copy the Remote Script into Live's User Library")
    p.add_argument("--user-library", help="Path to your User Library, if it's somewhere unusual")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("templates", help="List genre templates")
    p.set_defaults(func=cmd_templates)

    p = sub.add_parser("preview", help="Print a template's arrangement")
    p.add_argument("genre")
    p.add_argument("--variant", default="extended")
    p.add_argument("--bpm", type=float)
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser("status", help="Check the connection to Live")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("bench", help="Time one-change-per-request vs one batch (run in an empty set)")
    p.add_argument("--genre", default="house")
    p.add_argument("--variant", default="extended")
    p.add_argument("--seconds-per-tool-call", type=float, default=3.0,
                   help="Assumed Claude turn time per tool call, for the estimate")
    p.add_argument("--yes", action="store_true", help="Don't ask before changing the open set")
    p.add_argument("--no-write", action="store_true", help="Don't append results to docs/benchmarks.md")
    p.set_defaults(func=cmd_bench)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
