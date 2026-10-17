# Load and scaling tests (owner: ZHANG JI)

k6 scripts used in the demo:

- `search_load.js` - ramp owners searching across venues; shows HPA scaling the API pods and p95 latency
- `bulk_import.js` - upload 5,000 found-item photos; SQS backlog makes KEDA scale the embedding workers out and back in
