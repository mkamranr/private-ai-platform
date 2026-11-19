# Tools

Every tool carries an **explicit permission** and a risk level
(`LOW|MEDIUM|HIGH|CRITICAL`). Agents get no tool by default.

`HIGH` and `CRITICAL` require human approval (`AGENTS__APPROVAL_REQUIRED_RISK_LEVELS`).

`PYTHON` and `COMMAND` are registerable but **never executable** — catalogued so an operator
can see what exists, because agents must never have unrestricted shell execution. Two independent reasons they cannot run:
the pipeline refuses the type, and the executor table has no entry for them.

Credentials are stored Fernet-encrypted under a key mounted outside the database, and are
decrypted inside the executor at the last moment — never in a tool definition, an event
payload or an API response.

`POST /tools/{id}/test` validates a definition against its executor, so a broken endpoint
or credential surfaces at registration rather than three tool calls into someone's
conversation.

Type and parameter schema cannot be changed after registration: they define what the tool
*is*, and altering them under an agent already granted it would silently change what that
agent can do.

**Team tools.** A tool can belong to a team (`team_id` when registering, or
`PUT /scope` with `resource_type: "tool"`). Members of other teams do not see it, and it
may be granted only to agents of that team; a shared agent may use only shared tools.
This is checked when an agent is saved, when it is moved, and at every run. Moving a tool
into a team therefore stops shared agents that use it at their next run, with a message
saying which tool. Existing and discovered tools are shared. See
[teams.md](teams.md#what-an-agent-or-workflow-may-use).

**REST tools** call their endpoint under the platform's outbound-URL policy, checked when
the tool is saved and on every call: no cloud metadata or link-local addresses, no
credentials in the URL. Private hosts are fine. Redirects are not followed.

## Connectors: SQL and OpenAPI

Two tool types for reaching the systems an agent most often needs, built so the dangerous
version cannot be configured. Both reach only hosts the deployer lists, and both lists
are **empty by default**:

```bash
AGENTS__DATABASE_ALLOWED_HOSTS='["db.internal", "*.reporting.corp.local"]'
AGENTS__OPENAPI_ALLOWED_HOSTS='["tickets.corp.local"]'
```

`db.internal` matches only itself; `*.corp.local` matches its subdomains, not
`corp.local`. The lists are deployment settings (shown read-only on the Settings page), so
a console user cannot widen where agents reach. Hosts are checked when a tool is saved and
again on every call.

### Read-only SQL (`DATABASE`)

*Tools & MCP → Add SQL tool*, or `POST /tools` with `type: "DATABASE"`:

```json
{"type": "DATABASE", "config": {"sql": "SELECT id, total FROM orders WHERE customer_id = :customer_id", "max_rows": 50},
 "credentials": "postgresql://reader:…@db.internal:5432/crm", ...}
```

* **The operator writes the SQL; the model fills in values.** `:name` placeholders are sent
  as bound parameters (`$1`, `$2`, …), never as SQL text. A value is converted to the
  parameter's PostgreSQL type, so a date, number or uuid can arrive as a string.
* Without a `parameters_schema`, one is made from the placeholders (each a required
  string). Add descriptions with a new tool if the model needs them.
* **Read-only, bounded.** Every call runs in a `READ ONLY` transaction, so PostgreSQL itself
  refuses a write, whatever the template says. It has a `statement_timeout`
  (`AGENTS__DATABASE_STATEMENT_TIMEOUT_SECONDS`, default 10) and reads at most `max_rows`
  through a cursor (capped by `AGENTS__DATABASE_MAX_ROWS`, default 200). The model is told
  when more rows exist.
* One statement, a query (`SELECT`, `WITH`, `VALUES`, `TABLE`). Checked on save.
* The connection string is the tool's credential: encrypted, never returned, never shown.
  **Use a role that can only read what the query needs.** The read-only transaction stops
  writes, not reads of a table the role should not see.
* PostgreSQL only. *Test* connects and prepares the statement without running it.

### OpenAPI operations (`OPENAPI`)

*Tools & MCP → Import OpenAPI*, or `POST /tools/openapi/import` with an OpenAPI 3 document
(JSON or YAML text, or an object; the platform never fetches a spec). Each operation becomes
one tool, `<prefix>.<operationId>`:

* Every tool arrives **disabled at HIGH risk**, like a discovered MCP tool. Nothing an
  import adds is callable until someone has read it and enabled it. Names that already
  exist are skipped, never overwritten.
* Path, query and header parameters and a JSON body (`body`) become the parameter schema.
  Local `$ref`s are resolved. Operations with other bodies (multipart, form) are skipped
  and listed.
* The base URL (`base_url`, or the spec's `servers[0].url`) passes the outbound-URL policy
  and must be on `AGENTS__OPENAPI_ALLOWED_HOSTS`.
* The optional credential is sent as `Authorization: Bearer …` (`auth_header`,
  `auth_scheme` to change it). An `Authorization` parameter in the spec is ignored.
* Path values are URL-encoded, slashes included, so a value cannot climb the path.
  Redirects are not followed. At most 1 MB of a response is read.
* Imports are audited as `OPENAPI_IMPORTED`.

Not built: other databases, OpenAPI 2 (Swagger), OAuth flows for an API, and fetching a spec
from a URL.

**The authorisation pipeline — including the intersection rule that makes agents safe
— is documented in [agents.md](agents.md#the-tool-pipeline).** Read that before adding
an executor.


## Shipped tools

Declarative, in `tools/*.yaml`, imported alongside skills and agents.

| | type | risk | what it does |
|---|---|---|---|
| `current_datetime` | INTERNAL | LOW | The current UTC date and time |
| `calculator` | INTERNAL | LOW | Arithmetic, evaluated exactly |
| `date_calculator` | INTERNAL | LOW | Days between dates, date ± days, weekday |
| `platform_status` | INTERNAL | LOW | Nodes, GPUs and deployments by state |
| `model_catalog` | INTERNAL | LOW | Which models this installation serves, and whether they are running |
| `text_statistics` | INTERNAL | LOW | Characters, words and sentences in a text, Arabic and English |

Each is here because a model **cannot** know the answer, not because it is convenient:

- `current_datetime` — weights are fixed, so asked for "today" it invents something
  plausible. Anything reasoning about deadlines or expiry needs it.
- `calculator` and `date_calculator` — a model computes digits the way it composes prose.
  It is right about small sums often enough to look reliable and wrong about long ones
  often enough to matter, and nothing in the output tells the two apart.
- `text_statistics` — a length limit is the one instruction a model cannot check its own
  work against. Asked for "under 200 words" it writes something of roughly the right
  shape and reports a count it estimated; official letters and one-page briefs are
  exactly where roughly is not enough. It counts Arabic too, because a counter that only
  splits ASCII reports zero for half this platform's correspondence and passes every
  check.
- `model_catalog` — what this platform can do was decided by whoever assembled the
  bundle, long after the weights were frozen. Without it, an assistant asked "can you
  transcribe audio here" is guessing, and it guesses wrong in both directions.

### The calculator's whitelist is a security boundary

The expression reaching it was composed by a model, and that model is steered by whatever
text the user put in front of it — so "evaluate this string" is reachable by anyone who
can talk to an agent. `eval` would make that arbitrary code execution. Instead the
expression is parsed to an AST and walked against a whitelist of node types; anything else
is refused, including `__import__`, attribute access, comprehensions and lambdas.
`tests/unit/test_internal_tools.py` fires fourteen shapes of injection at it.

Exponents are capped at 128: `9**9**9` is eight characters and exhausts memory, and there
is no legitimate expression in this tool's remit that needs more.

All six are **read-only**, and that is a boundary rather than a limitation. A tool that
could stop a deployment would be a privileged action taken on a model's say-so; those
belong behind REST at HIGH risk, where the authorisation pipeline suspends them for human approval.

An existing tool is **never rewritten** by a re-import. Its type and parameter schema
define what it *is*, and changing them under an agent already granted it would silently
change what that agent can do.

INTERNAL tools resolve to a handler in a closed table in
`app/services/tool_executors.py` — never dynamic dispatch on a name from the database,
which would make "internal" an arbitrary-code path by the back door.
