"""Bars, pubs, cafés and restaurants of a bounding box from the Overture Maps places theme
(https://docs.overturemaps.org/guides/places, CDLA-Permissive-2.0 / Apache-2.0 / CC0 depending on the source),
written as a TSV for area/extract_country.py --venues=<file>.

Usage: python3 -I overture_venues.py <release, e.g. 2026-11-18.0> <xmin,ymin,xmax,ymax> <out.tsv> [min_confidence]
"""
import re
import sys

import duckdb

BAR = ["bar", "pub", "lounge", "night_club", "nightclub", "beer_garden", "cocktail_bar", "wine_bar", "sports_bar",
       "beer_bar", "hookah_bar", "dive_bar", "karaoke_bar", "brewpub", "gastropub", "irish_pub", "whiskey_bar",
       "rooftop_bar", "piano_bar", "jazz_and_blues", "dance_club"]
CAFE = ["cafe", "coffee_shop", "tea_room", "coffee_and_tea"]
RESTO_EXCLUDED = ["fast_food_restaurant", "food_truck", "food_stand", "food_court"]
# Categories that look like food & drink but are not a place to have a drink.
CAT_EXCLUDED = {"internet_cafe", "smoothie_juice_bar", "juice_bar", "airport_lounge", "bubble_tea", "fast_food_restaurant"}
# Businesses wrongly filed as bars / restaurants (shops, schools, IT companies…).
JUNK = re.compile(r"\b(technolog\w*|enterprises?|college|academy|school|universit\w*|bales|mitumba|supermarket|market|"
                  r"ltd|limited|agency|agencies|solutions?|services?|hardware|pharmacy|chemist|salon|barber\w*|church|"
                  r"ministr\w*|motors?|electronics?|cyber|printing|stationer\w*|boutique|clinic|hospital|investments?|"
                  r"ventures?|traders?|trading|wholesale|distributors?|suppliers?|consult\w*|logistics|realtors?|properties)\b",
                  re.IGNORECASE)
SAME_POINT_MAX = 3   # more places than this on the exact same point = a default geocode (junk)


