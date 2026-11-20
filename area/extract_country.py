"""Build Overpass-compatible files (<code>_poi.csv, <code>_addr.csv, <code>_roads.json) for a whole COUNTRY
from an OSM extract, in the same format as scripts/extract.py, so the osm_import pipeline of the Six-seven
database can process them (one pseudo-commune row per town, followed by the venues of that town).

The country is the admin_level=2 boundary whose ISO3166-1 tag equals <iso2>; only venues inside it are kept
(Geofabrik extracts overlap their neighbours, e.g. Kinshasa / Brazzaville across the river).

Town of each venue, in this order:
  1. inside the administrative area of a city (an admin boundary containing a place=city node of the same
     name, e.g. "Nairobi County", "Kinshasa", "Brazzaville"; at most 2,500 km² except for the capital,
     so that a county named after its main town does not swallow the other towns): that city;
  2. its addr:city tag, when it names a nearby city / town / village;
  3. the best nearby place: cities count within 15 km, towns within 7 km, villages within 3 km, the
     distance weighted by importance (city 4, town 2, village 1);
  4. otherwise the nearest city / town / village, whatever the distance.

Usage: python3 -I extract_country.py <input.osm.pbf> <out_dir> <code> <iso2>
"""
import json
import math
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict

import osmium
import shapely
from shapely.geometry import MultiPolygon, Point, Polygon

AMENITIES = {"bar", "pub", "cafe", "restaurant", "biergarten", "nightclub"}
AMENITY_AS = {"hookah_lounge": "cafe"}          # shisha lounges: listed as cafés
LEISURE_AS = {"beach_resort": "bar"}            # beach clubs: listed as bars
ROAD_EXCLUDED = {"motorway", "motorway_link", "trunk_link", "proposed", "construction", "platform", "steps",
                 "corridor", "elevator", "bus_stop"}
PLACE_W = {"city": 4.0, "town": 2.0, "village": 1.0}
PLACE_R = {"city": 15000.0, "town": 7000.0, "village": 3000.0}
CELL = 200.0          # metres, venue grid (addresses / roads)
PCELL = 5000.0        # metres, place grid
METRO_MAX_KM2 = 2500.0
ADDR_R = 40.0
ROAD_R = 100.0
NOISE = {"county", "city", "ville", "de", "du", "la", "province", "district", "department", "departement",
         "commune", "urbaine", "sub", "municipality", "municipal", "council", "of", "the", "territoire", "region"}


def clean(v):
    return (v or "").replace("\t", " ").replace("\n", " ").replace("\r", " ").strip()


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    words = [w for w in re.split(r"[^a-z0-9]+", s) if w and w not in NOISE]
    return " ".join(words)


