# Changelog

## 1.1.0

Needs Pi Hub 8.3.1.

- Clicking the header pill opens the Flights tab.
- Links on the Flights tab open in a new browser tab when clicked. The
  ADS-B receiver tab keeps its links for dashboards with frames turned off.
- History and the tile cache live in the plugin's `data/` folder, which
  survives updates. Files from 1.0.0 are moved there on the first start.
- With the header pill switched off, nothing is shown in its place.

## 1.0.0

First release.

- Flights tab: live map with own tiles (OpenStreetMap, CARTO, or a custom
  source), aircraft coloured by altitude and rotated by track, trails, range
  rings, tooltips, pan and zoom; sortable aircraft table; charts for today,
  24 h and 7 days; daily and best records; receiver health; links with online
  status.
- Header pill with the aircraft count.
- ADS-B receiver tab with numbers, receiver health and links that open in a
  new tab, also without frames.
- Per-minute history in `history.json` with hourly roll-ups and daily
  records, kept for 1 to 90 days.
- Every setting in the Configure dialog (Pi Hub 8.3), applied without a restart.
