# Voice assistant (M29)

Speech in, speech out, against the agents the platform already has.

```
microphone -> WebSocket -> STT -> agent run -> TTS -> speaker
```

Open it at **`/voice/`**; configure it under **Administration → Voice Assistant** in the
admin console.

**There are no settings on the voice page itself.** It had dropdowns for the agent and the
spoken language, which is a form asking somebody to configure a conversation before having
one — and both already have a site default the server applies, with `auto` for language.
The page now sends neither and shows the agent as a status chip instead. What remains is
one control, the microphone, plus a switch for how the assistant is drawn.

## The one architectural rule

**M29 is a client of the agent engine, not a second one** (§3). The transcript goes into
the same `AgentRunService` a typed chat uses, so:

- every existing agent is already voice-capable — nothing is rebuilt for voice;
- tool authorisation is the same §10 intersection of the agent's grants and the speaker's;
- a HIGH-risk tool suspends for approval exactly as it would in text (§41-42).

Voice changes the interface, not the rules. A parallel voice-agent would drift from the
real one, and the first thing to drift would be authorisation.

## Configuration, not code

Everything the assistant uses is chosen in the admin console and applied to the **next
session** — no restart (§49). Settings resolve in the order `.env` already establishes:

```
database override  >  environment / .env  >  field default
```

Only the difference from the environment is stored, so a later bundle that improves a
default is not silently pinned to today's value.

| | |
|---|---|
| Enabled | off until speech models are serving |
| Speech-to-text / text-to-speech | model **aliases**, not URLs |
| Default agent, language, voice | `auto` detects the language — see below |
| Interruption, VAD | barge-in is on by default |
| Store audio / transcripts | **both decisions, both explained on the page** |

### Models are aliases

`enterprise-transcribe` resolves through the registry to whatever the site deployed.
Putting a URL in the voice configuration would create a second way to reach a model that
the registry does not know about — which is how one component ends up talking to a
runtime the rest of the platform thinks is gone.

To point at a locally hosted engine, register it as a model with the **`external`**
runtime and its URL (Models → Register), then point the alias at it. The platform
**refuses a public cloud address**: everything here runs on your own hardware.

### Language

`auto` is the default and should usually stay. Forcing the wrong language does not fail —
it returns fluent, confident nonsense that nothing downstream will catch.

## Privacy

Raw audio is biometric data, and a microphone records whoever is nearby, not only the
person who pressed the button.

- **Audio retention is off by default.** When enabled it goes to MinIO under the
  session's own prefix — never into PostgreSQL (§28).
- **Transcript retention is on**, because a conversation with no memory of its own last
  turn is not a conversation. Turning it off keeps the session and the timings and drops
  what was said.
- A person can delete their own session — `DELETE /api/v1/voice/sessions/{id}` — without
  asking an administrator (§29).

## The protocol

`/ws/v1/voice/{session_id}?token=…` — the token is a query parameter because a browser
cannot set a header on a WebSocket upgrade, and it is verified *before* the socket is
accepted.

**Text frames are control, binary frames are audio.** No ambiguity, and no base64 in
either direction.

| client → server | server → client |
|---|---|
| `AUDIO_START`, `AUDIO_END`, `INTERRUPT`, `SESSION_END` | `SESSION_STARTED`, `LISTENING_STARTED`, `TRANSCRIPT_FINAL`, `AGENT_STARTED`, `TOOL_STARTED`, `TOOL_COMPLETED`, `APPROVAL_REQUIRED`, `RESPONSE_TEXT_FINAL`, `AUDIO_START`, `AUDIO_END`, `AUDIO_UNAVAILABLE`, `ASSISTANT_INTERRUPTED`, `ERROR` |

Audio is PCM, 16-bit, mono, 16 kHz — resampled in the browser, because that is what every
ASR engine resamples to anyway.

## What it degrades to

The assistant is built to fall back rather than fail (§39):

| failure | what happens |
|---|---|
| STT fails | "I couldn't understand the audio" — the session stays open |
| TTS fails | `AUDIO_UNAVAILABLE`; the answer is already on screen as text |
| A tool fails | the card turns red; the agent answers with what it has |
| Approval needed | the run suspends and the UI says where to approve it |

Speech-to-speech degrades to speech-to-text. It does not collapse.

## Observability

Every stage is timed into `voice_events`, because "the assistant felt slow" is not
actionable (§44-45):

```
TURN_COMPLETED  772.89ms   stt 273.98  agent 324.05  tts 174.86
```

`GET /api/v1/voice/sessions/{id}` returns the turns and that event trail.

## The avatar

Two themes, chosen from the switch in the top bar and remembered per browser. Both are
generated in code, so neither costs anything to ship beyond the vendored Three.js.

Both draw the **same head** — `js/head.js` holds the proportions, the outline and the
relief. A second copy of that twenty-one row anatomical table in the second theme would
have drifted the first time either was tuned, leaving two avatars that were recognisably
different people.

### Neural face — the default

~1875 nodes and ~3500 lines, with a jaw driven by the audio.

Four things make it read as a face rather than a decorated ball, each of which replaced an
earlier attempt that measured correctly and still looked wrong:

- **The eyes and mouth are holes**, cut out of the dot field rather than drawn into it. A
  version that made the eyes the *brightest* nodes on the head still read as a mask.
- **The outline is stated explicitly** — a temple pinch, the widest point at the
  cheekbone, a corner at the jaw angle, a flat chin. Seen from the front the silhouette
  *is* the face, and no amount of relief changes it, because relief fades to nothing
  exactly at the edge. An ellipsoid is a ball whatever you draw on it.
- **The proportions are the artist's canon** — a head is 1.4 times as tall as it is wide,
  and the eyes sit halfway between the crown and the chin.
- **The mesh follows the sampling grid.** Wiring each node to its nearest neighbours
  instead spends the whole budget going sideways, and draws horizontal stripes.

The jaw swings about an axis through the ears, so the chin travels furthest and the skin
by the ear barely moves.

### Waveform

A head that never moves, and a band of light across it that carries everything.

| | |
|---|---|
| you speaking | cyan |
| the assistant | violet |
| thinking, using a tool | amber |
| at rest | a flat line |

**The band is the audio, not an animation of it.** Its shape is the frequency spectrum of
whichever stream is live, mirrored about the centre — bass in the middle, sibilants
flicking out towards the tips — so a vowel and an "s" do not look the same. Amplitude
gates the whole thing, which is what makes it settle rather than hold its last pose when
the sound stops.

Pick this one when telling the two speakers apart matters more than expressiveness. A
lip-synced face has to be *right* to convince, and one that is nearly right is worse than
none; a waveform is legible at a glance and honest about what it is showing.

## What is not built yet

- **Partial transcripts** (§9). The platform's STT surface transcribes a whole utterance,
  so there is nothing to stream yet. Emitting fake partials would be theatre.
- **Sentence-level streaming TTS** (§21-22). Same reason: the TTS surface synthesises a
  whole utterance. The seam is `_speak` in `app/api/voice_ws.py` — one function.
- **Server-side VAD** (§4). The browser's press-and-hold decides utterance boundaries
  today; the `vad_enabled` switch is carried through configuration ready for it.
- **Wake word** — explicitly out of scope for the MVP (§17).
