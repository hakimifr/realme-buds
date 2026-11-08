"""Which GUI control reproduces each command-line option, and a way to check it.

The rule for this project: anything the command line can do, bypass or force, the GUI can too.
This table states it. ``tests/test_parity.py`` fails if the CLI gains a command or option that is
not listed here, or if a listed one is gone; ``tests/test_gui.py`` fails if a listed GUI control
does not exist. So adding a flag means adding its GUI control, in the same change.

``gui`` is ``<owner>.<attribute>`` where owner is one of:
    window    the main window (BudsWindow)
    advanced  the Advanced dialog (AdvancedDialog)
    hearing   the Golden Sound wizard (HearingTestDialog)
    eqdialog  the custom equaliser editor (CustomEqDialog)
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass


@dataclass(frozen=True)
class Parity:
    cli: str
    gui: str
    note: str = ""


PARITY: tuple[Parity, ...] = (
    # global options
    Parity("--addr", "advanced.address_row"),
    Parity("--channel", "advanced.channel_row"),
    Parity("--transport", "advanced.transport_row"),
    Parity("--service-uuid", "advanced.service_uuid_row"),
    Parity("--trace", "advanced.trace_view"),
    Parity("--profile", "advanced.profile_row"),
    Parity("--profile-file", "advanced.profile_file_button"),
    # commands
    Parity("devices", "advanced.devices_row"),
    Parity("status", "window.battery_rows", "the whole window is the status"),
    Parity("status --json", "advanced.json_button"),
    Parity("anc", "window.anc_buttons"),
    Parity("switch", "window.feature_rows", "also every generated per-feature command"),
    Parity("features", "window.feature_rows"),
    Parity("features --scan", "advanced.scan_button"),
    Parity("eq", "window.eq_row"),
    Parity("dynamic-audio", "window.dynamic_scales"),
    Parity("gestures", "window.gesture_rows"),
    Parity("gesture", "window.gesture_rows"),
    Parity("gesture --force", "advanced.force_gestures_row"),
    Parity("hold-cycle", "window.cycle_switches"),
    Parity("hold-cycle --force", "advanced.force_hold_row"),
    Parity("find", "window.find_button"),
    Parity("fit-test", "window.fit_button"),
    Parity("fit-test --timeout", "advanced.fit_timeout_row"),
    Parity("fit-test --force", "advanced.force_caps_row"),
    Parity("golden-sound", "window.golden_switch"),
    Parity("golden-sound --name", "hearing.name_entry"),
    Parity("golden-sound --skip-scan", "advanced.skip_scan_row"),
    Parity("--resolve-conflicts", "advanced.resolve_conflicts_row", "the GUI asks first by default, like the vendor app"),
    Parity("golden-sound --force", "advanced.force_caps_row"),
    Parity("watch", "window.session", "the window updates live from the buds' pushes and polling"),
    Parity("raw", "advanced.raw_send_button"),
    Parity("profile", "advanced.model_row", "list/show/validate/paths: the profile picker and the 'recognised as' row"),
    Parity("profile --out", "advanced.draft_button", "'profile detect --out' is the draft export"),
    Parity("eq save", "eqdialog.save_button", "subcommand-like action of 'eq'"),
    Parity("eq delete", "window.delete_custom_eq", "subcommand-like action of 'eq'"),
)

# CLI options with no GUI counterpart, and why. Keep this list short and honest.
NOT_APPLICABLE: dict[str, str] = {
    "golden-sound --yes": "skips the terminal confirmation prompts; the wizard confirms on screen instead",
}

# Actions of 'eq' are positional words, not options, so they are listed above by hand.
EXTRA_CLI_ITEMS = ("eq save", "eq delete")

# Controls that exist only in the GUI (they have no command-line meaning on their own).
GUI_ONLY: dict[str, str] = {
    "apply_button": "applies the device overrides; on the command line they apply to the next command",
    "profile_file_row": "shows the chosen profile file (the button is the control)",
    "trace_copy": "clipboard/file helpers for the trace; the command line prints it",
    "trace_clear": "clipboard/file helpers for the trace; the command line prints it",
    "trace_save": "clipboard/file helpers for the trace; the command line prints it",
    "raw_cmd_row": "part of the raw command form",
    "raw_payload_row": "part of the raw command form",
    "raw_result": "shows the reply of the raw command form",
    "scan_result": "shows the result of the feature scan",
    "scan_button": "",
    "draft_button": "",
}


def cli_surface(parser: argparse.ArgumentParser) -> set[str]:
    """Every public command and option of the CLI, as 'command', 'command --opt' or '--opt'."""
    out: set[str] = set()

    def options(p: argparse.ArgumentParser) -> list[str]:
        names = []
        for action in p._actions:  # noqa: SLF001
            if action.help == argparse.SUPPRESS or isinstance(action, (argparse._HelpAction, argparse._SubParsersAction)):  # noqa: SLF001
                continue
            names.extend(o for o in action.option_strings if o.startswith("--"))
        return names

    out.update(options(parser))
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))  # noqa: SLF001
    for name, p in sub.choices.items():
        if p.get_default("feature") is not None:
            continue  # a generated per-feature shortcut; covered by 'switch'
        out.add(name)
        out.update(f"{name} {o}" for o in options(p))
    out.update(EXTRA_CLI_ITEMS)
    return out
