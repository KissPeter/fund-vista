# Traffic day slices are NOT versioned (day/ and pass/ are gitignored; they
# would bloat every clone). ~2 MB per airport-day, gzip JSON. Layout mirrors
# the SliceStore keys:
#   day/{YYYY-MM-DD}/{ICAO}.json.gz   (L1: classified flight traces)
#   pass/{YYYY-MM-DD}.json            (L0: which airports a pass covered)
# Source: adsb.lol globe_history (ODbL) — attribution rides the API metadata,
# never the artwork. The app reads the live copy from its `/data/traffic`
# volume. `ops/traffic-ingest-daily.sh` on the Mac exports a local copy here
# (ingest → export, no commit). Do not hand-edit.
