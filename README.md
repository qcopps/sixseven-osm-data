# sixseven-osm-data

Bars, pubs, cafés and restaurants of France, one set of files per département, used to fill the venue list of the Six-seven app.

- `scripts/extract.py` reads a Geofabrik OpenStreetMap extract and writes, for each département `XX`:
  - `data/frXX_poi.csv`: the venues, grouped by commune;
  - `data/frXX_addr.csv`: addresses within 40 m of a venue;
  - `data/frXX_roads.json`: named roads within 100 m of a venue.
- `.github/workflows/build.yml` runs it on GitHub Actions for every French region and commits the result to `data/`.

Data © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), available under the Open Database License (ODbL).
