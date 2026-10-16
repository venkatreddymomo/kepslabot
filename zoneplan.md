# Zone bot — review + change plan (2026-08-10)

Review only. Nothing implemented. Every item below has a verified root cause with
file:line refs, plus what I'd change, blast radius, and what still needs your call.

**Hard constraint carried through every item:** the guest UI is ONE component —
`admin_ui/src/pages/ChatHarness.tsx` — served both as the harness page and as the
iframe embed (`?embed=1`, mounted by `static/embed/kebo-widget-loader.js`). So every
frontend change lands in both automatically, and must be verified in both at phone
and laptop widths. No change should be made in the loader that the harness doesn't get.

---

## Item map

| # | Ask | Layer | Root cause found | Risk |
|---|-----|-------|------------------|------|
| 1 | Kill the welcome bubble, keep only the intro form | Frontend | Form is gated ON the welcome bubble existing | Low, but 4 injection sites |
| 2 | Shorten replies | Prompt + content | Brevity directive actively *demands* length | Medium — can regress coverage rules |
| 3 | Stop re-asking travel preference after recommending | Content (DB) | Hardcoded tail in all 21 curated intro rows | Very low |
| 5 | Multi-hotel → give unique points + choice buttons | Content (DB) + small FE wiring | `location_intro` returns `suggested_actions=[]` | Low |
| 6 | On hotel select → auto room categories + images (façade/room first, all hotels) | Backend | Images only attach on literal title mentions | Medium — needs image-category audit |
| 7 | Accurate distances to key places | Backend (new tool) | No geo tool exists; grounding rule blocks guessing | High — new dependency + cost |
| 8 | Chips cut off on laptop | Frontend | `useIsMobile()` measures the *iframe* width | Low, high value |
| 9 | Add "assistance from team" button + form → edit existing ticket | FE + BE | Only one CTA today; no edit path from a form | Medium |

(No item 4 — your list skips it.)

---

## 1. Fresh session shows two messages; only the form should show

