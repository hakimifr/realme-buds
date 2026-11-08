# Protocol notes: realme Buds Air7 Pro control channel

What the earbuds speak, as used by this project. Written for people who want to
extend it or check a claim.

## How this was established

Three sources, in order of trust:

1. **The app itself.** realme Link 5.8.811.2814 (versionCode 530214) was decompiled with
   jadx. The earbud protocol lives in `classes40.dex`, package
   `com.realme.iot.headset.tl.protocol` (`SetCommandManager`, `PollCommandManager`,
   `Protocol`, `TLHeadsetConnectionImp`). Names are partly obfuscated, but the setter
   method names (`setHighQualitySound`, `setControlWindNoise`, `setDynamicLowSound`, ...) and
   the numeric command ids are intact. Evidence level `app` below.
2. **A real Air7 Pro**, via traces sent back from the user's laptop. Evidence level `reported`
   in code, and the tests in `tests/test_real_capture.py`.
3. **Other people's public notes** for things the app code does not spell out:
   QuickBuds (`NazimFilzer/QuickBuds`, GPL-3.0, used as documentation only, no code copied),
   `rahul19305/realme-buds-control` (MIT, an Air7 Pro tool) and `utkarshhxd/realme-buds-control`
   (MIT, tested on an Air8). Evidence level `family`.

Every table in `realmebuds/model.py` carries its level:

| Level | Meaning |
| --- | --- |
| `app` | the id and payload are what realme Link's own code sends |
| `reported` | seen on a real Air7 Pro |
| `family` | seen on other realme/OPPO/OnePlus buds of this protocol, not confirmed on the Air7 Pro |
| `guess` | plausible only |

Anything at `family` or `guess` is labelled experimental in the CLI. Every write is read
back because an ack of `00` does not prove a write took, and the Air7 Pro does not
acknowledge some writes at all (see below).

### Confirmed in realme Link 5.8.811.2814

| Command | App constant | Payload the app sends |
| --- | --- | --- |
| `0x0403` feature switch | `1027`, `setSwitchFeature` | `[feature id, 0 or 1]` |
| `0x0404` noise control | `1028`, `SetNoiseReduction` | `[1 or 2, 1 or 2, value]` (the second form, first byte 2, sets the touch-and-hold cycle) |
| `0x0406` select EQ preset | `1030`, `setEQStatus` | `[preset id]` |
| `0x0401` touch table | `1025`, `setKeyFunction` | `[count, (side, button, action, function)*]` |
| `0x0418` custom EQ | `1048`, `setEqInfo` | `[action, min, max, id, nameLen, name, nBands, (hz u16, gain)*]` |
| `0x041B` Dynamic audio | `1051`, `setBassEngineValue` | `[min, max, value]`, plus a 4th byte (the band, 1-3) when above 0 |

Feature ids, from the setters that write them: 4 wear detect, 6 game mode, 8 auto-answer
(UI row for `headset_setting_auto_answer_phone`), 9 enhance voices (`setEnhanceVoice`),
17 dual connection, 24 high-definition sound, 26 wind noise (`setControlWindNoise`),
27 spatial audio (`set3DSoundEffect`), 29 Dynamic audio (`setDynamicLowSound`). The app has a
second "enhance voice" setter on id 12 for a different row; this project does not use it.

Not found in the app code, so still `family`: the meaning of each touch function code,
which gestures accept which functions (that comes from cloud/model config), and the
names that go with EQ preset ids 0-3.

### Real-hardware findings

The Air7 Pro does **not** acknowledge every write. Seen so far: the subscription
(`0x0205`) and the Dynamic audio write (`0x041B`) are applied without a reply. The client
therefore sends, waits briefly, and confirms by reading the value back.

## Transport

Classic Bluetooth RFCOMM. The channel number is not fixed, so look it up over SDP
(L2CAP PSM 1, one ServiceSearchAttributeRequest for attribute `0x0004`) for service
UUID `0000079a-d102-11e1-9b23-00025b00a5a5`, then connect a plain RFCOMM socket to it.
No pairing step beyond what the OS already did, no authentication, no encryption.
The channel only exists while the buds are connected to this computer.

## Frame

```
AA  <TotalLen>  00 00  <Cmd:2 LE>  <Seq:1>  <PayLen:2 LE>  <Payload>
```

- `TotalLen` is LEB128 and equals `7 + PayLen`. A 124-byte payload is `AA 83 01 ...`.
- Replies use `cmd | 0x8000` and echo `Seq`. Pushes from the buds are plain packets (command
  without the reply bit), and on the real Air7 Pro they carry ordinary incrementing seq numbers
  (`0x18`, `0x19`, ...), not `0xFF`. Never classify a push by its seq.
- The first payload byte of a reply is a status, `00` for success. `0E` means no bud
  is in an ear (noise control refuses to change).
