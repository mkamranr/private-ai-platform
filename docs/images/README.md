# Screenshots

The README references these by exact filename. Drop the files in this directory with these
names and they appear on the GitHub front page — no other change needed.

## Present

| filename | what it shows |
|---|---|
| `dashboard.png` | Fleet summary, gateway traffic, recent audit activity. The front-page image |
| `gpus.png` | Per-card utilisation, memory, temperature and power, with allocation state |
| `agents.png` | The shipped agents with the tools and skills each one holds |
| `knowledge.png` | A knowledge base with an OCR'd document indexed, and the retrieval preview |

Downscaled to 1800px wide — still 2× what GitHub renders a README image at, and it keeps
each under 400 KB. They live in the repository forever.

## Worth adding later

| filename | what to capture | why |
|---|---|---|
| `chat.png` | Open WebUI mid-conversation with a real answer | The only screenshot that proves it is an AI platform rather than an admin panel |
| `models.png` | Model registry with a deployment RUNNING | Where operators actually spend their time |
| `approvals.png` | The human-in-the-loop queue with something pending | The part of the agent design that is hardest to explain in prose |

The README references only files that exist, so adding one means adding the file *and* a
line to the README — a reference to a missing image renders as a broken box on the front
page, which is worse than having one fewer screenshot.

## Taking them

**Sign out of anything real first.** These end up on a public repository.

- Use the **dark theme** — it is the default and what the UI was designed against.
- Browser window at **1440×900 or wider**, zoom at 100%.
- Capture the **viewport, not the whole screen**: no desktop, no dock, no other tabs.
- PNG. Keep each under ~400 KB; they are in the repository forever.

## Check before committing

Screenshots leak more than people expect. Look for:

- Real usernames, email addresses or hostnames — `admin` and `localhost` are fine
- API keys, tokens or anything beginning `aip_`, `aine_` or `sk-`
- Internal IP addresses or machine names that identify your network
- Document titles or chat content from anything real

If a screenshot would be more useful with data in it, seed it with obviously synthetic
content rather than blurring real content afterwards.

## A note on the empty states

Several screens deliberately show an illustrated empty state before anything is
configured. Those are worth a screenshot too — they are what a first-time user actually
sees, and a README full of fully-populated dashboards sets an expectation the first five
minutes will not meet.
