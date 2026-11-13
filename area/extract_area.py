"""Build Overpass-compatible files (<code>_poi.csv, <code>_addr.csv, <code>_roads.json) for ONE area
(for example the Emirate of Dubai) from an OSM extract, in the same format as scripts/extract.py,
so the osm_import pipeline of the Six-seven database can process them.

The area is the administrative boundary whose ISO3166-2 tag equals <iso>. Every venue inside it gets
the town name <city> (written as a single pseudo-commune row).

Usage: python3 -I extract_area.py <input.osm.pbf> <out_dir> <code> <iso> <city> [--en]
  --en : prefer the English name (name:en) of venues and roads when there is one.
"""
import json
import math
import os
import sys
from collections import defaultdict

import osmium
import shapely
from shapely.geometry import MultiPolygon, Point, Polygon

AMENITIES = {"bar", "pub", "cafe", "restaurant", "biergarten", "nightclub"}
AMENITY_AS = {"hookah_lounge": "cafe"}          # shisha lounges: listed as cafés
LEISURE_AS = {"beach_resort": "bar"}            # beach clubs: listed as bars
ROAD_EXCLUDED = {"motorway", "motorway_link", "trunk_link", "proposed", "construction", "platform", "steps",
                 "corridor", "elevator", "bus_stop"}
CELL = 200.0
ADDR_R = 40.0
ROAD_R = 100.0


def clean(v):
    return (v or "").replace("\t", " ").replace("\n", " ").replace("\r", " ").strip()


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
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    english = "--en" in sys.argv
    src, out_dir, code, iso, city = args[:5]
    os.makedirs(out_dir, exist_ok=True)

    def nm(t):
        return (t.get("name:en") if english else None) or t.get("name")

    def kind_of(t):
        a = t.get("amenity")
        if a in AMENITIES:
            return a
        if a in AMENITY_AS:
            return AMENITY_AS[a]
        return LEISURE_AS.get(t.get("leisure"))

    # ---- Pass A: venues + the area boundary --------------------------------------------------
    venues = {}
    area = None
    area_id = 0
    fp = (osmium.FileProcessor(src).with_locations("flex_mem")
          .with_areas(osmium.filter.KeyFilter("boundary", "amenity", "leisure"))
          .with_filter(osmium.filter.KeyFilter("amenity", "boundary", "leisure")))
    for o in fp:
        t = o.tags
        if o.is_area():
            if o.from_way():
                continue
            if t.get("boundary") == "administrative" and t.get("ISO3166-2") == iso:
                g = area_polygon(o)
                if g is not None and not g.is_empty:
                    area, area_id = g, o.orig_id()
            elif kind_of(t) and nm(t):
                key = f"relation/{o.orig_id()}"
                locs = [(n.lat, n.lon) for ring in o.outer_rings() for n in ring]
                if key not in venues and locs:
                    lat, lon = bbox_center(locs)
                    venues[key] = {"type": "relation", "id": o.orig_id(), "lat": lat, "lon": lon, "tags": dict(t)}
            continue
        if not kind_of(t) or not nm(t):
            continue
        if o.is_node():
            venues[f"node/{o.id}"] = {"type": "node", "id": o.id, "lat": o.location.lat, "lon": o.location.lon, "tags": dict(t)}
        elif o.is_way():
            locs = way_locs(o)
            if locs:
                lat, lon = bbox_center(locs)
                venues[f"way/{o.id}"] = {"type": "way", "id": o.id, "lat": lat, "lon": lon, "tags": dict(t)}
    print(f"pass A: {len(venues)} venues in the file, area {iso} found: {area is not None}", flush=True)
    if area is None:
        sys.exit(f"area {iso} not found")

    shapely.prepare(area)
    kept = [v for v in venues.values() if area.contains(Point(v["lon"], v["lat"]))]
    by_kind = defaultdict(int)
    for v in kept:
        by_kind[kind_of(v["tags"])] += 1
    print(f"venues inside {iso}: {len(kept)} {dict(by_kind)}", flush=True)
    if not kept:
        sys.exit("no venue inside the area")

    lat0 = sum(v["lat"] for v in kept) / len(kept)
    K = 111320 * math.cos(math.radians(lat0))
    Y = 111132.954 - 559.822 * math.cos(math.radians(2 * lat0)) + 1.175 * math.cos(math.radians(4 * lat0))
    grid = defaultdict(list)
    for v in kept:
        x, y = v["lon"] * K, v["lat"] * Y
        grid[(int(x // CELL), int(y // CELL))].append((x, y))

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

    # ---- Pass B: addresses + named roads near the venues -------------------------------------
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
            hw, rn = t.get("highway"), nm(t)
            if hw and rn and hw not in ROAD_EXCLUDED:
                locs = locs if locs is not None else way_locs(o)
                if any(near_segment(lo1 * K, la1 * Y, lo2 * K, la2 * Y, ROAD_R) for (la1, lo1), (la2, lo2) in zip(locs, locs[1:])):
                    roads.append({"type": "way", "id": o.id, "tags": {"highway": hw, "name": rn},
                                  "geometry": [{"lat": round(la, 7), "lon": round(lo, 7)} for la, lo in locs]})

    # ---- Write -------------------------------------------------------------------------------
    header = "@type\t@id\t@lat\t@lon\tname\tadmin_level\tpostal_code\tref:INSEE\tamenity\taddr:housenumber\taddr:postcode\taddr:street\taddr:place"
    lines = [header, "\t".join(["relation", str(area_id), "", "", city, "4", "", iso, "", "", "", "", ""])]
    for v in kept:
        t = v["tags"]
        lines.append("\t".join([v["type"], str(v["id"]), f"{v['lat']:.7f}", f"{v['lon']:.7f}", clean(nm(t)),
                                "", "", "", kind_of(t), clean(t.get("addr:housenumber")),
                                clean(t.get("addr:postcode")), clean(t.get("addr:street")), clean(t.get("addr:place"))]))
    with open(os.path.join(out_dir, f"{code}_poi.csv"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(out_dir, f"{code}_addr.csv"), "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(addr)) + "\n")
    with open(os.path.join(out_dir, f"{code}_roads.json"), "w", encoding="utf-8") as f:
        json.dump({"version": 0.6, "generator": "six-seven extract (OSM data © OpenStreetMap contributors, ODbL)",
                   "elements": roads}, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{code}: {len(kept)} venues, {len(addr)} addresses, {len(roads)} roads", flush=True)


if __name__ == "__main__":
    main()
