"""The CLI side of the rule "anything the command line can do, the GUI can too".

The GUI side (the listed controls exist) is checked in test_gui.py, which owns the GTK app."""

from realmebuds import cli, parity


def test_every_cli_command_and_option_has_a_gui_counterpart_or_a_reason():
    surface = parity.cli_surface(cli.build_parser())
    listed = {p.cli for p in parity.PARITY} | set(parity.NOT_APPLICABLE)
    missing = sorted(surface - listed)
    assert not missing, (
        "These command-line items have no entry in realmebuds/parity.py. Add the GUI control that "
        f"reproduces each one (and the entry), or explain in NOT_APPLICABLE why it cannot exist: {missing}"
    )


def test_the_table_has_no_stale_entries():
    surface = parity.cli_surface(cli.build_parser())
    stale = sorted(({p.cli for p in parity.PARITY} | set(parity.NOT_APPLICABLE)) - surface)
    assert not stale, f"entries in realmebuds/parity.py for CLI items that no longer exist: {stale}"


def test_not_applicable_is_kept_short_and_explained():
    assert len(parity.NOT_APPLICABLE) <= 3
    assert all(reason.strip() for reason in parity.NOT_APPLICABLE.values())


def test_every_bypass_flag_is_in_the_table():
    surface = parity.cli_surface(cli.build_parser())
    forces = {item for item in surface if item.endswith("--force")}
    assert forces == {"gesture --force", "hold-cycle --force", "fit-test --force", "golden-sound --force"}
    gui_for = {p.cli: p.gui for p in parity.PARITY}
    assert all(gui_for[f].startswith("advanced.") for f in forces)  # each bypass is an override in Advanced
