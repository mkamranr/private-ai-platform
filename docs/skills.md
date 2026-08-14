# Skills (M11)

A skill is a versioned, reusable package: name, description, instructions,
`required_tools`, `required_knowledge`, `parameters`, `required_permission`, `version`.

Reusable across agents — that reuse is the point, and is why skills are a separate entity
rather than prompt text on an agent.

Skills **append** to an agent's system prompt rather than replacing it: the agent's own
instructions set its character and boundaries, and a skill adds a capability within them.
See [agents.md](agents.md#skills-m11).


## Shipped skills

Declarative, in `skills/*.yaml`, imported with `make definitions-import` (or
`POST /api/v1/definitions/import`). They ship inside the offline bundle, so an air-gapped
site gets the same skills as everyone else rather than an operator retyping instructions.

| | what it is for |
|---|---|
| `document-grounded-answers` | Answer only from retrieved passages, cite every claim, say plainly when the documents do not cover it |
| `arabic-english-correspondence` | Reply in the language asked, keep formal register, never translate names or reference numbers |
| `platform-operations` | Report platform health from live status, explain what each state means, stop short of acting |
| `quantitative-reasoning` | Compute every figure with the calculator **before** stating it, show the expression, never estimate |
| `platform-capabilities` | Answer "what can this do" from the live model catalogue, distinguish registered from running |
| `decisions-and-actions` | Separate decisions from discussion, give every action an owner and an absolute date, list what the record never said |
| `sensitive-information-handling` | Carry classification and personal data across a document boundary deliberately, and say what was carried |

`quantitative-reasoning` is mostly a rule about *sequence*, which is the part that is easy
to miss. An agent handed a calculator will happily do the sum in its head and call the
tool afterwards to confirm — at which point the tool is decoration, because the answer was
already written.

`decisions-and-actions` exists because discussion compresses well and commitments do not
compress at all. "It was agreed to review the policy" names no owner and no date, reads
like a record, and cannot be chased — so an action is owner, action and absolute date, or
it is reported as incomplete rather than quietly completed by inference.

`sensitive-information-handling` requires no tools, and could not have one. The platform
being air-gapped removes exactly one risk — the answer leaving the building — and does
nothing about a summary that widens the audience of what it summarises. No classifier can
be trusted to decide what is sensitive on someone's behalf; what an agent can do is say
what it noticed, say what it left out, and stop when the boundary is unclear.

Re-importing an edited file **applies the new text**: instructions are the whole content
of a skill, and an operator who rewords one expects the next run to use it. What a given
run actually saw is in its trace, so history stays explainable.
