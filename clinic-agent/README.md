# Clinic WhatsApp receptionist agent

A WhatsApp assistant for a single physician's clinic in India. Patients book, view, reschedule
and cancel appointments over WhatsApp in English, Hindi or Marathi. Bookings go into Google
Calendar and get a token number (M-05, E-02) plus an approximate expected time. The live queue
applies fixed fairness rules for late arrivals and no-shows. Reminders, follow-ups,
move-earlier offers and a waitlist are built in, and reception gets a tablet queue board and a
waiting-room TV display.

The full specification is in [`CLAUDE.md`](CLAUDE.md). This README covers running it.

## Free tools used

Everything needed for development and testing is free.

| Need | Tool | Cost |
|---|---|---|
| Language / web | Python 3.12, FastAPI, Uvicorn | free, open source |
| Database | SQLite (zero setup) or PostgreSQL 16 (docker-compose / Supabase free tier) | free |
| WhatsApp (dev) | built-in `/dev/chat` simulator (`WA_MODE=mock`) | free |
| WhatsApp (staging) | Meta WhatsApp Cloud API **test number** (up to 5 verified recipients) | free |
| Calendar (dev) | built-in in-DB fake calendar (`CALENDAR_MODE=fake`) | free |
| Calendar (staging) | Google Calendar API with a service account (no billing needed) | free |
| AI (dev) | built-in rule-based mock (`LLM_MODE=mock`): the full bot works offline | free |
| AI (real) | Claude Haiku 4.5 via the Anthropic API | pay-as-you-go, a few paise per chat; set a spend limit |
| Tunnel to your laptop | `cloudflared tunnel` (free, no account) or ngrok free tier | free |
| Scheduler, encryption, admin UI | APScheduler, cryptography (Fernet), Jinja2 + HTMX (vendored) | free |
| Tests / CI | pytest, respx, GitHub Actions, pip-audit | free |

The only paid item is the real Claude model, and it's optional. With `LLM_MODE=mock` the bot
understands common booking, cancelling, queue and clinic-info requests in all three languages
using rules and dates it parses itself, with no API key.

## Quick start (stage 1, fully offline, about 2 minutes)

```bash
cd clinic-agent
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.dev.example .env          # SQLite + mock WhatsApp + fake calendar + mock LLM
uvicorn app.main:app --reload
```

Open:

- **http://localhost:8000/dev/chat**: the WhatsApp simulator. Pick a fake number, type "Hi",
  and tap the buttons and lists. Every message is signed and posted to the real webhook, so the
  whole path (signature check, parser, router, tools, booking, outbox) runs.
- **http://localhost:8000/admin**: staff UI (dev login `admin` / `admin`). Includes appointments,
  the queue board, follow-ups, the handoff inbox, manual booking, and session tools.
- **http://localhost:8000/dev**: dev tools. Freeze or fast-forward the clinic clock, run the
  reminder jobs now, add "doctor busy" blocks to the fake calendar, and run the queue simulator.
- **http://localhost:8000/queue/display/dev-display-key**: waiting-room TV. It shows tokens only.

Optional: `python -m scripts.seed_dev_data` adds fake patients and bookings.

**To talk to real Claude**, set `LLM_MODE=live` and `ANTHROPIC_API_KEY=...` in `.env`.

### With Docker and Postgres

```bash
cp .env.dev.example .env
docker compose up --build        # app + postgres 16; the app's DATABASE_URL points at the db service
```

### Tests

```bash
pytest -q                                                    # 147 tests, offline, SQLite
TEST_DATABASE_URL=postgresql+asyncpg://user:pw@localhost:5432/clinic_test pytest -q   # same suite on Postgres
pytest -q --cov=app.calendar --cov=app.scheduling --cov=app.agent.router            # ~90%
ANTHROPIC_API_KEY=... pytest -m live_llm                     # opt-in: real Claude conversations
pytest -m staging                                            # opt-in: real Meta test number / Google calendar
pip-audit --strict --skip-editable
```

Tests never sleep. They move `app/dev/clock.py` forward instead. Among other things they cover:

- 20 concurrent bookings on one slot, where exactly one wins.
- Every section 10A queue rule and every section 10B change rule.
- Reminder idempotency.
- The six required conversations: Hindi, Marathi, English, medical-advice refusal, emergency
  (no LLM call), and prompt injection.
