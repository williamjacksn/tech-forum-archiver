"""Archive the public phpBB forum at tech.churchofjesuschrist.org into SQLite."""

import sys


def main() -> None:
    from tech_forum_archiver.cli import main as cli_main

    sys.exit(cli_main())