- Big replies may be fragmented: the byte after `AA <len>` has bit 0-1 set (1 first,
  2 middle, 3 last) and a fragment counter byte follows the `00`. Implemented in
  `protocol.Framer`, covered by tests, never seen on real Air7 Pro traffic by me.

Documented example frames, all reproduced in `tests/test_protocol.py`:

```
AA 07 00 00 00 01 01 00 00                 handshake 0x0100, seq 1
AA 0A 00 00 04 04 06 03 00 01 01 01        set noise control (cmd 0x0404, seq 6) = 01 01 01
AA 0C 00 00 04 02 FF 05 00 03 01 01 08 00  push: noise control changed
```

## Connect sequence

1. `0x0100` handshake, reply is a command bitmap.
2. `0x0103` product id.
3. `0x0200` which push codes the buds offer, then `0x0205` subscribe with
   `<count> <ids>`. The count comes first. `01 01 02 02` subscribes to battery only.
   **The Air7 Pro never replies to `0x0205`**, so a missing reply is normal there.
   The real buds offered only `02 01` (wearing, battery). Noise control and game mode
   are therefore not pushed, and the app polls for them every few seconds.
4. The buds send `0x0500` (time request) with their own seq. **It must be answered**
   with `00 <unix seconds u32 LE>` or some firmwares reset the link a few seconds later.
   Other requests from the buds and the answers are in `protocol.answer_for`.

Nothing needs polling afterwards: battery, wearing, noise control and game mode are pushed.

## Captured from a real Air7 Pro

First contact, 2026-11-07, from `realme-buds --trace status` (all reproduced as tests in
`tests/test_real_capture.py`):

- Product id reply `00 12 4C 06`, i.e. product id `064C12`.
- Capability bitmap `00 BF 4F 5A E6 65 DC 40 04` (32 bits set). It enables gestures (3),
  feature switches (7), noise control (8), EQ select (10), custom EQ (34), bass engine (38),
  spatial source (43) and the multi-band bass engine (54). Not set: 56, the batch query.
  The client skips commands whose bit is clear.
- Unprompted on connect: wearing push `02 03 01 03 02 03 03 04` and battery push
  `01 03 01 50 02 64 03 00` (left 80%, right 100%, case unknown).
- The buds send `0x0500` with payload `AC 01 00 00` right after the handshake reply and
  expect `00 <unix time u32 LE>` back.

## Commands used

| Cmd | Purpose | Payload / reply | Evidence |
| --- | --- | --- | --- |
| `0x0106` | battery | `[00] <n> (<id> <raw>)*`, id 1 L, 2 R, 3 case, level `raw & 0x7F`, charging `raw & 0x80`, level 0 = unknown (case with lid closed) | reported |
| `0x0109` | wearing | pairs `<id> <status>`; 3 and 7 are in ear, 0 and 4 in case | family |
| `0x010C 01 01` | read noise mode | reply `00 01 01 <value LE>` | reported |
| `0x0404 01 01 <value>` | set noise mode | see table below | reported |
| `0x010C 02 01` / `0x0404 02 01 <mask>` | which modes touch-and-hold cycles | mask uses the same bits as below | reported |
| `0x010D <n> <ids>` | read feature switches | reply `00 <n> (<id> <val>)*`; missing ids are not listed | reported |
| `0x0403 <id> <0/1>` | write feature switch | | reported |
| `0x0108` | read touch table | `00 <n> (<side> <button> <action> <fn>)*` | reported |
| `0x0401 <n> (...)*` | write touch table | same 4-byte entries | family |
| `0x010F` / `0x0406 <id>` | read / select built-in EQ | | family |
| `0x0122` / `0x0418` | custom EQ list / write | see below | family |
| `0x012C` (or `0x0124`) / `0x041B` | Dynamic audio bands | see below | family |
| `0x041E 00` | 3D audio rendered by the buds | sent before enabling spatial audio | family |
| `0x0400 01/00` | find earbuds (rings both) | | family |

### Noise control values (set and report use the same bits on realme models)

| Value | Mode |
| --- | --- |
| `01` | Off |
| `02` | Transparency |
| `04` | Noise cancellation, mild |
| `10` | Noise cancellation, moderate |
| `08` | Noise cancellation, max |
| `20` | Noise cancellation, smart |

When smart is active the buds also push `03 04 01 <mask>` naming the level it
chose. That is the "Smart (Mild)" text in realme Link. Setting a mode while no bud
is in an ear returns status `0E`.

### Feature switch ids

| Id | Switch | Evidence |
| --- | --- | --- |
| `04` | Auto play/pause | reported |
| `06` | Game mode (also pushed as event `05 <state>`) | reported |
| `08` | Auto-answer | family |
| `09` | Enhance voices | family (id could also be `0C`, use `features --scan` to check) |
| `11` | Dual-device connection | reported |
| `18` | High-definition sound (LHDC). Changing it drops the link for a few seconds | reported |
| `1A` | Wind noise reduction | family |
| `1B` | Spatial audio | reported |
| `1D` | Dynamic audio on/off | family |
| `26` | Find my phone | family, not exposed |