- The contract suites that the fake and real WhatsApp and calendar clients share.

## Stage 2: real free test accounts

Everything in stage 1 keeps working. Switch with `.env.staging.example`.

1. **WhatsApp (Meta for Developers)**. Create a Business app and add the WhatsApp product. Meta
   gives you a free test number. Under API Setup, add up to 5 of your own numbers as recipients
   (each is verified by OTP) and copy the temporary token. The token lasts about 24h; for longer
   testing, create a System User token with `whatsapp_business_messaging` and
   `whatsapp_business_management`. Put your own numbers in `DEV_ALLOWED_NUMBERS`. The client
   refuses to message any other number in dev and staging.
2. **Tunnel**: run `cloudflared tunnel --url http://localhost:8000`. In the Meta app, set the
   webhook to `https://<tunnel>/webhook/whatsapp`, set the verify token to `WA_VERIFY_TOKEN`,
   and subscribe to `messages`. Set `WA_APP_SECRET` to the app secret; signatures are checked
   on every POST.
3. **Templates**: submit the templates listed below (category UTILITY, in en, hi and mr). Until
   they're approved, staging falls back to Meta's `hello_world` and logs a warning.
4. **Google Calendar**:
   - Use a separate test Google account and create a calendar named "Clinic TEST".
   - Create a Google Cloud project, enable the Calendar API, and create a service account. Save
     its JSON key to `secrets/gcal-sa.json`.
   - Share "Clinic TEST" with the service account email and give it "Make changes to events".
   - Put the calendar ID in `GOOGLE_CALENDAR_ID`.
   - The app uses only the `calendar.events` scope.
5. **Claude**: in the Anthropic Console, create a workspace `clinic-agent-dev` with its own key
   and a low monthly spend limit.
6. **Database**: use a free Supabase project, or keep local Postgres. Run `alembic upgrade head`.

## Production (stage 3)

Use `.env.prod.example` with new keys, a new number, the doctor's real calendar and a new
service account. The app **refuses to start** in prod if any mock or fake mode is on, if
`DEV_ALLOWED_NUMBERS` is set, or if a dev default secret is still in place. Deploy the Docker
image to Cloud Run (min instances = 1 so the scheduler runs) or to a small VPS behind
Caddy/Nginx with TLS. The container runs `alembic upgrade head` on start. Monitor `/health`.
The full checklist is in CLAUDE.md section 16.

## Choices made on your behalf (CLAUDE.md section 18)

I used the spec's example values. Change them in `app/clinic_config.yaml` (no code changes
needed):

- Hours Mon–Fri 10–13 and 17–20, Sat 10–13, Sunday closed. 15-minute slots, fee ₹500. The clinic
  name, address and phone are placeholders.
- `token_mode: slot` (recommended). Tokens are stable and gaps are allowed, so patients never get
  renumbering messages. `sequential` is fully implemented if you prefer tokens with no gaps.
- Grace period 10 min. A late patient is seated after 2 waiting patients. Anyone more than 45 min
  late goes to the end. An on-time patient can be pushed back at most 2 times.
- Move-earlier offers and the waitlist are on. The waiting-room TV display is included.
- Stage 1 (offline) is the default. Stage 2 needs your Meta, Google and Anthropic credentials.

## Design notes and small deviations

- **Emergency check before the consent gate.** Emergency guidance (call 108/112) reaches even a
  first-time sender. It stores nothing and never calls the LLM.
- **Doctor delay** is stored as "doctor expected at" (`delay_until`). ETAs start from it, and
  grace is measured from `max(slot, delay_until, ETA we told the patient)`. A declared delay
  never makes a patient "late". The first "Call next" of the day is not treated as a delay.
- **"Call next" is idempotent.** The board sends the id of the patient it showed as "with the
  doctor", so a double tap by two receptionists calls only one patient. Postgres uses
  `pg_advisory_xact_lock` for the session and slot. SQLite (dev only) uses in-process locks.
- **Early arrivals** are called before their slot only when nobody ahead is still inside their
  grace period (rule 8). Walk-ins go after everyone who is waiting or whose time has come.
- **Busy blocks** are read with `events.list` rather than `freebusy.query`. This drops the app's
  own appointment events (which the database tracks) and needs only the `calendar.events` scope.
