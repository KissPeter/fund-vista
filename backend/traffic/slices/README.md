# Traffic day slices live in git (HU-only scope keeps them small: ~2 MB per
# airport-day, gzip JSON). Layout mirrors the SliceStore keys:
#   day/{YYYY-MM-DD}/{ICAO}.json.gz   (L1: classified flight traces)
#   pass/{YYYY-MM-DD}.json            (L0: which airports a pass covered)
# Source: adsb.lol globe_history (ODbL) — attribution rides the API metadata,
# never the artwork. Written by `ops/traffic-ingest-daily.sh` on the Mac
# (ingest → export → commit → push); the Mac container reads the same files
# from its `/data/traffic` volume. Do not hand-edit.
