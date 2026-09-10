#!/usr/bin/env bash
# Single streaming pass over YFCC100M metadata (15 GiB gz / 44 GiB TSV).
# Emits two files:
#   hanoi_wide.tsv  - every geotagged row in a generous box around Hanoi
#   allgeo.tsv.gz   - (user, date, lon, lat) for EVERY geotagged row worldwide,
#                     needed because locals-vs-tourists depends on where else a
#                     photographer shoots, not just their Hanoi photos.
# Columns (verified): 1 id, 2 user NSID, 3 nickname, 4 date taken,
#                     5 date uploaded, 11 lon, 12 lat, 13 geo accuracy,
#                     14 page url, 15 download url, 18 server, 19 farm, 20 secret, 22 ext
set -uo pipefail
OUT=/home/baoro/stuff/random/img-city-heatmap/data/yfcc
URL=https://mmcommons.s3.amazonaws.com/yfcc100m_dataset.tgz

curl -sSL --retry 8 --retry-delay 5 --retry-all-errors "$URL" \
| tar -xzO \
| awk -F'\t' -v OFS='\t' \
      -v HAN="$OUT/hanoi_wide.tsv" \
      -v GZCMD="gzip -1 > $OUT/allgeo.tsv.gz" \
      -v STAT="$OUT/harvest.status" '
  BEGIN { n=0; ngeo=0; nhan=0 }
  {
    n++
    lon=$11; lat=$12
    if (lon != "" && lat != "" && (lon+0 != 0 || lat+0 != 0)) {
      ngeo++
      # compact worldwide history: 3dp is ~100m, ample for 50km city clustering
      printf "%s\t%s\t%.3f\t%.3f\n", $2, substr($4,1,10), lon, lat | GZCMD
      if (lon+0 >= 105.30 && lon+0 <= 106.40 && lat+0 >= 20.60 && lat+0 <= 21.50) {
        nhan++
        print $1, $2, $3, $4, lon, lat, $13, $14, $15, $18, $19, $20, $22 >> HAN
      }
    }
    if (n % 2000000 == 0) {
      printf "scanned=%d geotagged=%d hanoi_box=%d\n", n, ngeo, nhan > STAT
      close(STAT)
    }
  }
  END {
    printf "DONE scanned=%d geotagged=%d hanoi_box=%d\n", n, ngeo, nhan > STAT
    close(STAT); close(GZCMD)
  }'
echo "exit=$? " >> "$OUT/harvest.status"
