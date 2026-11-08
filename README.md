> [!CAUTION]
> # ⚠️ THIS PROJECT IS VIBECODED ⚠️
>
> **Almost all of this code was written by an AI coding agent from prompts, not written
> by hand or reviewed line by line by a human.** It has not been audited. Expect bugs,
> wrong assumptions and rough edges.
>
> - It talks to your earbuds over Bluetooth and **writes settings to them**. It can get
>   things wrong, and it has only been tried on a single pair of Air7 Pro.
> - The protocol was checked against the decompiled official app and a real pair of buds,
>   but a few details (touch function codes, EQ preset names) are still guesses from other
>   people's public notes. Those are labelled experimental in the tool.
> - **Use it at your own risk.** There is no warranty, and nothing here is endorsed by realme, OPPO or OnePlus.
>
> If you rely on it, read the code first.

# realme-buds

Control earbuds from your laptop: noise control, spatial audio, game mode, EQ, Dynamic
audio, touch controls and more. No phone, no vendor app, no account. A command line tool
and a GTK4 app, both on top of the same small library.

Built in: **realme Buds Air7 Pro**. Other OPPO/OnePlus/realme-family buds are added with a
small JSON file, not code, see [Other earbuds](#other-earbuds).

> **Read this first.** The code was written on a server with no Bluetooth and tested
> against a simulator, then fixed against traces from a real Air7 Pro and checked against
> the decompiled realme Link 5.8.811.2814. Most commands have still not been tried on real
> buds. The checklist below is how to find out.

## What works

| Feature | CLI | GUI | Confidence |
| --- | --- | --- | --- |
| Battery, wearing state, firmware | yes | yes | reported by other Air7 Pro owners |
| Noise control (off / transparency / ANC levels) | `anc` | yes | reported |
| Touch-and-hold noise cycle | `hold-cycle` | yes | reported |
| Game mode, auto play/pause, dual-device, spatial audio, LHDC | yes | yes | reported |
| Wind noise reduction, enhance voices, auto-answer | yes | yes | ids and payloads confirmed in the app's code, not yet tried on real buds |
| EQ built-in presets | `eq set` | yes | family-level, experimental |
| Custom EQ (6 bands, +-6 dB): create, edit, delete, select | `eq save`, `eq delete`, `eq set` | yes | payload confirmed in the app's code, untested on real buds |
| Dynamic audio (3 bands) | `dynamic-audio` | yes | confirmed in the app's code and working on a real Air7 Pro |
| Touch remapping (double / triple / hold, per bud) | `gesture` | yes | partly reported |
| Earbud fit test | `fit-test` | yes | payloads and result format confirmed in the app's code, untested on real buds |
| Golden Sound (ear scan, hearing test, profile on/off) | `golden-sound` | yes | whole flow confirmed in the app's code, untested on real buds |
| Find my earbuds | `find` | yes | family-level |

Left out on purpose: MindFlow, AI Translate, firmware updates, and managing several saved Golden Sound
profiles (the buds only keep the active one, so this tool makes a new one each time).
"Family-level" means it was seen on other realme/OPPO buds with the same protocol but
not confirmed on an Air7 Pro. Those commands print an "experimental" note. Every write
is read back from the buds, and a write that did not take is reported as a failure.

See [docs/PROTOCOL.md](docs/PROTOCOL.md) for the wire format and where each fact came from.

### CLI and GUI do the same things

A rule of this project: anything the command line can do, bypass or force, the GUI can too.
The command line's `--force` flags, `--skip-scan`, `--timeout`, `--profile`, `--profile-file`,
`--addr`, `--channel`, `--trace`, `devices`, `features --scan`, `raw`, `status --json` and
`profile detect` all have a control in the GUI's **Advanced** dialog (the gear in the header bar).
The mapping is in `realmebuds/parity.py`, and a test fails if the command line gains an
option that has no GUI counterpart.

## Other earbuds

Everything that differs between models is data in a *device profile*: the product id, what the
noise-control numbers mean, which feature switches exist, which functions each gesture accepts,
the EQ presets. Profiles are JSON files. Buds with no profile still connect with a cautious
generic one (battery, wearing, firmware, the switches they report, nothing guessed).

```sh
realme-buds profile list                     # known profiles and where they came from
realme-buds profile detect --out mine.json   # draft a profile from the buds connected now
realme-buds --profile-file mine.json status  # try it
realme-buds --profile realme-buds-air7-pro status   # force a profile on buds it did not recognise
```

**Do OPPO buds use the same protocol?** Probably for many models, but this project has not been
tried on any. realme Link contains three earbud protocols (`VENDOR_TL`, `VENDOR_WM`, `VENDOR_LD`) and this
code speaks `TL`, which other projects document on OPPO Enco, OnePlus and realme buds. Realme buds on the
other two chips, and BLE-only models, are out of reach for now. Details and the full how-to are in
[docs/ADDING_A_DEVICE.md](docs/ADDING_A_DEVICE.md).

## Setup on Fedora

No Python packages to install, only system ones that a GNOME install mostly has:

```sh
sudo dnf install bluez python3-gobject gtk4 libadwaita
```

Pair the buds once in your normal Bluetooth settings and **connect them to the laptop**.
The control channel only exists while they are connected to it. To keep them on your
phone at the same time, turn on *Dual-device connection* in realme Link once.

Run straight from the checkout:

```sh
python3 -m realmebuds status          # command line
python3 -m realmebuds.gui             # GTK app
```

Or install the two commands (`realme-buds`, `realme-buds-gui`) for your user:

```sh
pipx install --system-site-packages .
install -Dm644 data/realme-buds.desktop ~/.local/share/applications/realme-buds.desktop
```

It needs no root and no daemon. It talks to the buds with ordinary Bluetooth sockets.

## Command line

```sh
realme-buds status                       # everything the buds report (--json too)
realme-buds anc smart                    # the modes depend on the model; errors list the valid ones
realme-buds spatial on
realme-buds game-mode on                 # also: wear-detect, auto-answer, enhance-voices,
                                         #       dual, lhdc, wind
realme-buds eq set deep-bass             # pure-raw | deep-bass | pure-voice | pure-bass
realme-buds dynamic-audio 2 0 -1         # three numbers = low, mid, high (-5..5)
realme-buds dynamic-audio low +2         # or just the bands you name; also: on | off
realme-buds gestures                     # show the touch table
realme-buds gesture left double next     # side, gesture, function (add --force to bypass the model's list)
realme-buds hold-cycle off,transparency,anc
realme-buds fit-test                     # seal check, both buds in
realme-buds golden-sound                 # status; also: on | off | test (guided hearing test)
realme-buds watch                        # live changes, including from the buds' own touch controls
realme-buds devices                      # what BlueZ knows about
realme-buds switch 0x1B on               # any feature switch by id, for models without a full profile
realme-buds raw 0106                     # send a raw command and print the reply (for exploring)
realme-buds profile list                 # device profiles; also: show, validate, detect, paths
```

Useful when something misbehaves: `--trace` prints every frame to stderr, `--addr`
picks a pair explicitly, `--channel N` skips the SDP lookup, `--profile KEY` /
`--profile-file FILE` choose the device profile instead of recognising the buds.

## First run checklist on real buds

In this order, stopping at the first thing that fails:

1. `realme-buds devices` shows your buds as `connected` and with `realme control service`.
2. `realme-buds --trace status`. Expect firmware, battery, noise control and your
   feature switches. If it hangs, send me the `--trace` output.
3. `realme-buds anc off`, then `anc transparency`. Put a bud in your ear first, the
   buds refuse otherwise. Compare with what you hear.
4. Toggle a setting in realme Link and watch the laptop catch it with
   `realme-buds watch`, then do the reverse from the laptop.
5. `realme-buds features --scan` lists every switch id the buds answer to. Toggle
   *Enhance voices*, *Auto-answer* or *Wind noise reduction* in realme Link and scan again
   to see which id flipped. If an id differs from the profile (`realmebuds/profiles/builtin/`), that's a one-line fix.
6. Try the experimental ones one by one: `eq set`, `dynamic-audio`, `gesture`.
   Check `realme-buds gestures` afterwards, and note that realme Link overwrites EQ and
   Dynamic audio with its own values if you open those screens.

If a command fails, rerun it with `--trace` and keep the output.

## Cautions

- Changing **LHDC** makes the buds drop and reconnect for a few seconds. The GUI
  reconnects by itself.
- `find` rings both buds loudly. Take them out first.
- Spatial audio and LHDC are separate switches here. The official app treats them as
  exclusive on some models, so if one refuses to turn on, check the other.
- Settings are stored on the buds, so they stick for your phone too.
- **Golden Sound** tones play inside your ears. The test starts quiet and asks before every
  loud step, but keep it sensible. `realme-buds golden-sound off` turns the profile off again.

## Development

```sh
python3 -m pytest -q tests                      # protocol, client, CLI, session (no hardware)
xvfb-run -a python3 -m pytest -q tests/test_gui.py   # GUI against the simulator
python3 -m realmebuds.gui --sim                 # try the interface with pretend buds
python3 -m realmebuds --sim status
```

Layout:

| Part | What it is |
| --- | --- |
| `protocol.py`, `parsing.py` | framing, replies the buds expect, payload decoders for the `tl` family |
| `model.py` | what function codes and status bytes mean in the family (not per-model) |
| `profiles/` | device profiles: schema, loader, `builtin/*.json`, generic fallback, draft builder |
| `sdp.py`, `transport.py` | finding the RFCOMM channel and opening it (BlueZ) |
| `client.py`, `session.py` | the threaded client with read-back verified writes; an auto-reconnecting session |
| `sim.py` | pretend buds, can be any profile, used by the tests |
| `cli.py` | the command line |
| `gui.py`, `gui_advanced.py`, `gui_dialogs.py`, `gui_eq.py` | the GTK app and its dialogs |
| `parity.py` | the CLI-to-GUI mapping that tests enforce |

## Credits

The protocol was worked out by other people before this project, see the list in
[docs/PROTOCOL.md](docs/PROTOCOL.md). This is an unofficial tool, not affiliated with
realme, OPPO or OnePlus.
