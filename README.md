# Pi Hub Plugin: ADS-B

A dashboard for your own ADS-B receiver (readsb / tar1090) inside
[Pi Hub](https://github.com/Valli-2020/pi-hub): a live map, an aircraft
table, statistics with daily records, and shortcuts to your other receiver
pages.

## What it does

- **Flights tab** (sandboxed frame): stat tiles (aircraft now, with position,
  MLAT, messages per second, max range today, unique aircraft today), a
  slippy map with aircraft rotated by track and coloured by altitude, trails,
  range rings, the receiver position, tooltips, pan, wheel and pinch zoom,
  "center on receiver"; a sortable aircraft table (click a row to highlight
  the aircraft on the map); charts for today, 24 h and 7 days (aircraft,
  messages/s, max range); daily and all-time records (furthest, highest,
  fastest); receiver health from `stats.json` (signal, noise, gain, strong
  signals, frequency error); your links with an online status.
- **Header pill**: a plane and the current aircraft count, with the number
  of aircraft with a position on hover. It can be switched off.
- **ADS-B receiver tab** (plain Pi Hub nodes, no frame): the same numbers in
  short, receiver health and your links. These links open in a new browser
  tab; the sandboxed Flights frame cannot open windows. This tab also works
  with frames turned off (`/?noframes=1`).
- **History**: one row per minute (aircraft, with position, MLAT, messages/s,
  max range, unique aircraft) in `history.json` next to the plugin, rolled
  up to hourly rows after 48 hours and deleted after the number of days you
  set. Daily records are kept for the same time.

Pi Hub polls the receiver; your browser never talks to it directly.

## Install

1. In Pi Hub (8.3.0 or later): **Settings → Plugins**, add
   `https://github.com/Valli-2020/pi-hub-plugin-adsb`, install `adsb`.
2. Enable it and approve `ui.frame`, `ui.header`, `ui.style` and `ui.tab`.
3. **Configure**: set the data URL of your receiver. Everything else has a
   default and applies without a restart.

## Configuration

| Section | Setting | Default | Meaning |
|---|---|---|---|
| Receiver | Data URL | empty | The readsb/tar1090 `data` directory, e.g. `http://receiver.example.local/tar1090/data`. It must serve `aircraft.json`, `stats.json` and `receiver.json`. |
| Receiver | Refresh every | 5 s | 2 to 60 seconds. |
| Location | Latitude, longitude | empty | Receiver position in decimal degrees. Empty takes it from `receiver.json`. Needed for distances, range and rings. |
| Location | Range rings | `50,100,150,200` | Distances in km, up to 10. Empty hides them. |
| Map | Map tiles | OpenStreetMap | OpenStreetMap, CARTO light, CARTO dark, CARTO auto (follows the dashboard theme) or Custom. |
| Map | Custom tile URL | empty | For Custom: a template with `{z}`, `{x}` and `{y}`, e.g. `https://tiles.example.org/{z}/{x}/{y}.png`. |
| Map | Custom attribution | empty | Shown in the corner of the map for Custom tiles. |
| Map | Tile cache | 64 MB | Disk cache in the plugin folder (`tile-cache/`), least recently used tiles are removed first. 0 turns it off. |
| Map | Start zoom | 8 | 4 to 12. |
| Map | Trail length | 5 min | 0 to 30; 0 hides trails. |
| Header | Show the aircraft count | on | The header pill. |
| Header | Count only aircraft with a position | off | |
| Links | Links | none | Up to 30 rows of label, URL and "show online status". |
| Statistics | Keep history | 14 days | 1 to 90. |

## Map tiles and usage policies

Tiles are fetched by the Pi Hub server, cached on disk and handed to the
frame. Requests identify themselves as
`pi-hub-plugin-adsb/<version> (+https://github.com/Valli-2020/pi-hub-plugin-adsb)`,
at most two at a time, and a tile is re-used for 14 days.

- The **OpenStreetMap** tile servers are run by volunteers. Their
  [tile usage policy](https://operations.osmfoundation.org/policies/tiles/)
  allows light use like a personal dashboard; keep the cache on and do not
  point many dashboards at them.
- **CARTO** basemaps have their own
  [terms](https://carto.com/legal/); check that your use is covered.
- For **Custom** tiles use a source whose terms allow this, and enter its
  attribution. Only the host in your template is ever contacted.

## Privacy

- The receiver position, aircraft data and history stay on your Pi Hub. The
  plugin sends nothing anywhere except to your receiver, the tile server you
  chose, and the links you ask it to check.
- Tile requests reveal to the tile provider which area you look at, which
  roughly gives away where your receiver is. Use a self-hosted tile server
  with Custom if that matters to you.
- Anyone who can log in to Pi Hub, viewers included, sees the map and the
  receiver position.

## Development

```sh
python3 -m unittest discover -s tests            # or: pytest tests
PIHUB_CORE=<pi-hub checkout> python3 -m unittest discover -s tests   # against the real core
PIHUB_CORE=<pi-hub checkout> PLAYWRIGHT_ROOT=<folder with node_modules/playwright> \
  python3 tests/harness.py tests/browser.mjs      # the real dashboard in Chromium
```

The tests use a fake receiver with synthetic traffic, a fake tile server and
fake link targets; they never contact a real tile provider.

## Release assets

Each GitHub release ships two assets:

- `pihub-plugin.json`: the Pi Hub plugin manifest
- `pi-hub-plugin-adsb-<version>.tar.gz`: the plugin code (top-level dir
  `pi_hub_plugins/adsb/`)

Note: a store update replaces the plugin folder and keeps only
`config.json`, so `history.json` and the tile cache start fresh after an
update.

## License

MIT
