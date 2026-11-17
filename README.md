# Munim-ji — a hands-free voice khata for kirana stores

**BolBharat · Gnani Voice AI Innovation Challenge · PS2 (Voice Khata)**

A shopkeeper holds one button and says *"Ramesh ka 340 udhaar, kal 200 diya tha"*.
Munim-ji writes two ledger lines (₹340 credit today, ₹200 payment yesterday), says the new
balance back in one breath, and is ready for the next customer. Mistakes are fixed by voice
(*"nahi, 430"*), questions are answered by voice (*"Ramesh ka total?"*), and the screen never
needs to be touched.

Munim-ji is an AI assistant and says so in its first turn, in the user's language.

---

## What it does

| Requirement in the problem statement | How Munim-ji meets it |
|---|---|
| **Numbers under noise**: "teen sau chaalis", "340", "three-forty" | Streaming Prisma v3.0, then a number normaliser that understands digits, Hindi words (all of 0–99), fractions (*dedh sau*, *dhai hazaar*, *saade teen sau*, *paune do sau*) and English forms. Browser noise suppression plus tuned server VAD. |
| **Ledger logic**: customer, amount, credit or payment, relative dates | Rule parser for Hindi/Hinglish/English; LLM fallback for the other languages. *"ne … diye"* is a payment, *"ko … ka saamaan"* is credit. *kal*, *parso*, *3 din pehle* are resolved to dates. |
| **Spoken corrections**: "nahi, 430" | Fixes amount, name, direction or date of the last entry. After a two-line entry, *"nahi, 430"* fixes the amount it sounds like (340, not 200). *"hata do"* removes the entry. |
| **Two customers share a name** | Asks one short question: *"Kaun se? Doodh wale Ramesh Kumar ya Ramesh Gupta?"* Answer with a descriptor, a surname, or *"pehla / doosra"*. |
| **Spoken answers within 2 s** | Queries are plain SQL and replies come from fixed templates, so no LLM is in the path. |
| **Channel**: low-end Android or browser, push-to-talk or wake phrase | Installable PWA with no framework (about 25 KB of JS). Hold-to-talk, or hands-free with the wake phrase *"Munim ji"*. |

Beyond the brief:

- **The ledger trains the ear.** Customer names from the khata are sent to Prisma as `keyterms`, so name recognition improves as the ledger grows.
- **Tense-aware promises.** *"Ramesh kal 200 de dega"* is future tense, so it is noted as a promise and the balance does not change.
- **Entries are never overwritten.** A correction strikes the old line through and adds a new one, like a paper khata. The Entries tab shows the audit trail.
- **Ten languages.** Replies in Hindi, English, Kannada, Tamil, Telugu, Marathi, Bengali, Gujarati, Punjabi and Malayalam, following the language the user speaks.

Built for a noisy counter:

