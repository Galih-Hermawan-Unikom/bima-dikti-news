# Stable historical baselines

`bima_history_89.json` and `youtube_history_125.json` freeze the public cache
records from commit `e4ac89b`. JSON whitespace/escaping is normalized; record
values, order, duplicates and known malformed letter numbers are preserved.
No credentials, signed download URLs or delivery acknowledgements are included.
Tests must not load operational caches to assert these historical counts.
Growth tests append synthetic records to temporary copies, not production files.

`bima.html` retains the legacy DOM. `bima_ap_card.html` reproduces the public
card structure inspected on 20 October 2026, with two Aksara publication cards
and one synthetic special-character/multi-document card. No scripts, live URLs
or personal details are included. The invitation card deliberately retains
BIMA's displayed 1386 / 7 October metadata: its original PDF instead says
1385 / 6 October. This discrepancy must not be silently repaired in cache.