- **The token unique index** covers live appointments only. A cancelled token keeps its label for
  display, and a re-booked slot reuses the same token (section 10A.1).
- **Rendering at send time.** Outbox messages are built from the current database state. A
  reminder queued before a reschedule never shows the old token or time.
- **Outside the 24h window** only approved templates can be sent. Text that exists only in
  free-form messages (the late-arrival rule line on confirmations, and the "confirm within 12h"
  note for repeat no-shows) is therefore added only inside the window.
- **Conversation context** keeps at most 10 turns and is cleared after 24h. PII (names, phones,
  visit reasons) is Fernet-encrypted, lookups use HMAC phone hashes, and logs mask numbers.

## Project layout

```
app/
  main.py, config.py, clinic_config.yaml, scheduler.py, timeutil.py, logging_setup.py, ratelimit.py
  db/          models.py, session.py (engine, advisory locks), crypto.py
  whatsapp/    webhook.py, signature.py, client.py (all send guards), parser.py, templates.py
  agent/       router.py, flows.py, llm.py, mock_llm.py, tools.py, prompts.py, safety.py, state.py, dates.py
  calendar/    base.py, gcal.py, slots.py
  scheduling/  booking_service.py, tokens.py, queue_service.py, change_service.py, offers.py,
               reminders.py (jobs), outbox.py, notifications.py, common.py
  admin/       routes.py, queue_routes.py, auth.py, templates/
  dev/         simulator.py (/dev/*), fake_whatsapp.py, fake_calendar.py, clock.py, templates/
  i18n/        en.json, hi.json, mr.json
alembic/       migrations (0001 initial schema)
scripts/       seed_dev_data.py, send_test_webhook.py
tests/         147 tests
```

## WhatsApp templates to submit (copy-paste)

Category **UTILITY**. Create each template in `en`, `hi` and `mr`. Names must match exactly.
Quick-reply button payloads are set at send time.

### `appt_confirmation` (UTILITY)

Variables: {{1}} name, {{2}} date, {{3}} token, {{4}} expected time, {{5}} report-by time, {{6}} doctor

- **en**: Hello {{1}}, your appointment on {{2}} is confirmed. Token: {{3}}. Expected time: approx {{4}}. Please report by {{5}}. Doctor: {{6}}.
- **hi**: नमस्ते {{1}}, {{2}} को आपका अपॉइंटमेंट कन्फ़र्म है। टोकन: {{3}}। अनुमानित समय: लगभग {{4}}। कृपया {{5}} तक पहुँचें। डॉक्टर: {{6}}।
- **mr**: नमस्कार {{1}}, {{2}} रोजी तुमची अपॉइंटमेंट निश्चित झाली आहे. टोकन: {{3}}. अंदाजे वेळ: सुमारे {{4}}. कृपया {{5}} पर्यंत या. डॉक्टर: {{6}}.

### `appt_reminder_24h` (UTILITY)

Variables: {{1}} name, {{2}} date, {{3}} token, {{4}} expected time

- **en**: Reminder: {{1}}, you have an appointment on {{2}}. Token: {{3}}. Expected time: approx {{4}}.
  - Quick replies: Confirm / Reschedule / Cancel
- **hi**: रिमाइंडर: {{1}}, {{2}} को आपका अपॉइंटमेंट है। टोकन: {{3}}। अनुमानित समय: लगभग {{4}}।
  - Quick replies: कन्फ़र्म / समय बदलें / रद्द करें
- **mr**: आठवण: {{1}}, {{2}} रोजी तुमची अपॉइंटमेंट आहे. टोकन: {{3}}. अंदाजे वेळ: सुमारे {{4}}.
  - Quick replies: निश्चित / वेळ बदला / रद्द करा

### `appt_reminder_2h` (UTILITY)

Variables: {{1}} name, {{2}} token, {{3}} current expected time, {{4}} address

- **en**: {{1}}, your appointment is today. Token: {{2}}. Current expected time: approx {{3}}. Address: {{4}}.
  - Quick replies: On my way / Running late / Cancel
- **hi**: {{1}}, आज आपका अपॉइंटमेंट है। टोकन: {{2}}। अभी का अनुमानित समय: लगभग {{3}}। पता: {{4}}।
  - Quick replies: आ रहा/रही हूँ / देर हो रही है / रद्द करें