- **Measured under shop noise.** Spoken entries are mixed with customer chatter, traffic and a horn, then run through the real speech stack: 24/24 correct at 10 dB, 22/24 at 5 dB ([results](#audio-eval-under-shop-noise)).
- **One hold is one utterance.** A pause mid-sentence makes the recogniser split *"340 udhaar … kal 200 diya tha"* in two. Everything heard while the button is down is joined before it is understood.
- **A line with no name follows the last customer.** In hands-free mode, *"kal 200 diya tha"* said right after Suresh's entry goes to Suresh. It needs a cue (*kal*, *parso*, *aur*); a bare *"200 jama"* still gets the question *"kiske khate mein?"*.
- **Said twice is not written twice.** People repeat themselves over noise. The same line within 90 seconds gets *"abhi likha hai. Ek aur likhun?"* instead of a second entry.
- **"Dobara bolo" and "Madad".** The last reply is repeated on request, and help gives two examples in the user's language.
- **Udhaar and jama sound different.** A falling tone for credit, a rising one for a payment, so the direction is heard even when the spoken reply is not.
- **A customer's page by voice.** *"Ramesh ka khata sunao"* reads the last three lines and opens the page, with the balance after every line like a paper khata.
- **Promises close themselves.** *"Vaade batao"* lists who promised to pay and when. When that payment arrives, the promise is marked kept.
- **Barge-in that knows the room.** In hands-free mode, "loud enough to interrupt" is measured against the running noise level, so street noise alone does not cut the agent off.
- **Speed on screen.** The menu shows the session's median, fastest and slowest time from end of speech to first audio.
- **CSV export.** The whole khata downloads as a spreadsheet file for a backup or an accountant.

## Architecture

```mermaid
flowchart LR
    subgraph Phone["Android phone · Chrome PWA"]
        MIC["Mic → AudioWorklet<br/>16 kHz PCM16, 32 ms frames"]
        SPK["Speaker<br/>streamed PCM playback"]
        UI["Ledger screen<br/>earcons + haptics"]
    end

    subgraph Server["Python server · FastAPI"]
        SES["Session<br/>turn-taking, barge-in, metrics"]
        NLU["Brain<br/>1 number normaliser<br/>2 rule parser<br/>3 LLM fallback + validator<br/>4 dialogue state"]
        LED[("Encrypted ledger<br/>SQLite + AES-256-GCM")]
        TPL["Reply templates<br/>10 languages"]
    end

    subgraph Gnani["Gnani API Platform"]
        STT["Prisma v3.0<br/>streaming STT"]
        TTS["Timbre v3.0<br/>streaming TTS"]
    end

    LLM["LLM (free tier)<br/>Groq, OpenAI-compatible"]

    MIC -- "WebSocket: audio" --> SES
    SES -- "WebSocket: PCM frames, commit" --> STT
    STT -- "final transcript" --> SES
    SES --> NLU
    NLU <--> LED
    NLU -. "only when rules are unsure" .-> LLM
    NLU --> TPL --> SES
    SES -- "WebSocket: text" --> TTS
    TTS -- "audio chunks" --> SES
    SES -- "WebSocket: audio + events" --> SPK
    SES --> UI
```

### Gnani API Platform services used

| Service | Model | Endpoint | Used for |
|---|---|---|---|
| Speech-to-text, realtime | **Prisma v3.0** | `wss://api.vachana.ai/stt/v3/stream` | Streaming transcription with `keyterms`, language hints, server VAD and `commit` |
| Text-to-speech, realtime | **Timbre v3.0** | `wss://api.vachana.ai/api/v1/tts` | Streaming speech for every reply |

Every connection is a WebSocket. There is no telephony, no phone number and no voice cloning.
Orchestration is custom code in [munim/session.py](munim/session.py); no hosted agent platform is used.

### Why it is fast

Time to first audio is measured on every turn (end of user speech to first reply audio) and
shown under the transcript on screen.

1. **`commit` on button release.** Prisma sends only final transcripts, after its silence timer. Releasing the button sends `commit`, which ends the utterance at once.
2. **No LLM on the common path.** Hindi, Hinglish and English entries, corrections and all queries are parsed by rules in a few milliseconds.
3. **One-sentence replies.** Timbre takes a whole sentence per request, so replies are short templates.
4. **Warm sockets.** STT, TTS and the LLM connection are opened when the session starts. After a barge-in closes the TTS socket, a new one is opened immediately.
5. **Barge-in stops audio on the phone first**, then tells the server, so the agent goes quiet without a network round trip.

## Setup

Requires Python 3.11 or newer.

```bash
git clone https://github.com/sanjaysaini383/Bol_Bharat.git
cd Bol_Bharat
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then fill in the keys below
python -m munim
```

Open <http://localhost:8000>, press **Shuru karein**, allow the microphone, then hold the
button and speak.

### Environment variables

| Variable | Required | What it is |
|---|---|---|
| `GNANI_API_KEY` | **Yes** | Your Gnani API Platform key (Prisma v3.0 + Timbre v3.0). |
| `LLM_API_KEY` | Recommended | Key for any OpenAI-compatible chat API. The default is Groq's free tier: <https://console.groq.com/keys>. Without it, Hindi/Hinglish/English still work through rules; the other eight languages need it. |
| `LLM_BASE_URL` | No | Default `https://api.groq.com/openai/v1`. |
| `LLM_MODEL` | No | Default `openai/gpt-oss-120b` (about 0.7 s on Groq). `qwen/qwen3.8-27b` is faster (about 0.5 s) with `LLM_REASONING_EFFORT=none`. Groq retires models; `GET /models` lists the current ones. |
| `LLM_REASONING_EFFORT` | No | Default `low`. Sent only when set; leave it empty for models that do not take it. |
| `KHATA_ENC_KEY` | No | 32-byte base64 key for encryption at rest. If empty, one is generated into `data/khata.key`. |
| `ACCESS_TOKEN` | No | If set, the app needs `?k=<token>` in the URL. Set this on a public demo link. |
| `SHOP_NAME`, `DEFAULT_LANG`, `GNANI_TTS_VOICE`, … | No | See [.env.example](.env.example) for the full list. |

`.env` is in `.gitignore`. No key is ever committed.

### Using it on a phone

Browsers only allow the microphone on `https://` or `localhost`. Either deploy the app
(below) or tunnel your laptop, for example `cloudflared tunnel --url http://localhost:8000`,
and open the HTTPS link on the phone. Then use **Add to Home screen**.

### Deploy

```bash
docker build -t munim-ji .
docker run -p 8000:8000 -v munim-data:/data \
  -e GNANI_API_KEY=... -e LLM_API_KEY=... -e ACCESS_TOKEN=choose-a-token munim-ji
```

Any host that gives HTTPS and WebSockets works (Render, Railway, Fly.io). Choose an India
region to keep the round trip to Gnani short.

### First-run checklist

The speech clients were checked against the live Gnani API on 2026-11-16, using Timbre audio
fed into the app as microphone input. Prisma v3.0 returned transcripts in about 110 ms, and
end of speech to first reply audio was about 0.7 s for Hindi (rules) and 1.3 s for Kannada
(LLM). Where the live service differs from the published docs, the code follows the live
service: one `language_code` per stream (a list is rejected), two `complete` messages per
TTS reply, and no `processing` event.

That check used synthetic speech. Before a demo, confirm with your own voice and key:

1. Record a short 16 kHz mono WAV of your own voice and run `python scripts/probe_stt.py clip.wav`. You should see the transcript and what the agent understood.
2. Compare number output: `--format transcribe` (digits, the default) against `--format codemix`.
3. In the app, check the *"suna → bola"* time under the transcript on a few turns.
4. Note how many credits one minute of talking uses, so 5,000 credits last through judging.

## What to say

| You say | Munim-ji does |
|---|---|
| "Ramesh ka 340 udhaar, kal 200 diya tha" | Two lines: ₹340 credit today, ₹200 payment yesterday |
| "Sunita ne dedh sau diye" | ₹150 payment |
| "Suresh ko 200 ka saamaan diya" | ₹200 credit |
| "Suresh 150" | Asks: *"Udhaar ya jama?"* |
| "Nahi, 430" / "Nahi, jama tha" / "Nahi, Sunita" | Corrects amount / direction / customer |
| "Hata do" | Removes the last entry |
| "Ramesh ka total?" | Balance for Ramesh |
| "Aaj kitna udhaar gaya?" · "Aaj ka hisaab" | Today's credit · today's credit and payments |
| "Sabse zyada kiska baaki hai?" | Top three dues |
| "Ramesh kal 200 de dega" | Notes a promise; balance unchanged |
| "Sunita reminder ke liye raazi hai" | Records reminder consent |
| "Sunita ko reminder bhejo" | Prepares a polite reminder to share (only with consent) |
| "Mera sara data delete karo" | Asks for confirmation, then wipes everything |
| "Ramesh ka khata sunao" | Reads Ramesh's last three lines and opens his page |
| "Vaade batao" · "Aaj kiska vaada hai?" | Lists open promises to pay |
| "Dobara bolo" · "Kya bola?" | Repeats the last reply |
| "Madad" | Says what you can say, with two examples |
| The same entry again within 90 s | Asks *"Abhi likha hai. Ek aur likhun?"* |

In hands-free mode, start with *"Munim ji"*. For eight seconds after a reply, and whenever
Munim-ji has asked a question, the wake phrase is not needed.

## Responsible AI and compliance

| Rule | Implementation |
|---|---|
| **AI disclosure** | The first turn of every session says *"…main Munim-ji hoon, aapka AI khata sahayak"*. If the user switches language, the first reply in the new language discloses again. The start screen states it as well. Tested in `test_first_turn_discloses_ai_in_users_language`. |
| **Synthetic data only** | Demo customers in [munim/seed.py](munim/seed.py) are invented. Use your own voice for test audio. |
| **Encrypted at rest** | Customer names, descriptors and transcripts are encrypted with AES-256-GCM before they are written to SQLite ([munim/crypto.py](munim/crypto.py)). A test reads the raw database file and checks no name appears in it. |
| **Delete after judging** | Transcripts expire after `TRANSCRIPT_RETENTION_HOURS` (default 72) and are purged hourly. `python scripts/purge.py` or the menu button deletes them at once; `--all` or the voice command deletes the ledger too. |
| **No lending** | Requests for interest, loans, late fees or penalties are refused in the user's language. The ledger has no field for interest. |
| **No pressure language** | Requests to threaten or shame a customer are refused. Reminders use one fixed polite template per language, and the final text is still checked against a pressure-word list ([munim/guardrails.py](munim/guardrails.py)). |
| **Opt-in reminders only** | A reminder is produced only when the customer's consent is recorded, and the shopkeeper sends it himself through the phone's share sheet. Nothing is sent automatically. |
| **LLM limits** | The LLM only extracts structure. It never writes a spoken reply, never sees the customer list, and any amount it returns must appear in the transcript or the result is discarded. |
| **Secrets** | Keys live in `.env`, which is git-ignored. |
| **Voices** | Stock Timbre v3.0 voices only. No cloning. |

## Tests and evaluation

```bash
pip install -r requirements-dev.txt
pytest                         # 202 unit, conversation, session and protocol tests
python scripts/eval_text.py    # 40 scripted conversations, judged like the live test
python scripts/eval_audio.py   # spoken entries under shop noise (uses Gnani credits once)
```

Current result of the conversation eval (rules only, no LLM): **40 / 40** on exact amount,
credit/payment direction and customer. These run on text, so they measure understanding,
not speech recognition.

### Audio eval under shop noise

`scripts/eval_audio.py` speaks 24 conversations with Timbre v3.0, mixes in shop noise
(overlapping customer chatter, traffic rumble, a horn), streams them through Prisma v3.0 as
push-to-talk holds with `language_code=indic-auto`, and scores the ledger. Run on 2026-11-17:

| Noise | Conversations correct | Amounts exact | Wrong line written |
|---|---|---|---|
| None | 24 / 24 | 24 / 24 | 0 |
| 10 dB SNR | 24 / 24 | 24 / 24 | 0 |
| 5 dB SNR | 22 / 24 | 22 / 24 | 0 |
| 0 dB SNR (noise as loud as the speech) | 7 / 24 | 7 / 24 | 1 |

The cases cover *teen sau chaalis*, *three forty*, *dedh sau*, *saade chaar sau*, *sava do
sau*, hard two-digit words (39, 67, 89), corrections, a shared name, a new customer, English,
and one Kannada and one Marathi entry through the LLM. Full output, with every failure:
[docs/audio-eval.md](docs/audio-eval.md).

Read the table with two limits in mind. The speech is synthetic and comes from the same
vendor as the recogniser, so real voices and phone microphones will score lower. And at 0 dB
the recogniser starts writing down the background chatter. In all but one of those runs no
wrong line reached the ledger: Munim-ji asked a question or did not write, which is the
failure a shopkeeper can hear and retry. The eval also found bugs that are now fixed, such as *"sava do sau"* heard as
*"सवाल 200"* and saved as 200.

Speech and transcripts are cached under `eval/audio/` (git-ignored), so a re-run after a
code change is free. For your own recordings use `scripts/probe_stt.py` and
`scripts/mix_noise.py`.

## Project layout

```
munim/
  textnorm.py    one romaniser for all 10 scripts; spelling-tolerant matching keys
  numbers.py     spoken amounts → integers
  parser.py      rule parser: entries, corrections, queries, guardrail intents
  llm.py         LLM fallback (OpenAI-compatible) and output validation
  dialogue.py    dialogue manager: the only code that changes the ledger
  ledger.py      encrypted SQLite khata with an audit trail
  replies.py     reply templates in 10 languages
  guardrails.py  reminder consent and pressure-language gate
  gnani_stt.py   Prisma v3.0 streaming client
  gnani_tts.py   Timbre v3.0 streaming client
  session.py     real-time loop per browser: audio in, audio out, barge-in
  server.py      FastAPI app, static files, WebSocket endpoint
web/             the PWA (HTML, CSS, vanilla JS, AudioWorklet)
scripts/         eval, STT probe, noise mixer, purge
tests/           pytest suite
```

## Known limits

- Number **words** are understood in Hindi and English. In the other languages Munim-ji relies on Prisma writing digits (`format=transcribe`) or on the LLM fallback.
- Reply templates outside Hindi and English were not reviewed by native speakers.
- One shop per server. There are no user accounts; protect a hosted demo with `ACCESS_TOKEN`.
- New customer names reach the recogniser's `keyterms` at the next session start, not mid-session.
- In **Auto** language mode the recogniser is given Hindi hint words and names, which can pull Kannada or Marathi speech into Hindi words. Picking the language in the app sends hints in that language instead, and the entry is then read correctly.
- Noise as loud as the speaker (0 dB) is beyond what the pipeline handles: 7 of 24 test conversations survive it.
- The duplicate guard and the carry-over of a customer to a nameless line are per session and reset when the page reloads.