**Where:** [ChatHarness.tsx:2540-2574](admin_ui/src/pages/ChatHarness.tsx#L2540-L2574)
injects the welcome bubble; [ChatHarness.tsx:712-766](admin_ui/src/pages/ChatHarness.tsx#L712-L766)
builds its text (the multi-property branch at line 737-740 is the exact
*"Hi there! How can I assist you today? Please choose the location…"* string in your
screenshot). The form card is at [ChatHarness.tsx:4711-4748](admin_ui/src/pages/ChatHarness.tsx#L4711-L4748).

**The catch — this is not a one-line delete.** The form only renders when
`welcomeCtaEligible` is true ([:4711](admin_ui/src/pages/ChatHarness.tsx#L4711)), and that
depends on `hasOnlyWelcomeMessage` ([:3007-3010](admin_ui/src/pages/ChatHarness.tsx#L3007-L3010)):

```ts
messages.length <= 1 && (messages[0]?.id || "").startsWith("welcome_")
```

Drop the welcome bubble and `messages` is empty → `messages[0]?.id` is `undefined` →
predicate is **false** → **the form disappears too**. So the gate has to be rewritten
before the bubble is removed.

**Plan**
- Change `hasOnlyWelcomeMessage` to a "conversation hasn't started" predicate:
  `messages.filter(m => m.role === "user").length === 0 && no non-welcome assistant rows`.
  `hasSentUserMessage` ([:3016](admin_ui/src/pages/ChatHarness.tsx#L3016)) already exists and is
  the robust signal the file's own comment recommends.
- Suppress the welcome bubble **only** for the fresh pre-booking intro-form case
  (multi-property Zone). Do not touch the pre_checkin / during_stay greetings at
  lines 744-763 — Sarovar and the in-stay widget rely on them.
- There are **four** injection points to keep consistent, not one:
  [:1800](admin_ui/src/pages/ChatHarness.tsx#L1800) (reset), [:2548](admin_ui/src/pages/ChatHarness.tsx#L2548) (mount),
  [:2563](admin_ui/src/pages/ChatHarness.tsx#L2563) (re-sync), [:3406](admin_ui/src/pages/ChatHarness.tsx#L3406).
- Fold the greeting line into the form card header (it already says
  *"Hey there! Let's get you set up 😊"*) so nothing is lost.

**Verify:** fresh load in harness + embed, phone + laptop; then dustbin → new
conversation (`convoResetNonce` path) — that's where the form previously got stuck.

---

## 2. Replies are too long

**Root cause is not the model — the prompt orders it.**
[`_answer_brevity_directive()`](services/llm_orchestration_directives_mixin.py#L27-L46)
is misnamed; it pushes *up*:

- rule 2: broad questions are *"NOT a one-liner"*, *"roughly 3-5 useful highlights"*, *"A rich property must NEVER be reduced to a single line"*
- rule 3: *"MULTIPLE selling points… One short line here is a failure."*
- rule 7: *"cover EVERY in-scope property"* and *"include ALL"* room types

Same push in the service-agent contract
([llm_orchestration_service.py:388-405](services/llm_orchestration_service.py#L388-L405)):
"2-4 compact mini sections", "2-4 guest-friendly mini sections".

Second contributor: the 21 curated city intros are **223–475 words** each
(Kolkata 475, Coimbatore 413, Goa 442 — the multi-property ones are the worst).

**Plan**
- Add explicit ceilings rather than softening adjectives (models obey numbers, not
  "be concise"): specific ask ≤ ~40 words; broad ask ≤ ~120 words / max 3 mini
  sections; multi-property comparison ≤ ~60 words per property.
- Rewrite rules 2 and 3 to remove *"never a one-liner"* / *"one short line is a failure"*.
- **Keep rule 7's coverage requirement** (never silently drop a property or a room
  type) — that was a fix for a real bug. Shorten each item instead of dropping items.
- Prompt-only change, so it's revertible via the prompt registry with no deploy.
- Rewrite the 21 static intros to ~120-160 words (bundled with items 3 and 5 — same rows).

**Watch for regressions in:** `tests/test_book_answerer_prompt.py`-style pins
(`test_book_answerer_response_is_not_truncated_by_post_processing`), the
"cover every property" behaviour, and the Sarovar bullets/bold styling contract
(this directive is shared across tenants — check whether Zone needs a hotel-scoped
override row rather than a global edit).

---

## 3. Don't re-ask travel preference after already recommending

**This is static content, not the LLM.** The text in your screenshot 2 is verbatim
from `intromessage.txt` line 305 → seeded into `new_bot_location_intros.body` and
served with **zero LLM involvement** by
[`_build_location_intro_response`](api/routes/chat.py#L1117-L1255) via
[`location_intro_service.get_intro`](services/location_intro_service.py#L70).

I checked: **all 21 of 21** intro bodies end with a preference question
("Are you planning a **business trip, a heritage tour…**?" / "Whether you're planning…
I'd be happy to recommend the perfect room").

**Plan**
- Strip the preference-asking tail from all 21 rows.
- Replace it with a *passive* closer that hands control to the guest — e.g.
  "Happy to go deeper on either one — just say the word." — so the bot only
  recommends again when explicitly asked, which is exactly your rule.
- Two places to edit, both needed: the DB rows (live) **and** `intromessage.txt`
  (the seed source, or the next `tools/seed_location_intros.py` run re-introduces it).
- Cache: `location_intro_service` TTL-caches 300s; call `invalidate()` or wait 5 min.
- Guard the LLM path too: the answerer can still tack on a "shall I recommend?"
  closer on later turns. Add one line to the answer-style directive — *never re-ask
  for stay preferences once a recommendation has been given this conversation; wait
  for the guest to ask.* Prompt-driven, no keyword list (per the standing rule).

---

## 5. Multi-hotel: unique points + explicit choice buttons

Only **3 of 21** cities are multi-property: **Coimbatore, Kolkata, Goa** (2 each).
So this is a small, contained content job — 3 rows.

**Two parts.**

**(a) Content — unique "best for" points.** Today Kolkata buries the differentiator
in a prose paragraph at the end. Restructure each of the 3 multi-property bodies to:
short city line → per hotel a bolded one-line *"Best for: …"* + 2-3 tight points →
a single "which would you like to continue with?" line. No trailing preference question.

**(b) Buttons.** [`_build_location_intro_response`](api/routes/chat.py#L1213) currently
returns `suggested_actions=[]` and the response's `suggested_actions=[]`
([:1245](api/routes/chat.py#L1245)). Populate it with one entry per in-scope property
(the names are already resolved right there as `property_names`, and are already
re-ordered to match the body's display order at
[:1174-1188](api/routes/chat.py#L1174-L1188) — reuse that order).

Rendering needs **no frontend work**: assistant `suggested_actions` already render as
full-width pill buttons at
[ChatHarness.tsx:4611-4625](admin_ui/src/pages/ChatHarness.tsx#L4611-L4625), and a click
sends the label as a normal message, which the existing narrow-on-interest routing
already turns into a property pin.

**Decision needed:** button label. `"Zone by The Park, Kolkata (New Town)"` (clean, and
the router already matches property names) vs `"Tell me more about New Town"`. I'd go
with the bare property name — it's unambiguous for the router and reads like a choice.

**Only emit buttons when 2+ properties are in scope** — a single-property city
shouldn't grow a pointless button.

---

## 6. After selecting a hotel: auto-show room categories + images

**Root cause — confirmed, and it is exactly the keyword problem you described.**
[`llm_select_images`](services/image_tool_service.py#L894-L951) hard-gates on
[`_select_candidates_by_title_mentions`](services/image_tool_service.py#L192-L264):
an image attaches **only if its stored title appears as a literal token sequence in
the assistant's reply text**. No mention → early return
`no_title_mentions_in_assistant_response` ([:923-929](services/image_tool_service.py#L923-L929))
→ zero images. And `chat_smart_images_fast_title_match_only=True`
([settings.py:321](config/settings.py#L321)) means even the LLM selector never runs.

Ordering is by **position of first mention** ([:251](services/image_tool_service.py#L251)) —
there is no façade-first concept anywhere.

**Plan — add a second, non-keyword selection path (don't rip out the first).**

1. **New selector** in `image_tool_service`: `select_gallery_for_properties(...)` that
   picks straight from `bot_hotel_images_scraper` scoped by `property_id`, ordered by
   a category rank (façade/exterior → rooms → dining → pool/amenities), then
   `priority desc`. `list_candidate_assets` ([:733-821](services/image_tool_service.py#L733-L821))
   already loads `category`, `tags`, `priority`, `property_id` — the data is there.
2. **New trigger** in [`_attach_smart_images`](api/routes/chat.py#L2070): when this turn
   is a *property-selection / show-me-the-rooms* turn, use the gallery selector
   instead of (or as fallback to) title matching.
3. **Multi-hotel:** `_multi_scope_ids` ([chat.py:2262-2269](api/routes/chat.py#L2262-L2269))
   already carries the full in-scope property set. Fan out N images per property so
   all hotels are represented, rather than the current all-or-nothing.
4. **Text/image agreement:** the reply should also *list* the room categories, so the
   carousel isn't captioned by an unrelated paragraph. That's the answerer prompt.

**Prerequisite I could not complete — flagging it rather than guessing.** VPN was down
(`172.16.5.32` timed out), so I could not audit what `category`/`tags` values actually
exist on Zone's image rows. **The whole category-ordering design depends on that
vocabulary being populated.** If `category` is mostly NULL we need either a backfill or
to derive from title via the existing
[`_derive_tags_from_title`](services/image_tool_service.py#L299). First task when the
VPN is up: `SELECT category, COUNT(*) … GROUP BY category` for the Zone hotel_id.

**Decision needed:** how many images per property on a selection turn? I'd say façade +
up to 3 room categories = 4, so a 2-hotel city shows 8 in the carousel rather than
flooding it.

---

## 7. Accurate distances to nearby key places

**Current state: there is no geo capability at all.** Grep for search/maps returns only
`services/weather_service.py`, which geocodes *cities* via Open-Meteo. There is no
Places, Distance Matrix, Nominatim or web-search integration; the LLM runs on
`gpt-4.1-mini` over `chat.completions` ([.env:62](.env#L62)), which has no built-in
web-search tool.

And the bot is **correctly** refusing today — the grounding directive forbids it:
> *"Never state a number, name, price, distance, or time that is not in the KB"*
> ([llm_orchestration_directives_mixin.py:62](services/llm_orchestration_directives_mixin.py#L62))
> *"a property's address is not a named landmark's distance"* ([:67](services/llm_orchestration_directives_mixin.py#L67))

So this cannot be fixed by prompt alone. Either we feed it real data, or we tell it to
guess — and guessing distances to hospitals/airports is exactly the kind of wrong that
gets noticed.

**Plan**
- New `services/place_distance_service.py`:
  - Property coordinates from `Property.location` JSON (schema is
    `{city, address, country, coordinates}` — [models/database.py:752](models/database.py#L752)),
    falling back to a one-time geocode of the address, cached.
  - Landmark → coordinates → road distance + drive time.
- **Route the call the right way** (per your standing rule — no keyword lists in
  Python): the router LLM emits a field like `needs_local_geo: true` with the landmark
  it wants; the code enforces on *its own emitted field* and calls the tool. Never a
  `if "how far" in msg` guard.
- Inject the tool result as a **KB-grade fact block** for that turn, so the grounding
  directive is satisfied by real data instead of being bypassed.
- Cache aggressively per (property, landmark) — these never change.

**Decision needed — provider.** This is the one item with a real external cost:

| Option | Accuracy | Cost | Notes |
|---|---|---|---|
| Google Places + Distance Matrix | Best (real driving distance/time, best POI matching for Indian landmarks) | API key + per-call billing | My recommendation if budget allows |
| OSM Nominatim + OSRM | Good | Free | Rate-limited (1 req/s on public instances), needs attribution; realistically wants self-hosting for production |
| Curate distances into the KB per property | Perfect, zero runtime cost | Manual, static | 24 properties × ~6 landmarks; already partly present in the intros (e.g. "10 km from Chennai Airport") |

Honest read: **option 3 covers 80% of real questions** (airport, railway station, bus
stand, city centre) for a day of content work and zero new infrastructure, and it is
fully consistent with the grounding rule. I'd do option 3 first and add option 1 only
for the long tail ("how far is Apollo Hospital?"). Tell me which way you want to go.

---

## 8. Suggestion chips cut off on laptop

**Root cause found, and it's a clean one.**
[`useIsMobile()`](admin_ui/src/hooks/use-mobile.tsx) is `window.innerWidth < 768`.
Inside the embed iframe, `window` is the **iframe**, which the loader sizes to
`LOCAL_WIDGET_WIDTH = 380` ([kebo-widget-loader.js:4](static/embed/kebo-widget-loader.js#L4)).

**So `isMobile` is always `true` in the embed — including on a laptop.**

That flips the chip row to the horizontal-scroll branch at
[ChatHarness.tsx:4913](admin_ui/src/pages/ChatHarness.tsx#L4913):

```ts
isMobile ? "chip-scroll-row flex gap-2 overflow-x-auto px-3" : "flex flex-wrap gap-2 px-3"
```

…with `scrollbarWidth: "none"` ([:4917](admin_ui/src/pages/ChatHarness.tsx#L4917)) and a
WebKit scrollbar hidden in [index.css:103](admin_ui/src/index.css#L103). Chips overflow
right with **no visible scrollbar and no affordance** — exactly your screenshot. Chips
are capped at 3 ([llm_orchestration_chips_mixin.py:2246](services/llm_orchestration_chips_mixin.py#L2246)),
so it's usually the 3rd one that's invisible.

**Two more symptoms of the same bug, worth fixing together:**
- The inline form is split into multi-step pages on desktop embed
  ([:3551-3555](admin_ui/src/pages/ChatHarness.tsx#L3551-L3555)) — that was meant for phones only.
- The CTA reads "Book" instead of "Book a Stay" on desktop embed
  ([:4721](admin_ui/src/pages/ChatHarness.tsx#L4721)).

**Plan**
- Give the widget a true device signal instead of an iframe-width proxy. Cleanest:
  the loader already measures the **parent** viewport in `applyResponsiveSize`
  ([kebo-widget-loader.js:543-559](static/embed/kebo-widget-loader.js#L543-L559)) — have it pass a
  `device=mobile|desktop` param (it already sets ~20 frame params at
  [:282-310](static/embed/kebo-widget-loader.js#L282-L310)) and postMessage updates on resize.
- Fallback for direct iframe embeds that skip the loader: `matchMedia("(pointer: coarse)")`,
  which reports the real input device regardless of iframe width.
- Then per your ask: desktop → `flex-wrap` (chips wrap onto 2 lines, nothing hidden);
  mobile → leave the horizontal scroller exactly as-is.
- Height: wrapping 3 chips adds at most one row (~28px). The loader's
  `setRequestedSize` path ([:525](static/embed/kebo-widget-loader.js#L525)) already handles frame
  resize, so no layout break expected — but verify at 380px width.

---

## 9. "Book on website" + a second "assistance from the team" button

Your spec, as I read it: keep the silent ticket as-is; when the bot shows *Book on
website*, add a second button under it; clicking opens a form (dates, number of guests,
message textarea); on submit the details land on the **existing** ticket rather than
spawning a new one.

**What already exists (good news — most of it):**
- CTA buttons render from `metadata.booking_cta` and **already support a list** of
  buttons stacked vertically ([ChatHarness.tsx:4581-4609](admin_ui/src/pages/ChatHarness.tsx#L4581-L4609)).
- The silent room-booking ticket is created in the background by
  [`_rb_create_booking`](services/chat_service_booking_mixin.py#L40-L84) →
  `_create_silent_room_booking_ticket_bg`, with `category="room_booking"` and the
  property name stamped on it *specifically so it can be found later for update/cancel*
  ([:60](services/chat_service_booking_mixin.py#L60)).
- The edit primitive exists and is proven:
  `ticketing_service.append_ticket_comment(session_id, ticket_ref, note, set_status)`
  writes the DB row **and** PATCHes the external ticketing API
  ([used at llm_orchestration_service.py:7175](services/llm_orchestration_service.py#L7175)).
  `list_session_tickets(session_id)` gives the refs.
- Inline forms already post to `/api/chat/form-submit`
  ([ChatHarness.tsx:3277](admin_ui/src/pages/ChatHarness.tsx#L3277) → [api/routes/chat.py:4696](api/routes/chat.py#L4696)).

**Plan**
1. **Second button.** Add an `assist_cta` alongside `booking_cta` in
   [`_apply_booking_cta_marker`](api/routes/chat.py#L1680-L1918) — attached on the same
   turns the booking button is attached, so it inherits every existing suppression
   rule for free (multi-property early-return at [:1806](api/routes/chat.py#L1806),
   handoff/decline guard at [:1829-1864](api/routes/chat.py#L1829-L1864), invalid-date
   guard at [:1763](api/routes/chat.py#L1763)). Renders as a button that opens a form
   locally instead of a link — small addition next to the CTA block at
   [ChatHarness.tsx:4581](admin_ui/src/pages/ChatHarness.tsx#L4581).
2. **The form.** Check-in date, check-out date, number of guests, message textarea.
   Reuse the existing `inlineForm` machinery (validation, mobile stepping, input
   locking) rather than hand-rolling — it already handles the embed and mobile cases.
3. **Submit → edit, don't create.** New handler (or a reserved `service_id` on
   `/api/chat/form-submit`) that:
   - finds this session's open `room_booking` ticket for the in-focus property
     (`list_session_tickets` + property_name match — the field is already stamped),
   - calls `append_ticket_comment` with a formatted note (dates / guests / message),
   - **falls back to creating** one via the same payload builder if none exists
     (guest can click the button before any booking-intent ticket was raised).
4. **Reply:** "Passing this to our team — they'll call you with the best rates."

**Decisions needed:**
- Button label — *"Get help from our team"* / *"Have the team call me"*?
- If the guest submits the form **twice**, second submit = another comment on the same
  ticket, or replace? I'd append (audit trail; matches how cancel/modify works today).
- Multi-property still in focus: today **both** CTAs would be suppressed by the
  `_too_many_properties` early return. Should the assistance button appear anyway
  (it doesn't need a chosen hotel), or follow the same rule? I'd follow the same rule —
  the ticket needs a property to route to.

---

## Cross-cutting: harness + iframe parity

All frontend items ride the same component, so parity is free — *except* the device
detection in item 8, which is the one place where the harness and the embed genuinely
diverge today. Fixing that makes the two behave identically, which also silently fixes
the multi-step form and the truncated CTA label in the embed.

**Test matrix per item:** harness @ laptop · harness @ phone · embed @ laptop ·
embed @ phone · fresh session · dustbin-rotated session.

---

## Suggested sequencing

**Batch A — content only, no deploy, instantly revertible (items 3, 5a, part of 2)**
21 intro rows: strip the preference tail, restructure the 3 multi-property bodies with
"Best for" lines, cut word counts. Highest visible improvement per unit of risk.

**Batch B — small frontend (items 1, 8, 5b)**
Device signal, chip wrapping, welcome-bubble removal + gate rewrite, choice buttons.
Self-contained, easy to verify visually.

**Batch C — prompt (item 2 remainder)**
Length ceilings in the answer directive. Needs a regression pass on both tenants —
check whether this should be a Zone-scoped override rather than global.

**Batch D — backend (items 6, 9)**
Gallery image selector and the assistance form → ticket edit. Item 6 is **blocked on
the image-category audit** until the VPN is up.

**Batch E — item 7**
Blocked on your provider decision. My recommendation: curate the common landmarks into
the KB first (covers most questions, zero new infra), add a live geo tool later only if
the long tail justifies it.

---

## What I need from you before implementing

1. **Item 7:** Google Maps (paid) / OSM (free, rate-limited) / curate into KB / mix?
2. **Item 5:** button label — bare property name, or "Tell me more about X"?
3. **Item 6:** how many images per property on a selection turn (I suggest 4)?
4. **Item 9:** button label; and should the assistance button show while 2+ hotels are
   still in focus?
5. **Item 2:** is the length change Zone-only, or should Sarovar get it too? (The
   directive is currently shared.)