### Touch table

Entries are `<side> <button> <action> <function>`. Side 1 left, 2 right, 4 both. Button 1
is the normal gestures, 6 the on-call group (never written by this project). Action 2
double, 3 triple, 4 hold, 1 single, 5 slide, 6 long hold.

| fn | Function |
| --- | --- |
| `00` none, `01` play/pause, `03` voice assistant, `05` previous, `06` next | family |
| `08` switch noise control (the cycle itself is the separate hold mask above) | reported |
| `0B` volume up, `0C` volume down, `11` game mode, `1C` decline, `1D` answer | family |
| `0D` switch device | guess |

Which functions each gesture accepts is decided by realme Link's per-model data, not
by the buds, which will answer a rejected write with a non-zero status. The table's
shape can differ between the two buds, so `Buds.set_gesture` writes every slot
matching (side, action) and re-reads the table.

### Dynamic audio

Read `0x012C` -> `00 <n> (<lo> <hi> <value> <band>)*` as signed bytes (`lo` -5, `hi` +5).
Write `0x041B <lo> <hi> <value> <band>` per band: 1 low, 2 mid, 3 high. Matches the
three sliders in realme Link.

### Custom EQ

List `0x0122` -> `00 <n>` then `<active> <gmin> <gmax> <id> <nameLen> <name> <nBands>
(<hz u16 LE> <gain s8>)*`. Write `0x0418 <action> <gmin> <gmax> <id> <nameLen> <name>
<nBands> (...)*` with action 1 create (id 0, the buds assign one), 2 save and select,
3 delete. The firmware accepts any centre frequency but limits gain to +-6 dB. The app
only offers 62/250/1k/4k/8k/16k Hz.

## Earbud fit test

All from realme Link's code (`setCompactnessDetectionAction`, `CompactnessDetectDialog`).

- Start `0x0405 01`, stop `0x0405 00`. Both buds must be in an ear.
- The result is a push `04 <side> <status> <side> <status>` (side 1 left, 2 right). The app
  dispatches any push by its first byte, so it works even when the buds did not list event 4
  among their offered events. The Air7 Pro lists only 1 and 2, so this client also sends
  `0x0201 04` (the app's single-event registration) before starting; the buds may ignore it.
- Status as the app reads it: `1` good, `0` average, `2`/`3` failed (retry), `4` a bud was
  removed, `5` interrupted by the app (ignored, keep waiting), anything else such as `6` shows as poor.

## Golden Sound (the app calls it HearId)

Everything below is read from `goldenhearid/*` and `TLHeadsetConnectionImp` in realme Link, and agrees
with what other projects captured from HeyMelody. **Not yet tried on real Air7 Pro buds.**

| Step | Frame |
| --- | --- |
| Switch on/off | `0x0403 0B 01/00` (the app's `switchHearIdFeature`) |
| Read the stored profile | `0x0115` -> `00 <type> <count> (<side> <band> <db s8>)*count <id u32 BE> <name>` |
| Ear scan | start `0x040D 04 01 <id u32 BE>`; result push `0E <action> <len u16 LE> <168 data bytes> <id>`; stop `04 00 <id>` |
| Hearing test on/off | `0x040D 02 01` / `0x040D 02 00` |
| Play a tone | `0x040E 03 01 <side> <band> <db s8>`; stop it with `0x040E 04` (sent before each new level) |
| Apply | `0x0411 01 01 01 00 0B` (restore data), `0x040E 03 <n> <n x (side band db)> <id u32 BE> <name>`, `0x0415 03 <len u16 LE> <scan data> <id>`, then `0x0403 0B 01` |

The test visits left-ear bands 1 to 6, then right-ear bands 1 to 6, starting at -25 dB. The slider has
25 stops (-120 -88 -55 -52 -49 -45 -41 -38 -35 -30 -25 -22 -19 -15 -11 -8 -5 -1 3 5 7 10 13 15 17),
and the stored value is the stop snapped to the nearest of -55 -49 -41 -35 -25 -19 -11 -5 3 7 13 17.
The app warns before levels of 10 dB and above. The buds keep only the active profile.

## Per-gesture options on the Air7 Pro

From realme Link's model data for product id `064C12` (published by QuickBuds, confirmed by an
Air7 Pro owner there): double tap accepts none, play/pause, voice assistant, next. Triple tap accepts
none, voice assistant, previous, next, game mode. Touch-and-hold accepts none, voice assistant,
noise-control cycle, game mode, and the cycle needs at least two modes. The EQ presets for this model are
ids 0 to 3 (Pure Raw, Deep Bass, Pure Voice, Pure Bass) from the same data.

## Not implemented on purpose

MindFlow and AI Translate (they depend on the phone app), firmware update, personalised ANC,
saving several Golden Sound profiles, and the on-call gesture group.
