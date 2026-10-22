# D1 labelled match pairs

`match_pairs.csv` contains **102 pairs**: 34 text queries, 34 photo queries and
34 text+photo queries. Each modality has 17 positives and 17 negatives. The 17
query scenarios cover 11 D1 categories. Candidate pairs include 15 same-category
appearance negatives and 2 cross-category negatives per modality.

The five columns match `scripts/calibrate_match_threshold.py` exactly:

```text
query_text,query_photo,item_description,item_photo,is_match
```

Photo paths are relative to this directory. All 21 referenced original JPEGs are
included in `match_photos/` (about 4.7 MiB total), so a checkout does not need the
full, gitignored D1 download. Item descriptions are copied unchanged from the D1
manifest to retain the text actually embedded during the demo import.

Labels and limitations:

- Queries were manually described from a visual review of D1 context photos;
  ambiguous category examples were excluded. These are synthetic owner queries.
- A positive refers to the same source object/image. A negative is a visibly
  different object, including similar objects in the same category. Category
  equality alone does not make a pair positive.
- Positive photo queries **reuse the candidate's exact original photo**. This is
  a duplicate-photo baseline, not a different-angle or independent-photo benchmark.
  Photo scores will be optimistic. Contexts can contain several objects.
- The same 17 scenarios recur across modalities; the 102 rows are not independent
  trials. No held-out evaluation or production threshold validity is claimed.
  Review scores and add independent owner views before production calibration.

`match_pairs.provenance.json` records each CSV row's query/candidate source IDs,
label basis, photo SHA256 and original author/source/license attribution. Photos
are unmodified Open Images validation originals under their recorded CC BY 2.0
licenses. Retain that attribution when redistributing. Open Images annotations
are CC BY 4.0; dataset credit: Open Images Dataset, Google.

From the repository root, using Node.js 22.12+:

```powershell
npm ci --prefix tests/data --ignore-scripts
node tests/data/match-pairs.mjs validate
npm test --prefix tests/data
```

To reproduce from the local D1 manifest and checksummed downloads:

```powershell
node tests/data/match-pairs.mjs build
```

The build reads `tests/data/generated/manifest.json`, checks each selected image
against D1 SHA256 and copies only those photos. It makes no network/API calls.

To score with the pinned CLIP model, install the matching-service ML dependencies
and configure `CLIP_CHECKPOINT` and `CLIP_SHA256` as described by
`scripts/prepare_clip_model.py`, then run:

```powershell
python scripts/calibrate_match_threshold.py --pairs tests/data/match_pairs.csv --write-scores tests/data/match_scores.csv
```

Scores and a recommended threshold have not been produced as part of this data
handoff. Do not replace MATCH_THRESHOLD with an assumed or fabricated value.
