# Teams and resource scope

Expansion phase E3 (plan §5.12). Teams decide **which** models, keys, agents and
knowledge bases a person or an application can reach. Permissions still decide **what**
they can do with them. Both checks must pass.

An install with no teams behaves exactly as before. Every existing resource is
*shared*, and shared resources work the way everything did before E3.

Code: `backend/app/core/scope.py` (the rule), `backend/app/services/teams.py` (teams,
membership, the per-request guard), `backend/app/api/v1/teams.py` (API).

## The rule

These resources carry an optional `team_id`:

| resource | notes |
|---|---|
| models | deployments belong to their model's team |
| model aliases | |
| API clients | a key belongs to its client's team |
| agents | runs belong to their agent's team |
| knowledge bases | documents belong to their base's team |
| workflows (E6) | runs and approvals belong to their workflow's team |

- **`team_id` empty: shared.** Anyone who holds the resource's permission can see it.
- **`team_id` = T: owned by team T.** Only members of T can see it, plus callers whose
  scope covers every team: superusers and holders of **`scope.all`**.

A resource outside your scope is reported as **404 Not Found**, never 403. A 403 would
confirm that something with that id exists in another team.

Lists leave out-of-scope rows out. Paginated totals (`GET /models`) count only what you
can see.

## Creating into a team

Create requests for the six resources above accept an optional `team_id`:

- Omit it to create a shared resource, as before.
- To set a team, you must be a member of that team, unless you hold `scope.all`.
  Otherwise the answer is 403.

References are checked against the creator's scope too. An alias cannot point at a
model you cannot see, and an agent cannot name a model alias or knowledge base you
cannot see. An agent runs with the platform's reach, so this check is what stops an
agent from becoming a way around team scope. Workflow definitions get the same check,
on save and on publish, for the knowledge bases and model names their steps use.

## What an agent or workflow may use (E18)

Since E18 an agent or workflow is also checked against **its own team**, because that is
what it runs as. It may use shared resources and its own team's, never another team's,
whoever saved it. In particular a **shared** agent or workflow may use only **shared**
models, aliases and knowledge bases: otherwise it would offer one team's model to
everyone. This is the rule an API key already follows.

It is checked:

| when | answer |
|---|---|
| saving an agent, or saving or publishing a workflow | 422, with `details.uses` listing what is out of reach |
| `PUT /scope` moving an agent or workflow somewhere that cannot use what it refers to | 409; the resource is not moved |
| every agent run (`/agents/{id}/execute`, `agent:<slug>`, voice) | 409; no run is created |
| every workflow LLM and retrieval step, on the rendered values | the step fails with the same message |

The message names the problem and the fix: *This agent uses model 'x', which belongs to
another team. It may only use shared resources (it is shared). Move the agent into the
same team, or make the resource shared.* A templated model name such as
`{{ input.model }}` is judged at run time, by what it became.

**Behaviour change.** A shared agent or workflow that refers to a team's model or
knowledge base stops working at its next run after the upgrade. Move it into that team,
or make the resource shared.

## Moving a resource between teams

```http
PUT /api/v1/scope
{"resource_type": "model", "resource_id": "…", "team_id": "…"}   # or "team_id": null for shared
```

To move a resource you need all of these:

- the permission that changes that kind of resource: `model.register` (model),
  `model.deploy` (alias), `apikey.manage` (API client), `agent.edit` (agent),
  `knowledge.manage` (knowledge base), `workflow.edit` (workflow);
- sight of the resource;
- membership of both the team it leaves and the team it joins, unless you hold
  `scope.all`.

Each move is audited as `SCOPE_ASSIGNED`, with the team before and after.

## The gateway

An API key's scope is its **client's team**:

- A key whose client belongs to team K can call shared aliases and models, and team K's.
- A key whose client is shared can call only shared aliases and models.
- An alias is usable only when both the alias and the model behind it are in scope. Its
  fallback alias must be in scope too, or the fallback is skipped.
- `GET /v1/models` lists only what the key can call, including `agent:<slug>` entries.
  An `agent:` route to an agent in another team answers 404.
- Each usage record stores the key's team at call time (`usage_records.team_id`). Moving
  a client to another team later does not rewrite past usage.