def bbox_center(locs):
    lats = [l[0] for l in locs]
    lons = [l[1] for l in locs]
    return (min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2


def way_locs(w):
    out = []
    for n in w.nodes:
        try:
            if n.location.valid():
                out.append((n.location.lat, n.location.lon))
        except osmium.InvalidLocationError:
            pass
    return out


def area_polygon(a):
    polys = []
    for outer in a.outer_rings():
        shell = [(n.lon, n.lat) for n in outer]
        holes = [[(n.lon, n.lat) for n in inner] for inner in a.inner_rings(outer)]
        if len(shell) >= 4:
            polys.append(Polygon(shell, [h for h in holes if len(h) >= 4]))
    if not polys:
        return None
    g = polys[0] if len(polys) == 1 else MultiPolygon(polys)
    return g if g.is_valid else shapely.make_valid(g)


def main():
    src, out_dir, code, iso = sys.argv[1:5]
    os.makedirs(out_dir, exist_ok=True)

    def kind_of(t):
        a = t.get("amenity")
        if a in AMENITIES:
            return a
        if a in AMENITY_AS:
            return AMENITY_AS[a]
        return LEISURE_AS.get(t.get("leisure"))

    # ---- Pass A: venues, places, country and admin boundaries --------------------------------
    venues, places, admins = {}, [], []
    country, country_id = None, 0
    fp = (osmium.FileProcessor(src).with_locations("flex_mem")
          .with_areas(osmium.filter.KeyFilter("boundary", "amenity", "leisure"))
          .with_filter(osmium.filter.KeyFilter("amenity", "boundary", "leisure", "place")))
    for o in fp:
        t = o.tags
        if o.is_area():
            if t.get("boundary") == "administrative" and t.get("name"):
                lvl = t.get("admin_level", "")
                if lvl == "2" and not o.from_way() and iso in (t.get("ISO3166-1"), t.get("ISO3166-1:alpha2")):
                    g = area_polygon(o)
                    if g is not None and not g.is_empty:
                        country, country_id = g, o.orig_id()
                elif lvl in ("4", "5", "6", "7", "8"):
                    g = area_polygon(o)
                    if g is not None and not g.is_empty:
                        admins.append((int(lvl), t.get("name"), g))
                continue
            if o.from_way():
                continue
            if kind_of(t) and t.get("name"):
                key = f"relation/{o.orig_id()}"
                locs = [(n.lat, n.lon) for ring in o.outer_rings() for n in ring]
                if key not in venues and locs:
                    lat, lon = bbox_center(locs)
                    venues[key] = {"type": "relation", "id": o.orig_id(), "lat": lat, "lon": lon, "tags": dict(t)}
            continue
        if o.is_node() and t.get("place") in PLACE_W and t.get("name"):
            places.append({"name": clean(t.get("name")), "type": t.get("place"), "lat": o.location.lat, "lon": o.location.lon,
                           "capital": t.get("capital") in ("yes", "2")})
        if not kind_of(t) or not t.get("name"):
            continue
        if o.is_node():
            venues[f"node/{o.id}"] = {"type": "node", "id": o.id, "lat": o.location.lat, "lon": o.location.lon, "tags": dict(t)}
        elif o.is_way():
            locs = way_locs(o)
            if locs:
                lat, lon = bbox_center(locs)
                venues[f"way/{o.id}"] = {"type": "way", "id": o.id, "lat": lat, "lon": lon, "tags": dict(t)}
    print(f"pass A: {len(venues)} venues in the file, {len(places)} places, {len(admins)} admin areas, "
          f"country {iso} found: {country is not None}", flush=True)
    if country is None:
        sys.exit(f"country {iso} not found")

    shapely.prepare(country)
    kept = [v for v in venues.values() if country.contains(Point(v["lon"], v["lat"]))]
    places = [p for p in places if country.contains(Point(p["lon"], p["lat"]))]
    print(f"venues inside {iso}: {len(kept)}; places inside: {len(places)}", flush=True)
    if not kept:
        sys.exit("no venue inside the country")

    # ---- Metropolitan areas: an admin area containing a place=city node of the same name ----------
    metros = []
    for p in places:
        if p["type"] != "city":
            continue
        pt, pn = Point(p["lon"], p["lat"]), norm(p["name"])
        best = None
        for lvl, name, g in admins:
            an = norm(name)
            if not pn or not an or not (an == pn or an.startswith(pn + " ") or pn.startswith(an + " ")):
                continue
            if g.contains(pt) and (best is None or g.area < best.area):
                best = g
        if best is None:
            continue
        km2 = best.area * 111.32 * math.cos(math.radians(p["lat"])) * 110.57
        # A big area named after its main town (a Kenyan county, a DR Congo province) also covers other
        # towns: only the capital's area may be big.
        if km2 > METRO_MAX_KM2 and not p["capital"]:
            print(f"  not a metropolitan area: {p['name']} ({km2:.0f} km²)", flush=True)
            continue
        shapely.prepare(best)
        metros.append((best.area, p["name"], best))
    metros.sort(key=lambda m: m[0])   # smallest first
    print(f"metropolitan areas: {len(metros)} " + ", ".join(m[1] for m in metros[:30]), flush=True)

    # ---- Projection and place grid -----------------------------------------------------------
    lat0 = sum(v["lat"] for v in kept) / len(kept)
    K = 111320 * math.cos(math.radians(lat0))
    Y = 111132.954 - 559.822 * math.cos(math.radians(2 * lat0)) + 1.175 * math.cos(math.radians(4 * lat0))
    pgrid = defaultdict(list)
    for p in places:
        p["x"], p["y"] = p["lon"] * K, p["lat"] * Y
        pgrid[(int(p["x"] // PCELL), int(p["y"] // PCELL))].append(p)
    by_name = defaultdict(list)
    for p in places:
        by_name[norm(p["name"])].append(p)

    def ring(cx, cy, k):
        if k == 0:
            yield cx, cy
            return
        for i in range(cx - k, cx + k + 1):
            yield i, cy - k
            yield i, cy + k
        for j in range(cy - k + 1, cy + k):
            yield cx - k, j
            yield cx + k, j

    def town_of(v):
        x, y = v["lon"] * K, v["lat"] * Y
        pt = Point(v["lon"], v["lat"])
        for _, name, g in metros:
            if g.contains(pt):
                return name, "metro"
        ac = norm(v["tags"].get("addr:city", ""))
        if ac and ac in by_name:
            cand = min(by_name[ac], key=lambda p: (p["x"] - x) ** 2 + (p["y"] - y) ** 2)
            if math.hypot(cand["x"] - x, cand["y"] - y) <= 30000:
                return cand["name"], "addr_city"
        cx, cy = int(x // PCELL), int(y // PCELL)
        best, score = None, None
        reach = int(PLACE_R["city"] // PCELL) + 1
        for k in range(reach + 1):
            for c in ring(cx, cy, k):
                for p in pgrid.get(c, ()):
                    d = math.hypot(p["x"] - x, p["y"] - y)
                    if d <= PLACE_R[p["type"]]:
                        s = d / PLACE_W[p["type"]]
                        if score is None or s < score:
                            best, score = p, s
        if best is not None:
            return best["name"], "near"
        for k in range(reach + 1, 400):
            found = [p for c in ring(cx, cy, k) for p in pgrid.get(c, ())]
            if found:
                p = min(found, key=lambda p: math.hypot(p["x"] - x, p["y"] - y))
                return p["name"], "nearest"
        return None, "none"

    towns, how = defaultdict(list), Counter()
    for v in kept:
        name, src_ = town_of(v)
        how[src_] += 1
        if name:
            towns[name].append(v)
    by_kind = Counter(kind_of(v["tags"]) for v in kept)
    print(f"kinds: {dict(by_kind)}", flush=True)
    print(f"town source: {dict(how)}; {len(towns)} towns", flush=True)
    print("biggest towns: " + ", ".join(f"{n} {len(vs)}" for n, vs in sorted(towns.items(), key=lambda x: -len(x[1]))[:25]), flush=True)

    # ---- Pass B: addresses + named roads near the venues -------------------------------------
    grid = defaultdict(list)
    for v in kept:
        vx, vy = v["lon"] * K, v["lat"] * Y
        grid[(int(vx // CELL), int(vy // CELL))].append((vx, vy))

    def near_point(x, y, r):
        cx, cy = int(x // CELL), int(y // CELL)
        for i in (cx - 1, cx, cx + 1):
            for j in (cy - 1, cy, cy + 1):
                for vx, vy in grid.get((i, j), ()):
                    if (vx - x) ** 2 + (vy - y) ** 2 <= r * r:
                        return True
        return False

    def near_segment(x1, y1, x2, y2, r):
        i0, i1 = int((min(x1, x2) - r) // CELL), int((max(x1, x2) + r) // CELL)
        j0, j1 = int((min(y1, y2) - r) // CELL), int((max(y1, y2) + r) // CELL)
        if (i1 - i0 + 1) * (j1 - j0 + 1) > 40000:
            return False
        dx, dy = x2 - x1, y2 - y1
        L2 = dx * dx + dy * dy
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                for vx, vy in grid.get((i, j), ()):
                    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((vx - x1) * dx + (vy - y1) * dy) / L2))
                    px, py = x1 + t * dx, y1 + t * dy
                    if (vx - px) ** 2 + (vy - py) ** 2 <= r * r:
                        return True
        return False

    addr, roads = set(), []
    fp = (osmium.FileProcessor(src).with_locations("flex_mem")
          .with_filter(osmium.filter.KeyFilter("addr:housenumber", "highway")))
    for o in fp:
        t = o.tags
        if o.is_node():
            if "addr:housenumber" in t and near_point(o.location.lon * K, o.location.lat * Y, ADDR_R):
                addr.add("\t".join([f"{o.location.lat:.7f}", f"{o.location.lon:.7f}", clean(t.get("addr:housenumber")),
                                    clean(t.get("addr:street")), clean(t.get("addr:postcode")), clean(t.get("addr:place"))]))
        elif o.is_way():
            locs = None
            if "addr:housenumber" in t:
                locs = way_locs(o)
                if any(near_point(lo * K, la * Y, ADDR_R) for la, lo in locs):
                    lat, lon = bbox_center(locs)
                    addr.add("\t".join([f"{lat:.7f}", f"{lon:.7f}", clean(t.get("addr:housenumber")), clean(t.get("addr:street")),
                                        clean(t.get("addr:postcode")), clean(t.get("addr:place"))]))
            hw, rn = t.get("highway"), t.get("name")
            if hw and rn and hw not in ROAD_EXCLUDED:
                locs = locs if locs is not None else way_locs(o)
                if any(near_segment(lo1 * K, la1 * Y, lo2 * K, la2 * Y, ROAD_R) for (la1, lo1), (la2, lo2) in zip(locs, locs[1:])):
                    roads.append({"type": "way", "id": o.id, "tags": {"highway": hw, "name": rn},
                                  "geometry": [{"lat": round(la, 7), "lon": round(lo, 7)} for la, lo in locs]})

    # ---- Write: one pseudo-commune row per town, followed by its venues ----------------------
    header = "@type\t@id\t@lat\t@lon\tname\tadmin_level\tpostal_code\tref:INSEE\tamenity\taddr:housenumber\taddr:postcode\taddr:street\taddr:place"
    lines = [header]
    for k, (town, vs) in enumerate(sorted(towns.items()), 1):
        lines.append("\t".join(["relation", str(k), "", "", town, "8", "", iso, "", "", "", "", ""]))
        for v in vs:
            t = v["tags"]
            lines.append("\t".join([v["type"], str(v["id"]), f"{v['lat']:.7f}", f"{v['lon']:.7f}", clean(t.get("name")),
                                    "", "", "", kind_of(t), clean(t.get("addr:housenumber")),
                                    clean(t.get("addr:postcode")), clean(t.get("addr:street")), clean(t.get("addr:place"))]))
    with open(os.path.join(out_dir, f"{code}_poi.csv"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(out_dir, f"{code}_addr.csv"), "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(addr)) + "\n")
    with open(os.path.join(out_dir, f"{code}_roads.json"), "w", encoding="utf-8") as f:
        json.dump({"version": 0.6, "generator": "six-seven extract (OSM data © OpenStreetMap contributors, ODbL)",
                   "elements": roads}, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{code}: {len(kept)} venues in {len(towns)} towns, {len(addr)} addresses, {len(roads)} roads", flush=True)


if __name__ == "__main__":
    main()
