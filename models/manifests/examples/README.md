# Example manifests for real engines (Phase 9)

Not imported. `models/manifests/*.y*ml` is scanned one level deep, so anything here ships
with the bundle and stays out of the catalogue until an operator moves it up a directory.

That is deliberate. Each of these declares `runtime: external` — the platform points at
an engine somebody else started rather than starting it — and the platform rightly
refuses such a manifest without an `endpoint_url`. That URL is site-specific: there is no
value this repository could put there that would be correct anywhere.

To use one:

```bash
cp models/manifests/examples/faster-whisper-large-v3.yaml models/manifests/
# add:  endpoint_url: http://whisper.internal:8000
curl -X POST .../api/v1/models/import-manifests -H "Authorization: Bearer $TOKEN"
```

The GPU-free equivalents — `mock-asr`, `mock-tts`, `mock-ocr` — are one directory up and
import automatically, which is what `make gate-phase9` exercises.
