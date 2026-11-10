"""Build Overpass-compatible files (frXX_poi.csv, frXX_addr.csv, frXX_roads.json) for French départements
from an OSM extract (.osm.pbf / .osm), so the existing osm_import pipeline in the database can process them.

Usage: python3 -I extract.py <input.osm.pbf> <out_dir> <dep,dep,...> [lat0]
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
ROAD_EXCLUDED = {"motorway", "motorway_link", "trunk_link", "proposed", "construction", "platform", "steps",
                 "corridor", "elevator", "bus_stop"}
CELL = 200.0  # metres
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
    src, out_dir, deps = sys.argv[1], sys.argv[2], set(sys.argv[3].split(","))
    os.makedirs(out_dir, exist_ok=True)

    # ---- Pass A: venues + communes -------------------------------------------------------------
    venues = {}      # osm_id -> dict
    communes = []    # (ref, id, name, postal_code, geom)
    fp = (osmium.FileProcessor(src).with_locations("flex_mem")
          .with_areas(osmium.filter.KeyFilter("boundary", "amenity"))
          .with_filter(osmium.filter.KeyFilter("amenity", "boundary")))
    for o in fp:
        t = o.tags
        if o.is_area():
            if o.from_way():
                continue
            if t.get("boundary") == "administrative" and t.get("admin_level") == "8" and t.get("ref:INSEE"):
                g = area_polygon(o)
                if g is not None and not g.is_empty:
                    communes.append((t.get("ref:INSEE"), o.orig_id(), clean(t.get("name")), clean(t.get("postal_code")), g))
            elif t.get("amenity") in AMENITIES and t.get("name"):
                key = f"relation/{o.orig_id()}"
                if key not in venues:
                    locs = [(n.lat, n.lon) for ring in o.outer_rings() for n in ring]
                    if locs:
                        lat, lon = bbox_center(locs)
                        venues[key] = {"type": "relation", "id": o.orig_id(), "lat": lat, "lon": lon, "tags": dict(t)}
            continue
        if t.get("amenity") not in AMENITIES or not t.get("name"):
            continue
        if o.is_node():
            venues[f"node/{o.id}"] = {"type": "node", "id": o.id, "lat": o.location.lat, "lon": o.location.lon, "tags": dict(t)}
        elif o.is_way():
            locs = way_locs(o)
            if locs:
                lat, lon = bbox_center(locs)
                venues[f"way/{o.id}"] = {"type": "way", "id": o.id, "lat": lat, "lon": lon, "tags": dict(t)}
    print(f"pass A: {len(venues)} venues, {len(communes)} communes", flush=True)

    # ---- Venue -> commune -> département -------------------------------------------------------
    tree = shapely.STRtree([c[4] for c in communes])
    vlist = list(venues.values())
    pts = [Point(v["lon"], v["lat"]) for v in vlist]
    pairs = tree.query(pts, predicate="within") if communes else [[], []]
    inside = {}
    for vi, ci in zip(pairs[0], pairs[1]):
        inside.setdefault(int(vi), int(ci))
    no_commune = 0
    for i, v in enumerate(vlist):
        ci = inside.get(i)
        v["commune"] = ci
        v["dep"] = communes[ci][0][:2] if ci is not None else None
        if ci is None:
            no_commune += 1
    kept = [v for v in vlist if v["dep"] in deps]
    print(f"venues in target départements: {len(kept)} (outside any commune: {no_commune})", flush=True)
    if not kept:
        return

    lat0 = float(sys.argv[4]) if len(sys.argv) > 4 else sum(v["lat"] for v in kept) / len(kept)
    K = 111320 * math.cos(math.radians(lat0))
    Y = 111132.954 - 559.822 * math.cos(math.radians(2 * lat0)) + 1.175 * math.cos(math.radians(4 * lat0))

    grid = defaultdict(list)  # cell -> [(x, y, dep)]
    for v in kept:
        x, y = v["lon"] * K, v["lat"] * Y
        grid[(int(x // CELL), int(y // CELL))].append((x, y, v["dep"]))

    def deps_near_point(x, y, r):
        found = set()
        cx, cy = int(x // CELL), int(y // CELL)
        for i in (cx - 1, cx, cx + 1):
            for j in (cy - 1, cy, cy + 1):
                for vx, vy, d in grid.get((i, j), ()):
                    if d not in found and (vx - x) ** 2 + (vy - y) ** 2 <= r * r:
                        found.add(d)
        return found

    def deps_near_segment(x1, y1, x2, y2, r):
        found = set()
        i0, i1 = int((min(x1, x2) - r) // CELL), int((max(x1, x2) + r) // CELL)
        j0, j1 = int((min(y1, y2) - r) // CELL), int((max(y1, y2) + r) // CELL)
        dx, dy = x2 - x1, y2 - y1
        L2 = dx * dx + dy * dy
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                for vx, vy, d in grid.get((i, j), ()):
                    if d in found:
                        continue
                    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((vx - x1) * dx + (vy - y1) * dy) / L2))
                    px, py = x1 + t * dx, y1 + t * dy
                    if (vx - px) ** 2 + (vy - py) ** 2 <= r * r:
                        found.add(d)
        return found

    # ---- Pass B: addresses + roads near venues ---------------------------------------------------
    addr = defaultdict(list)
    roads = defaultdict(list)
    fp = (osmium.FileProcessor(src).with_locations("flex_mem")
          .with_filter(osmium.filter.KeyFilter("addr:housenumber", "highway")))
    for o in fp:
        t = o.tags
        if o.is_node():
            if "addr:housenumber" not in t:
                continue
            lat, lon = o.location.lat, o.location.lon
            ds = deps_near_point(lon * K, lat * Y, ADDR_R)
            if ds:
                row = "\t".join([f"{lat:.7f}", f"{lon:.7f}", clean(t.get("addr:housenumber")), clean(t.get("addr:street")),
                                 clean(t.get("addr:postcode")), clean(t.get("addr:place"))])
                for d in ds:
                    addr[d].append(row)
        elif o.is_way():
            locs = None
            if "addr:housenumber" in t:
                locs = way_locs(o)
                ds = set()
                for la, lo in locs:
                    ds |= deps_near_point(lo * K, la * Y, ADDR_R)
                if ds:
                    lat, lon = bbox_center(locs)
                    row = "\t".join([f"{lat:.7f}", f"{lon:.7f}", clean(t.get("addr:housenumber")), clean(t.get("addr:street")),
                                     clean(t.get("addr:postcode")), clean(t.get("addr:place"))])
                    for d in ds:
                        addr[d].append(row)
            hw = t.get("highway")
            if hw and t.get("name") and hw not in ROAD_EXCLUDED:
                locs = locs if locs is not None else way_locs(o)
                ds = set()
                for (la1, lo1), (la2, lo2) in zip(locs, locs[1:]):
                    ds |= deps_near_segment(lo1 * K, la1 * Y, lo2 * K, la2 * Y, ROAD_R)
                    if len(ds) == len(deps):
                        break
                if ds:
                    el = {"type": "way", "id": o.id, "tags": {"highway": hw, "name": t.get("name")},
                          "geometry": [{"lat": round(la, 7), "lon": round(lo, 7)} for la, lo in locs]}
                    for d in ds:
                        roads[d].append(el)

    # ---- Write files -----------------------------------------------------------------------------
    header = "@type\t@id\t@lat\t@lon\tname\tadmin_level\tpostal_code\tref:INSEE\tamenity\taddr:housenumber\taddr:postcode\taddr:street\taddr:place"
    by_dep = defaultdict(lambda: defaultdict(list))
    for v in kept:
        by_dep[v["dep"]][v["commune"]].append(v)
    for d, per_commune in sorted(by_dep.items()):
        code = "fr" + d.lower()
        lines = [header]
        for ci, vs in sorted(per_commune.items(), key=lambda kv: communes[kv[0]][0]):
            ref, cid, cname, cpc, _ = communes[ci]
            lines.append("\t".join(["relation", str(cid), "", "", cname, "8", cpc, ref, "", "", "", "", ""]))
            for v in vs:
                t = v["tags"]
                lines.append("\t".join([v["type"], str(v["id"]), f"{v['lat']:.7f}", f"{v['lon']:.7f}", clean(t.get("name")),
                                        "", "", "", t.get("amenity"), clean(t.get("addr:housenumber")),
                                        clean(t.get("addr:postcode")), clean(t.get("addr:street")), clean(t.get("addr:place"))]))
        with open(os.path.join(out_dir, f"{code}_poi.csv"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        with open(os.path.join(out_dir, f"{code}_addr.csv"), "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(set(addr[d]))) + "\n")
        with open(os.path.join(out_dir, f"{code}_roads.json"), "w", encoding="utf-8") as f:
            json.dump({"version": 0.6, "generator": "six-seven extract (OSM data © OpenStreetMap contributors, ODbL)",
                       "elements": roads[d]}, f, ensure_ascii=False, separators=(",", ":"))
        n_v = sum(len(vs) for vs in per_commune.values())
        print(f"{code}: {n_v} venues in {len(per_commune)} communes, {len(set(addr[d]))} addresses, {len(roads[d])} roads", flush=True)


if __name__ == "__main__":
    main()
