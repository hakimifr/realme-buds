# Adding another pair of earbuds

Supporting a model means writing one JSON file, a *device profile*. No Python is needed when the
buds speak the same protocol family as the Air7 Pro.

> This project is vibecoded. A profile you write is only as good as what you checked on real buds,
> so every entry carries an evidence level and the tools label the uncertain ones experimental.

## Do OPPO (and OnePlus) buds use the same protocol?

Probably, for many models, but I have not tested any. What I know and from where:

- realme Link 5.8.811.2814 contains **three** earbud implementations. Each model's cloud config
  names its family in `sdkVendor`: `VENDOR_TL`, `VENDOR_WM` or `VENDOR_LD`
  (`HeadsetManagerFactory`, found by decompiling the app).
- `VENDOR_TL` is the one this project speaks: Bluetooth Classic RFCOMM on service
  `0000079A-D102-11E1-9B23-00025B00A5A5`, frames starting with `AA`. The Air7 Pro is one of these.
- `VENDOR_WM` uses plain SPP (`00001101-…`) with a different, simpler protocol, and `VENDOR_LD` is built on
  Airoha's SDK. **Realme buds on those chips cannot be driven by this code.**
- Other people's public work (QuickBuds, OppoPodsManager) drives OPPO Enco, OnePlus and realme buds with
  this same TL-style protocol, and HeyMelody (OPPO/OnePlus's own app) speaks it. That is where my belief about
  OPPO comes from. I did not verify it from the HeyMelody app, and some newer models use Bluetooth LE instead
  of RFCOMM, which this project does not do yet.

Within the TL family the framing, command ids and the connect sequence are the same for every model.
What differs per model is data, and that is what a profile holds: the product id, what the noise-control
numbers mean (some models report a different number than the one you write), which feature ids exist,
which functions each gesture accepts, the EQ presets, whether the model has a fit test or Golden Sound.

| Family | Transport | Supported here |
| --- | --- | --- |
| `tl` | RFCOMM, service `0000079A-…` | yes |
| `wm` | RFCOMM, `00001101-…` (SPP) | no |
| `ld` | Airoha SDK | no |
| BLE-only OPPO/OnePlus models | GATT | no |

## Quick path

1. Connect the buds to the computer, then ask them what they report:

   ```sh
   realme-buds profile detect --out my-buds.json
   ```

   It says which profile it recognised (probably none, so the cautious `generic` one) and writes a draft
   from what the buds report: product id, the feature switches they have, the gesture slots, the Dynamic
   audio bands, and which commands the buds claim to support.

2. Fill in what it cannot know (below), keeping the evidence honest.

3. Check and try it:

   ```sh
   realme-buds profile validate my-buds.json
   realme-buds --profile-file my-buds.json status
   realme-buds-gui --profile-file my-buds.json
   ```

4. Put it where it is picked up automatically (`realme-buds profile paths` prints the folder):

   ```sh
   mkdir -p ~/.config/realme-buds/profiles && cp my-buds.json ~/.config/realme-buds/profiles/
   ```

   Buds are recognised by product id first, then by exact Bluetooth name. A profile with the same
   `key` as a built-in one replaces it, so you can also correct a built-in profile.

Until a profile exists, unknown buds still connect with the `generic` profile. It shows battery, wearing
state, firmware and the switches the buds report under placeholder names, and it claims nothing else (no
noise-control table, because a wrong one can switch noise cancelling *on* when asked to turn it off).
`--profile KEY` (or *Advanced → Device profile* in the GUI) tries a sibling model's profile on unknown buds.

## Finding the values

**Noise control** (`anc.modes`). Each mode has `set` (what you write) and `report` (what the buds say is
active). They are the same on realme models and differ on some others. Put the buds in a mode with the
vendor's app, then read the current value with `realme-buds raw 010c 0101`; the reply is
`00 01 01 <value, little endian>`. That is `report`. Write a candidate with `realme-buds raw 0404 0101<value>`
and read it back to confirm. Do not guess: some models turn noise cancelling on when you write their "Off" bytes.
`groups` collect levels under one button (for example Smart/Max/Moderate/Mild under "Noise cancellation").

**Feature switches** (`features`). `realme-buds features --scan` lists every id the buds answer to. Toggle one
setting in the vendor app and scan again: the id that flipped is that setting. Give it a `key`, a `label` and the
`group` it appears in (`noise`, `sound`, `mode`, `personal`). `drops_link: true` if changing it disconnects the buds.

**Gestures** (`gestures`). `realme-buds gestures` shows the table the buds hold. List the `actions` you may remap
(1 single, 2 double, 3 triple, 4 hold, 5 slide, 6 long hold) and, if you know them, the function codes each accepts in `options`.
Function codes are shared across the family; `realme-buds gesture --help` lists their names.

**Touch-and-hold cycle** (`anc.hold_cycle`). Each option is one bit of the mask written with `hold-cycle`. `min` is how
many modes the vendor app insists on.

**Equaliser** (`equalizer`). `presets` are the built-in presets by id. `custom` describes the user-made presets
(band frequencies and the gain limit); leave it out if the model has none.

**Capabilities.** `fit_test` and `golden_sound` show those sections. The buds' own capability list (`realme-buds --trace status` shows it) tells you whether the commands exist.

## Profile reference (schema 1)

```json
{
  "schema": 1,
  "key": "example-buds",
  "name": "Example Buds",
  "vendor": "example",
  "family": "tl",
  "match": { "product_ids": ["0A0B0C"], "names": ["Example Buds"] },
  "notes": "Anything a maintainer should know. Say what you verified and on which firmware.",
  "anc": {
    "modes": [
      { "key": "off", "label": "Off", "set": 1, "report": 8, "evidence": "reported" },
      { "key": "transparency", "label": "Transparency", "set": 4, "report": 256 },
      { "key": "anc", "label": "Noise cancellation", "set": 2, "report": 2 }
    ],
    "groups": [],
    "buttons": ["off", "transparency", "anc"],
    "hold_cycle": { "options": [ { "key": "off", "label": "Off", "bit": 1 }, { "key": "anc", "label": "Noise cancellation", "bit": 2 } ], "min": 2 }
  },
  "features": [
    { "key": "game-mode", "id": 6, "label": "Game mode", "note": "Low latency", "evidence": "app", "group": "mode" },
    { "key": "lhdc", "id": 24, "label": "High-definition sound", "evidence": "reported", "group": "sound", "drops_link": true }
  ],
  "gestures": { "sides": [1, 2], "actions": [2, 4], "options": { "2": [0, 1, 6], "4": [0, 8] } },
  "equalizer": {
    "presets": [ { "id": 0, "label": "Balanced" }, { "id": 1, "label": "Bass" } ],
    "custom": { "freqs": [62, 250, 1000, 4000, 8000, 16000], "gain": [-6, 6], "max": 3 }
  },
  "dynamic_audio": { "bands": { "1": "Low", "2": "Mid", "3": "High" }, "range": [-5, 5] },
  "capabilities": { "fit_test": false, "golden_sound": false }
}
```

| Field | Notes |
| --- | --- |
| `key` | lowercase letters, digits and dashes; unique |
| `match.product_ids` | six hex digits, from `status` ("product id"). Preferred way to be recognised |
| `match.names` | exact Bluetooth names, a fallback |
| `family` | only `tl` is supported by this build |
| `anc.modes[].evidence` | `app`, `reported`, `family` or `guess`; default `family` |
| `anc.buttons` | order of the top-level buttons; each is a mode key or a group key |
| `features[].key` | `golden-sound` and `dynamic-audio` are reserved for the dedicated screens |
| `gestures.options` | keys are action numbers listed in `actions`; values are function codes |

Evidence levels: **app** the vendor's own app sends exactly this; **reported** seen on real buds of this model;
**family** seen on other models of the family; **guess** plausible only. Anything below `reported` is shown as experimental.

## Sharing it

A profile is plain data, so it is easy to review. Say in `notes` what you checked and with which firmware. If you
confirmed something against a vendor app, say which version. Do not copy another project's data files into a profile
without checking their license.

## Adding a whole new protocol family (developers)

A profile only covers a family the library can already speak. For another family you would add:

1. A transport (RFCOMM channel lookup for SPP, or GATT for BLE) next to `transport.py`.
2. A framing and command layer like `protocol.py`/`parsing.py`/`client.py`, exposing the same high-level `Buds` methods
   that the CLI and GUI call (`set_anc_mode`, `set_feature`, `read_battery`, …).
3. The family's name in `profiles.schema.KNOWN_FAMILIES`, and a dispatch in `session.connect_buds`.

The CLI, the GUI and the simulator are written against the `Buds` methods and the profile, not against the wire
format, so they should not need to change.
