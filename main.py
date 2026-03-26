"""Unified TUI entrypoint for Oracle and Arena modes."""

from __future__ import annotations

import sys

from rich.prompt import Confirm, Prompt

import bot_arena
import oracle_mode
import ui


def main() -> None:
    ui.display_title()
    ui.console.rule("[bold cyan]Poker Control Center[/bold cyan]")

    while True:
        mode = Prompt.ask(
            "Mode",
            choices=["oracle", "arena", "quit"],
            default="arena",
        )

        if mode == "oracle":
            oracle_mode.run_oracle_mode(show_title=False)
        elif mode == "arena":
            bot_arena.run_arena_mode(show_title=False)
        elif mode == "quit":
            break

        if not Confirm.ask("Return to control center?", default=True):
            break

    ui.console.print("[green]Bye from Poker Control Center.[/green]")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        ui.console.print("\n[dim]Interrupted.[/dim]")
        sys.exit(0)
