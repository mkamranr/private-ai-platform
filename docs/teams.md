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

- **`team_id` empty: shared.** Anyone who holds the resource's permission can see it.
- **`team_id` = T: owned by team T.** Only members of T can see it, plus callers whose
  scope covers every team: superusers and holders of **`scope.all`**.

A resource outside your scope is reported as **404 Not Found**, never 403. A 403 would
confirm that something with that id exists in another team.

Lists leave out-of-scope rows out. Paginated totals (`GET /models`) count only what you
can see.

## Creating into a team

Create requests for the five resources above accept an optional `team_id`:

- Omit it to create a shared resource, as before.
- To set a team, you must be a member of that team, unless you hold `scope.all`.
  Otherwise the answer is 403.

References are checked against the creator's scope too. An alias cannot point at a
model you cannot see, and an agent cannot name a model alias or knowledge base you
cannot see. An agent runs with the platform's reach, so this check is what stops an
agent from becoming a way around team scope.

## Moving a resource between teams

```http
PUT /api/v1/scope
{"resource_type": "model", "resource_id": "…", "team_id": "…"}   # or "team_id": null for shared
```

To move a resource you need all of these:

- the permission that changes that kind of resource: `model.register` (model),
  `model.deploy` (alias), `apikey.manage` (API client), `agent.edit` (agent),
  `knowledge.manage` (knowledge base);
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

## Not team-scoped (yet)

- **Memory and conversations** are scoped by tenant and end user (M16), not by team.
- **Dashboard counts** and **monitoring** are platform-wide operator views.
- **Quotas and circuit breakers** are platform settings, managed with `quota.*`.
- **Tools, skills and MCP servers** are a platform catalogue. An agent's tool grants are
  governed by `tool.*` permissions and the §10 pipeline.
- **Agent runtime:** an agent's model and knowledge bases are checked when the agent is
  saved, not again on every run. If a referenced model later moves to another team, the
  agent keeps working until it is next edited.

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