def main():
    release, bbox, out = sys.argv[1:4]
    min_conf = float(sys.argv[4]) if len(sys.argv) > 4 else 0.6
    xmin, ymin, xmax, ymax = (float(v) for v in bbox.split(","))
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
    src = f"read_parquet('s3://overturemaps-us-west-2/release/{release}/theme=places/type=place/*', hive_partitioning=1)"
    cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {src} LIMIT 0").fetchall()}
    print("columns:", sorted(cols), flush=True)
    hier = "coalesce(taxonomy.hierarchy, [])" if "taxonomy" in cols else "coalesce(categories.alternate, []) || [categories.primary]"
    prim = "taxonomy.primary" if "taxonomy" in cols else "categories.primary"
    basic = "coalesce(basic_category, '')" if "basic_category" in cols else "''"
    status = "coalesce(operating_status, '')" if "operating_status" in cols else "''"
    q = f"""
    WITH p AS (
      SELECT id, names.primary AS name, {prim} AS cat, {basic} AS basic, {hier} AS h, confidence, {status} AS status,
             addresses[1].freeform AS street, addresses[1].postcode AS postcode, addresses[1].locality AS locality,
             addresses[1].country AS country, (bbox.xmin + bbox.xmax) / 2 AS lon, (bbox.ymin + bbox.ymax) / 2 AS lat
      FROM {src}
      WHERE bbox.xmin BETWEEN {xmin} AND {xmax} AND bbox.ymin BETWEEN {ymin} AND {ymax}
    )
    SELECT *, CASE
        WHEN list_contains(h, 'pub') OR basic = 'pub' OR cat LIKE '%pub' THEN 'pub'
        WHEN list_has_any(h, {BAR}) OR basic IN ('bar', 'nightclub', 'night_club', 'lounge') OR cat LIKE '%\\_bar' ESCAPE '\\' THEN 'bar'
        WHEN list_has_any(h, {CAFE}) OR basic IN ('cafe', 'coffee_shop') THEN 'cafe'
        WHEN (list_contains(h, 'restaurant') OR basic = 'restaurant' OR cat LIKE '%restaurant')
             AND NOT list_has_any(h, {RESTO_EXCLUDED}) AND cat NOT IN {tuple(RESTO_EXCLUDED)} THEN 'restaurant'
      END AS kind
    FROM p
    """
    rows = con.execute(f"SELECT * FROM ({q}) WHERE kind IS NOT NULL AND name IS NOT NULL").fetchall()
    names = [d[0] for d in con.description]
    print(f"{len(rows)} food & drink places in the box (before confidence / status filters)", flush=True)
    i = {n: k for k, n in enumerate(names)}
    for th in (0.3, 0.5, 0.6, 0.7, 0.8, 0.9):
        print(f"  confidence >= {th}: {sum(1 for r in rows if (r[i['confidence']] or 0) >= th)}", flush=True)
    statuses = {}
    for r in rows:
        statuses[r[i["status"]]] = statuses.get(r[i["status"]], 0) + 1
    print("  operating status:", statuses, flush=True)
    cats = {}
    for r in rows:
        cats[(r[i["kind"]], r[i["cat"]])] = cats.get((r[i["kind"]], r[i["cat"]]), 0) + 1
    print("  top categories:", sorted(cats.items(), key=lambda x: -x[1])[:40], flush=True)

    def clean(v):
        return re.sub(r"\s+", " ", str(v or "")).strip()

    for r in rows:
        if re.search(r"dijo", str(r[i["name"]] or ""), re.IGNORECASE):
            print("  check:", r[i["name"]], r[i["cat"]], r[i["confidence"]], r[i["lat"]], r[i["lon"]], flush=True)
    point = {}
    for r in rows:
        k = (round(r[i["lat"]], 6), round(r[i["lon"]], 6))
        point[k] = point.get(k, 0) + 1
    crowded = {k for k, n in point.items() if n > SAME_POINT_MAX}
    print(f"  {len(crowded)} crowded points ({sum(point[k] for k in crowded)} places on them)", flush=True)
    dropped = {"confidence": 0, "closed": 0, "category": 0, "name": 0, "point": 0}
    kept = 0
    with open(out, "w", encoding="utf-8") as f:
        f.write("id\tlat\tlon\tname\tkind\tstreet\tpostcode\tlocality\tconfidence\tcategory\n")
        for r in rows:
            if (r[i["confidence"]] or 0) < min_conf:
                dropped["confidence"] += 1
                continue
            if r[i["status"]] in ("permanently_closed", "temporarily_closed", "closed"):
                dropped["closed"] += 1
                continue
            if r[i["cat"]] in CAT_EXCLUDED or set(r[i["h"]] or []) & CAT_EXCLUDED:
                dropped["category"] += 1
                continue
            name = clean(r[i["name"]])
            if len(name) < 2 or re.fullmatch(r"[\d\s+().-]+", name) or JUNK.search(name):
                dropped["name"] += 1
                continue
            if (round(r[i["lat"]], 6), round(r[i["lon"]], 6)) in crowded:
                dropped["point"] += 1
                continue
            if name.islower() or (name.isupper() and len(name) > 4):   # « dijo rooftop », « MAMA OLIECH »
                name = re.sub(r"[^\W\d_]+", lambda m: m.group(0).capitalize(), name.lower())
            f.write("\t".join([clean(r[i["id"]]), f"{r[i['lat']]:.7f}", f"{r[i['lon']]:.7f}", name, r[i["kind"]],
                               clean(r[i["street"]]), clean(r[i["postcode"]]), clean(r[i["locality"]]),
                               f"{r[i['confidence']]:.2f}", clean(r[i["cat"]])]) + "\n")
            kept += 1
    print(f"kept {kept} places (confidence >= {min_conf}); dropped {dropped}", flush=True)


if __name__ == "__main__":
    main()
