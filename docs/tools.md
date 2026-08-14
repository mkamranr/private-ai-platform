# Tools (M12)

Every tool carries an **explicit permission** and a risk level
(`LOW|MEDIUM|HIGH|CRITICAL`). Agents get no tool by default.

`HIGH` and `CRITICAL` require human approval (`AGENTS__APPROVAL_REQUIRED_RISK_LEVELS`).

`PYTHON` and `COMMAND` are registerable but **never executable** — §M12 lists them, §25
forbids unrestricted shell execution by agents. Two independent reasons they cannot run:
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

**The §10 authorisation pipeline — including the intersection rule that makes agents safe
— is documented in [agents.md](agents.md#the-tool-pipeline-10).** Read that before adding
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

All five are **read-only**, and that is a boundary rather than a limitation. A tool that
could stop a deployment would be a privileged action taken on a model's say-so; those
belong behind REST at HIGH risk, where the §10 pipeline suspends them for human approval.

An existing tool is **never rewritten** by a re-import. Its type and parameter schema
define what it *is*, and changing them under an agent already granted it would silently
change what that agent can do.

INTERNAL tools resolve to a handler in a closed table in
`app/services/tool_executors.py` — never dynamic dispatch on a name from the database,
which would make "internal" an arbitrary-code path by the back door.