Calls with no API key are not team-scoped: the voice assistant, agent runs started from
the console, and document ingestion. They are the platform acting for itself, and the
people who start them have already passed the scope check on the agent or knowledge base.

## Team quotas

Gateway quotas gain the scope **`team`** (subject: the team's id, or `*` for each team
individually). A team budget is shared by every key whose client belongs to the team. It
is counted from `usage_records.team_id`, the same way key budgets are. See
[gateway-policy.md](gateway-policy.md).

## Teams and members

| who | may |
|---|---|
| `team.view` | list and read teams: every team with `scope.all`, otherwise their own |
| `team.manage` | create, rename and delete teams, and manage any team's members |
| a team **owner** | manage that team's members without `team.manage` (needs `team.view` to reach the routes) |

- A team that owns resources cannot be deleted (409, and the error lists what it owns).
  Move or delete the resources first. Otherwise they would either be destroyed or
  silently become shared with everyone.
- A team that has owners always keeps at least one.
- Membership is read on every request, like permissions. Removing someone takes effect
  on their next call.

Audit actions: `TEAM_CREATED`, `TEAM_UPDATED`, `TEAM_DELETED`, `TEAM_MEMBER_ADDED`,
`TEAM_MEMBER_UPDATED`, `TEAM_MEMBER_REMOVED`, `SCOPE_ASSIGNED`.

## Roles

The plan names eight built-in roles. They map onto the existing role names, which are
**not renamed**: OIDC and LDAP group mappings refer to role names, and renaming one
would silently strip people's access.

| plan role | role here | notes |
|---|---|---|
| Super Admin | `SUPER_ADMIN` | |
| Platform Admin | `ADMIN` (and `INFRA_ADMIN` for hosts only) | |
| Model Admin | `AI_ADMIN` | |
| Developer | `DEVELOPER` | |
| Knowledge Admin | `KNOWLEDGE_ADMIN` | **new in E3**: knowledge bases and ingestion |
| Agent Developer | `AGENT_ADMIN` | |
| Auditor | `AUDITOR` | also holds `team.view` and `scope.all`, so an audit sees every team |
| User | `USER` | |

`scope.all` is held by `SUPER_ADMIN`, `ADMIN` and `AUDITOR` only. A unit test asserts
that no other role has it.

## Reads that follow scope (E18)

- **Traces.** `GET /traces/{trace_id}` of another team's agent run is 404, like an
  unknown trace id.
- **Voice sessions.** A session cannot be opened against another team's agent (404).
- **Memory and conversations** follow their **agent's** team, on top of the tenant and
  end-user scope of M16. Entries with no agent, or whose agent was deleted, count as
  shared. Search, list, `GET /conversations/{id}` and deleting one entry see only those.
  `POST /memory/forget-all` from a team-limited caller erases only those entries and
  leaves session state alone (it is keyed by person, not agent). A `scope.all` caller
  erases everything, as before.
- **Dashboard.** Model and deployment counts, and the gateway section (requests, errors,
  tokens, timings, top models, the hourly series), count what your teams can see, as
  `GET /usage` does. The **monitoring overview** counts runs of agents and usage of keys
  in your teams.
- **Definitions import** (`POST /definitions/import`) does not add a version to another
  team's agent, and skips an agent whose model or knowledge bases its team cannot use,
  with the reason. The CLI import runs as the platform.

## Not team-scoped

- **Quotas and circuit breakers** are platform settings, managed with `quota.*`.
- **Tools, skills and MCP servers** are a platform catalogue. An agent's tool grants are
  governed by `tool.*` permissions and the §10 pipeline.
- **Fleet figures** (nodes, GPUs, backends, dependencies) are platform-wide.

## Verifying

`tests/api/test_route_authorization.py` walks every route in the OpenAPI schema:

- With no credentials, every route except a short explicit public list must answer 401.
- With a user holding no permissions, every management route must answer 403.

A new route is covered the day it is added.

`tests/api/test_teams.py` covers:

- team management;
- list, get and gateway isolation;
- creating into a team;
- moving resources;
- team quotas.

`tests/api/test_team_scoping_e18.py` covers the E18 checks: save, move and run-time
references for agents and workflows, traces, voice, memory and the dashboard.
