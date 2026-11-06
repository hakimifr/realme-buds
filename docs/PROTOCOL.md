# Protocol notes: realme Buds Air7 Pro control channel

What the earbuds speak, as used by this project. Written for people who want to
extend it or check a claim.

## How this was established, honestly

I could not decompile realme Link for this: APKMirror gates the file behind a
browser session and Cloudflare, and no other mirror gave a usable direct file from the
machine this was developed on. So nothing here comes from reading the app's code.

It comes from three public projects that captured and documented the same protocol
family, cross-checked against each other and against your screenshots of realme Link
5.8.811.2814 on an Air7 Pro:

- QuickBuds (`NazimFilzer/QuickBuds`, GPL-3.0), whose `docs/PROTOCOL.md` is the most
  complete map, including Air7 Pro and realme Link notes. Used as documentation only,
  no code copied.
- `rahul19305/realme-buds-control` (MIT), an Air7 Pro specific Windows tool.
- `utkarshhxd/realme-buds-control` (MIT), tested on an Air8, source of the
  three-band Dynamic audio and custom-EQ layouts.

Every table in `realmebuds/model.py` carries an evidence level:

| Level | Meaning |
| --- | --- |
| `reported` | seen by someone with an Air7 Pro |
| `family` | seen on other realme/OPPO/OnePlus buds of this protocol, not confirmed on the Air7 Pro |
| `guess` | plausible only |

Anything not `reported` is labelled experimental in the CLI. Every write is read back
because an ack of `00` does not prove a write took.

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
- Replies use `cmd | 0x8000` and echo `Seq`. Pushes from the buds use `Seq = 0xFF`.
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
4. The buds send `0x0500` (time request) with their own seq. **It must be answered**
   with `00 <unix seconds u32 LE>` or some firmwares reset the link a few seconds later.
   Other requests from the buds and the answers are in `protocol.answer_for`.

Nothing needs polling afterwards: battery, wearing, noise control and game mode are pushed.

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

## Not implemented on purpose

MindFlow and AI Translate (they depend on the phone app), Golden Sound hearing profile,
fit test, firmware update, personalised ANC, and the on-call gesture group.