- **mr**: {{1}}, आज तुमची अपॉइंटमेंट आहे. टोकन: {{2}}. सध्याची अंदाजे वेळ: सुमारे {{3}}. पत्ता: {{4}}.
  - Quick replies: येत आहे / उशीर होतोय / रद्द करा

### `queue_turn_soon` (UTILITY)

Variables: {{1}} token, {{2}} patients ahead, {{3}} expected time

- **en**: Token {{1}}: your turn is coming soon. Patients ahead: {{2}}. Expected time: approx {{3}}.
- **hi**: टोकन {{1}}: आपकी बारी जल्द आने वाली है। आगे मरीज़: {{2}}। अनुमानित समय: लगभग {{3}}।
- **mr**: टोकन {{1}}: तुमचा नंबर लवकरच येईल. आधी रुग्ण: {{2}}. अंदाजे वेळ: सुमारे {{3}}.

### `queue_delay_notice` (UTILITY)

Variables: {{1}} token, {{2}} new expected time

- **en**: Token {{1}}: the clinic is running late. Your new expected time is approx {{2}}.
  - Quick replies: Still coming / Cancel
- **hi**: टोकन {{1}}: क्लिनिक में देरी चल रही है। आपका नया अनुमानित समय लगभग {{2}} है।
  - Quick replies: आ रहा/रही हूँ / रद्द करें
- **mr**: टोकन {{1}}: क्लिनिकमध्ये उशीर होत आहे. तुमची नवीन अंदाजे वेळ सुमारे {{2}} आहे.
  - Quick replies: येत आहे / रद्द करा

### `queue_missed_turn` (UTILITY)

Variables: {{1}} token, {{2}} what happens on arrival

- **en**: Token {{1}}: we called your turn but you were not at the clinic. {{2}}
  - Quick replies: Coming now / Reschedule
- **hi**: टोकन {{1}}: आपकी बारी आई पर आप क्लिनिक में नहीं थे। {{2}}
  - Quick replies: अभी आ रहा/रही हूँ / समय बदलें
- **mr**: टोकन {{1}}: तुमचा नंबर आला पण तुम्ही क्लिनिकमध्ये नव्हता. {{2}}
  - Quick replies: आत्ता येत आहे / वेळ बदला

### `appt_no_show` (UTILITY)

Variables: {{1}} name, {{2}} date

- **en**: {{1}}, we missed you at your appointment on {{2}}. Would you like to book again?
  - Quick replies: Book again
- **hi**: {{1}}, {{2}} के अपॉइंटमेंट पर आप नहीं आ पाए। क्या आप फिर से बुक करना चाहेंगे?
  - Quick replies: फिर से बुक करें
- **mr**: {{1}}, {{2}} च्या अपॉइंटमेंटला तुम्ही येऊ शकला नाहीत. पुन्हा बुक करायचे आहे का?
  - Quick replies: पुन्हा बुक करा

### `followup_reminder` (UTILITY)

Variables: {{1}} name, {{2}} doctor, {{3}} due date

- **en**: Hello {{1}}, {{2}} asked for a follow-up visit around {{3}}. Would you like to book it?
  - Quick replies: Book now / Not now
- **hi**: नमस्ते {{1}}, {{2}} ने {{3}} के आसपास फ़ॉलो-अप विज़िट के लिए कहा था। क्या आप बुक करना चाहेंगे?
  - Quick replies: अभी बुक करें / अभी नहीं
- **mr**: नमस्कार {{1}}, {{2}} यांनी {{3}} च्या आसपास फॉलो-अप भेटीसाठी सांगितले होते. बुक करायचे आहे का?
  - Quick replies: आत्ता बुक करा / आत्ता नको

### `appt_cancelled` (UTILITY)

Variables: {{1}} name, {{2}} date, {{3}} token

- **en**: {{1}}, your appointment on {{2}} (token {{3}}) has been cancelled.
  - Quick replies: Book again
- **hi**: {{1}}, {{2}} का आपका अपॉइंटमेंट (टोकन {{3}}) रद्द कर दिया गया है।
  - Quick replies: फिर से बुक करें
- **mr**: {{1}}, {{2}} ची तुमची अपॉइंटमेंट (टोकन {{3}}) रद्द करण्यात आली आहे.
  - Quick replies: पुन्हा बुक करा

