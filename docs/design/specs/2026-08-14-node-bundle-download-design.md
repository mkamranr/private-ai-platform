# Serving node install artifacts from the control plane — design

**Date:** 2026-08-14
**Status:** approved

## Problem

Registering a node prints:

> Run this on the node, from the offline bundle you copied there

which asks an operator to move **1.9 GB** to the host and does not say which parts matter.
A node needs four things totalling **288 MB**:

| | |
|---|---|
| `install-node.sh`, `lib.sh` | the installer and its helpers |
| `manifest.json` | what the installer verifies the archive against |
| `images/node-agent.tar` | 288 MB — everything else in `images/` is for the control plane |

The other 1.6 GB is postgres, valkey, qdrant, minio, nginx, backend and the mock engine,
none of which a node runs.

## Rule 4 is not in the way

Rule 4 forbids pulling **from the Internet** — Docker Hub, PyPI — which is what makes the
platform installable on a host with no route out. A node fetching from its own control
plane on the same isolated network is not that: the bytes were already carried in on the
bundle, and this only moves them the last hop.

`install-node.sh` keeps its property exactly as its header claims: **it downloads
nothing**. The operator's `curl` does, before the installer runs.

## Design

### 1. Staging

`offline/install.sh` copies the four artifacts into `${PLATFORM_DATA_ROOT}/node-bundle/`,
mounted into the backend **read-only** at `/data/node-bundle`. The backend can read those
files and nothing else on the host.

### 2. Endpoint

`GET /api/v1/nodes/enrollment-bundle`, authenticated by `EnrollmentContextDep` — the same
one-time enrolment token that authorises joining the fleet. Three consequences worth
stating:

* no second credential exists to be managed, rotated or leaked;
* the token is *resolved*, not *consumed* — downloading must not burn the single use that
  `enroll_node` needs afterwards;
* the existing per-source rate limit applies before the token is looked at.

The response is a `tar` built from a **fixed list of four filenames**. No path, prefix or
pattern comes from the request, so there is no traversal surface to get wrong.

### 3. Console text

Staged — three lines, the token carried in a header rather than the URL because the access
log records paths:

```bash
curl -fsSL -H "Authorization: Bearer aine_..." \
     http://server:8080/api/v1/nodes/enrollment-bundle -o node-bundle.tar
tar xf node-bundle.tar
sudo ./install-node.sh --server ... --name ... --token ...
```

Not staged — today's wording, plus the four paths and the 288 MB figure, so the flow
degrades to something that still works rather than a link that 404s.

### 4. The air-gap gate

`check_airgap.py` scans `.py` as well as shell, so the module printing that `curl` line
trips "no runtime code path shells out to a network fetcher". It needs a
`NETWORK_ALLOWLIST` entry — the same treatment the acceptance gates already carry — with
the reason: *prints the operator's download command; the platform itself fetches nothing.*

This is the one real cost of the feature. It is a documented hole rather than a silent one,
and the check keeps its meaning everywhere else.

### 5. Transport

Served over whatever scheme the console advertises. `install-node.sh` already refuses a
plain-HTTP control plane for the enrolment callback unless `--insecure-http` is passed;
the download stays consistent with that rather than inventing a second policy. On an
isolated GPU subnet plain HTTP is acceptable; on anything shared it is not, and the
existing refusal is what says so.

## Testing

* the token is required, and a user JWT is not accepted in its place;
* downloading does **not** consume the enrolment, so the subsequent enrol still succeeds;
* unstaged returns 404 naming what is missing, not a stack trace;
* the served archive contains exactly the four artifacts.

Phase 1 asserts the endpoint, since it already owns enrolment. Phase 8 asserts that
`install.sh` stages the artifacts, since it already rehearses a real install.

## Noted, not fixed

The backend mounts no `/data` at all, yet `MODELS__ROOT_PATH=/data/models` is configured —
so scanning a real model's files from the backend would fail. Nothing has hit it because
the `mock` runtime skips the filesystem by design. Pre-existing and out of scope here;
this design adds only the narrow `node-bundle` mount.
