# realme-buds

Control realme Buds Air7 Pro from your laptop: noise control, spatial audio, game
mode, EQ, Dynamic audio, touch controls and more. No phone, no realme Link, no
account. A command line tool and a GTK4 app, both on top of the same small library.

> **Read this first.** The code was written on a server with no Bluetooth and tested
> against a simulator built from public protocol notes. The first connection to a real
> Air7 Pro (captured 2026-11-07) fixed one bug and is now part of the tests, but most
> commands have still not been tried on real buds. The checklist below is how to find out.

## What works

| Feature | CLI | GUI | Confidence |
| --- | --- | --- | --- |
| Battery, wearing state, firmware | yes | yes | reported by other Air7 Pro owners |
| Noise control (off / transparency / ANC levels) | `anc` | yes | reported |
| Touch-and-hold noise cycle | `hold-cycle` | yes | reported |
| Game mode, auto play/pause, dual-device, spatial audio, LHDC | yes | yes | reported |
| Wind noise reduction, enhance voices, auto-answer | yes | yes | family-level, experimental |
| EQ built-in presets | `eq set` | yes | family-level, experimental |
| Custom EQ (6 bands, +-6 dB) | `eq save` | no | family-level, experimental |
| Dynamic audio (3 bands) | `dynamic-audio` | yes | family-level, experimental |
| Touch remapping (double / triple / hold, per bud) | `gesture` | yes | partly reported |
| Find my earbuds | `find` | yes | family-level |

Left out on purpose: MindFlow, AI Translate, Golden Sound, fit test, firmware updates.
"Family-level" means it was seen on other realme/OPPO buds with the same protocol but
not confirmed on an Air7 Pro. Those commands print an "experimental" note. Every write
is read back from the buds, and a write that did not take is reported as a failure.

See [docs/PROTOCOL.md](docs/PROTOCOL.md) for the wire format and where each fact came from.

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
realme-buds anc smart                    # off | transparency | smart | max | moderate | mild
realme-buds spatial on
realme-buds game-mode on                 # also: wear-detect, auto-answer, enhance-voices,
                                         #       dual, lhdc, wind
realme-buds eq set deep-bass             # pure-raw | deep-bass | pure-voice | pure-bass
realme-buds dynamic-audio 2 0 -1         # low mid high, -5..5   (or: on | off)
realme-buds gestures                     # show the touch table
realme-buds gesture left double next     # side, double|triple|hold, function
realme-buds hold-cycle off,transparency,anc
realme-buds watch                        # live changes, including from the buds' own touch controls
realme-buds devices                      # what BlueZ knows about
```

Useful when something misbehaves: `--trace` prints every frame to stderr, `--addr`
picks a pair explicitly, `--channel N` skips the SDP lookup.

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
   to see which id flipped. If an id differs from `model.py`, that's a one-line fix.
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

## Development

```sh
python3 -m pytest -q tests                      # protocol, client, CLI, session (no hardware)
xvfb-run -a python3 -m pytest -q tests/test_gui.py   # GUI against the simulator
python3 -m realmebuds.gui --sim                 # try the interface with pretend buds
python3 -m realmebuds --sim status
```

Layout: `protocol.py` framing and answers to the buds, `parsing.py` payload decoders,
`model.py` what the numbers mean, `sdp.py` and `transport.py` Bluetooth plumbing,
`client.py` the threaded client, `session.py` auto-reconnecting connection for the
GUI, `sim.py` the pretend buds, `cli.py` and `gui.py` the front ends.

## Credits

The protocol was worked out by other people before this project, see the list in
[docs/PROTOCOL.md](docs/PROTOCOL.md). This is an unofficial tool, not affiliated with
realme.