### `appt_rescheduled` (UTILITY)

Variables: {{1}} name, {{2}} new date, {{3}} new token, {{4}} new expected time, {{5}} report-by time

- **en**: {{1}}, your appointment has moved to {{2}}. New token: {{3}}. Expected time: approx {{4}}. Please report by {{5}}.
- **hi**: {{1}}, आपका अपॉइंटमेंट {{2}} पर बदल गया है। नया टोकन: {{3}}। अनुमानित समय: लगभग {{4}}। कृपया {{5}} तक पहुँचें।
- **mr**: {{1}}, तुमची अपॉइंटमेंट {{2}} वर बदलली आहे. नवीन टोकन: {{3}}. अंदाजे वेळ: सुमारे {{4}}. कृपया {{5}} पर्यंत या.

### `slot_offer_earlier` (UTILITY)

Variables: {{1}} name, {{2}} current token + time, {{3}} earlier time, {{4}} minutes to reply

- **en**: {{1}}, an earlier time has opened up. Your current booking: {{2}}. Earlier time available: {{3}}. Reply within {{4}} minutes if you would like to move.
  - Quick replies: Yes, move me / No, keep mine
- **hi**: {{1}}, पहले का एक समय खाली हुआ है। आपकी मौजूदा बुकिंग: {{2}}। पहले का समय: {{3}}। बदलना हो तो {{4}} मिनट में जवाब दें।
  - Quick replies: हाँ, बदल दें / नहीं, मेरा रखें
- **mr**: {{1}}, आधीची एक वेळ रिकामी झाली आहे. तुमची सध्याची बुकिंग: {{2}}. आधीची वेळ: {{3}}. बदलायचे असल्यास {{4}} मिनिटांत उत्तर द्या.
  - Quick replies: हो, बदला / नको, माझी ठेवा

### `waitlist_offer` (UTILITY)

Variables: {{1}} name, {{2}} date, {{3}} time, {{4}} minutes to reply

- **en**: {{1}}, a slot has opened on {{2}} at {{3}}. Reply within {{4}} minutes to book it.
  - Quick replies: Book it / No thanks
- **hi**: {{1}}, {{2}} को {{3}} पर एक स्लॉट खाली हुआ है। बुक करने के लिए {{4}} मिनट में जवाब दें।
  - Quick replies: बुक करें / नहीं, धन्यवाद
- **mr**: {{1}}, {{2}} रोजी {{3}} वाजता एक स्लॉट रिकामा झाला आहे. बुक करण्यासाठी {{4}} मिनिटांत उत्तर द्या.
  - Quick replies: बुक करा / नको, धन्यवाद

### `token_updated` (UTILITY)

Variables: {{1}} name, {{2}} old token, {{3}} new token, {{4}} expected time

- **en**: {{1}}, your token number has changed from {{2}} to {{3}}. Your expected time is unchanged: approx {{4}}.
- **hi**: {{1}}, आपका टोकन नंबर {{2}} से बदलकर {{3}} हो गया है। आपका अनुमानित समय वही है: लगभग {{4}}।
- **mr**: {{1}}, तुमचा टोकन नंबर {{2}} वरून {{3}} झाला आहे. तुमची अंदाजे वेळ तीच आहे: सुमारे {{4}}.

### `session_cancelled_by_clinic` (UTILITY)

Variables: {{1}} name, {{2}} date, {{3}} session

- **en**: {{1}}, we are sorry: the doctor is unavailable on {{2}} ({{3}}), so your appointment is cancelled. Would you like to reschedule?
  - Quick replies: Reschedule / Cancel
- **hi**: {{1}}, क्षमा करें: डॉक्टर {{2}} ({{3}}) को उपलब्ध नहीं हैं, इसलिए आपका अपॉइंटमेंट रद्द है। क्या आप समय बदलना चाहेंगे?
  - Quick replies: समय बदलें / रद्द करें
- **mr**: {{1}}, क्षमस्व: डॉक्टर {{2}} ({{3}}) रोजी उपलब्ध नाहीत, त्यामुळे तुमची अपॉइंटमेंट रद्द झाली आहे. वेळ बदलायची आहे का?
  - Quick replies: वेळ बदला / रद्द करा

