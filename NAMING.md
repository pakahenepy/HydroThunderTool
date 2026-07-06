# World-container resource naming scheme

The 12-character codes (`GGTPTBOHAL0`) are the **only names in the game data** —
the container's name field is fixed at 12 bytes and no long-name table exists.
But they're a systematic encoding Midway/Eurocom's tools used, and most of it
decodes:

```
G G T PTBOHA L0
| | | |      |
| | | |      variant / LOD suffix
| | | mnemonic (≤6 chars)
| | category
| | 
| context letter (track, or B/H/_/W)
resource type
```

## Char 0 — resource type

| letter | type |
|---|---|
| `G` | 3D model (`g` = variant/secondary model) |
| `M` | mipmapped world-surface texture |
| `T` | texture (UI/effects/water/skies) |
| `P` | boat physics parameters |
| `B` | loading screen |
| `A` | prop keyframe animation |
| `H` | track scene (world assembly) |
| `D` | demo/credits camera script |
| `I` | sound-parameter table |

## Char 1 — track / context letter

From the exe's track table (display names verbatim):

| letter | track |
|---|---|
| `A` | ARCTIC CIRCLE |
| `C` | FAR EAST |
| `D` | CATACOMB (bonus) |
| `E` | XTR2 (bonus, "HYDRO SPEEDWAY"-family) |
| `F` | XTR3 (bonus) |
| `G` | SHIP GRAVEYARD |
| `J` | LOST ISLAND |
| `M` | LOOP3 |
| `N` | NILE ADVENTURE |
| `P` | LAKE POWELL |
| `R` | GREEK ISLES |
| `V` | VENICE CANALS |
| `X` | THUNDER PARK |
| `Y` | NEW YORK DISASTER |
| `Z` | HYDRO SPEEDWAY |
| `1`/`2`/`3` | bonus-variant scenes (`H1WARCT` = Arctic bonus layout) |
| `B` | boats (not a track) |
| `H` | HUD/interface 3D elements |
| `W` | shared/world-wide |
| `_` | global (effects, glows, test assets) |

## Char 2 — category

Observed: `T` = track scenery, `X` = ambient objects/extras, `F` = effects,
`W` = world/water, `B` = boat body, `S` = ?, `T` after `B` (e.g. `TBB…`) =
boat-related textures.

## Chars 3–8 — mnemonic

Six chars, vowels dropped when needed. Seen so far: `PENG`=penguin,
`BEAR`, `HELI`=helicopter, `KAYA`=kayak, `ORCA`, `CLIF`=cliff, `SAND`,
`BLDG`=building, `TORC`=torch, `BATT`=battleship, `DESA`=destroyer,
`PTBO`=PT boat, `SPINRM`=spinning room, `COLIS`=colosseum, `WATR`/`WAT1`=water,
`EXHA`=exhaust, `HUD*`=HUD widgets, `RADA`=radar, `SPDM`=speedometer,
`CHAS`=chase camera, `PLAY`=player marker. So `GGTPTBOHAL0` =
**Model, Ship Graveyard, Track scenery, PT Boat HAtch(?), Low LOD 0**.

### Boat codes (from `P*` params `SELECT_BOAT`)

`BANS` Banshee · `CUTT` Cut Throat · `DAMN` Damn the Torpedoes ·
`HOVR` Hovercraft · `MIDW` Midway · `MISS` Miss Behave · `RADH` Rad Hazard ·
`RAZR` Razorback · `SEAD` Sea Dog · `THRE` Thresher · `TIDA` Tidal Blade ·
`TINY` Tiny Tanic · `COPB`/`CHAS`/`JUCR`/`RESC` AI chaser/police/rescue boats

## Chars 9–10/11 — variant & LOD suffix

- `H0`,`H1`,… = high-detail LOD (the exe's "Player Lod"/"High Lod"/"Drone Lod"
  tiers), `M0` = medium, `L0` = low
- `P0`,`P1`,`P2` = paint-job/variant (boat skins & physics come in `…HUP0`/`…HUP1` pairs)
- boat sub-parts: `HU`=hull, `FI`=fin, `FL`=flag, `SE`=?, `WC`=wake, `SH`=shadow,
  `BU`=?, `EN`=engine, `PR`=propeller
- pure digits = sequence frames (water animation `TATWAT100…131`, torch
  `MNTTORC_112…118`)

No cleaner names exist to recover — this table plus `manifest.csv`
(hash → original `h:\…` paths for the FSD level) is the complete picture.
