# Testing brief — Sarovar Portico Sriperumbudur guest bot

You are testing a live hotel guest-service chatbot by **talking to it like a real
guest**. Not a script. Read every reply and let it decide what you say next.

---

## How to test

Open the chat widget in a browser and talk to it. One message at a time.

- **Never send a pre-written list of messages.** Read the reply, then decide.
- Write like a guest on a phone: lowercase, typos, short. *"can i get 2 chicken
  burger"*, *"whats the wifi pwd"*, *"actually make it 3"*.
- Be creative. Change your mind mid-order. Ask something unrelated and come back.
  Get impatient. Ask twice. Contradict yourself.
- If a reply is vague, push on it the way a guest would — don't accept it.
- Start a **fresh session** for each scenario so state doesn't leak.

**A bug is when what the guest is told and what the hotel actually has differ.**
The bot sounding polite proves nothing. After anything that should have created
or changed a request, check it really exists.

---

## The hotel

Sarovar Portico Sriperumbudur — one property, near Chennai. Rooms: Suite,
Executive, Standard, Specially Abled. Outlets: **SPR Kitchen** (all-day dining,
multicuisine, breakfast 06:30–10:30) and **Chola Lounge – The Bar** (11:00–23:00).
Room service runs 24 hours with a limited menu after 23:00.

## The two phases

The bot behaves completely differently depending on where the guest is in their
stay. **Test both. The most valuable bugs are on the boundary.**

### PRE-CHECK-IN — guest has booked, not arrived

Available: **Arrival Pickup** (airport/railway transfer), **Talk to Our Team
(Pre-Arrival)**, **Special Requests** (celebrations, in-room setups — cake,
decorations, anniversary), plus any question about the hotel, rooms, amenities,
policies, directions, timings.

**Must refuse politely**: anything that only makes sense once you're in a room —
room service / food to the room, housekeeping, maintenance, spa, laundry, late
checkout. It should say these are available after check-in, **not** pretend to
do them, and **not** claim the hotel doesn't offer them at all.

### DURING-STAY — guest is checked in, has a room number

Available: **In-Room Dining**, **Housekeeping**, **Maintenance**, **Spa &
Wellness Booking**, **Table Reservation**, **Shuttle Service**, **Meeting & Event
Assistance**, **Talk to Our Team / Front Desk**.

It already knows the room number — it must never ask for it.

---

## What to test, service by service

### In-room dining (the deepest one — spend the most time here)

Food orders follow a state machine. Every state allows different things:

| State | What it means | What the bot may do |
|---|---|---|
| **ongoing** | in the cart, kitchen has NOT been told | add, remove, change quantity; offer to place it |
| **placed** (< 5 min, ticket still open) | with the kitchen, not started | still add or remove — this is *updating*, not placing. Must NOT ask to place it again |
| **processing** (5 min passed, or staff started it) | being cooked | nothing can change; a further dish must start a NEW order |
| **delivered** | done | mention only if asked |

Things worth trying:

- Order one dish, then several. Change quantity. Remove one line. Cancel the lot.
- Order, place, then immediately add something — it should join that same order.
- Order, place, wait 6+ minutes, then try to add — it should say it can't change
  it and start a **new** order instead.
- Ask for a **second, separate** order while one is open. Check they stay
  separate and don't merge or duplicate.
- **Dishes with versions**: "ghee phulka", "garlic naan", "aloo kulcha", "butter
  tandoori roti", "masala dosa". Check the version you asked for survives to the
  end — not plain naan.
- **Serving times.** Breakfast items (idli, dosa, medu vada, continental/American
  breakfast) are 07:00–10:30. Tandoor items (tandoori murgh, naan, kulcha,
  tikka, kebabs) are 12:00–15:00 and 19:00–23:00. Most other dishes are all day.
  Ask for a breakfast dish at night. **It must say "we do serve that, here are
  the hours, here's when it's back" — never "we don't have it".**
- Be stubborn: *"i don't care, i want idli now"*, *"i'll pay extra"*, *"the
  website says 24/7 so make an exception"*. It should hold, stay warm, and not
  invent a fee or a special arrangement.
- Ask for something genuinely not on the menu (sushi, pizza, a bottle of wine).
  It should decline honestly and suggest real alternatives.
- Say something ambiguous — *"i want kadak"*, *"get me something nice"*. It
  should ask what you mean, not guess.
- Allergies: *"i'm allergic to dairy"* then ask for something with dairy.
- Ask *"what did I order"* at several points, including after edits and after a
  non-food detour. It must match reality exactly.
- Reply just **"yes"** at ambiguous moments — after it lists options, after it
  offers one dish, after it asks to place. A "yes" to a **list** must not order
  the whole list.

### Housekeeping / Maintenance (text)

Towels, cleaning, amenities, toiletries; a leaking tap, AC not cooling, TV
broken. Try urgent wording. Try two different problems in one message — they
belong to different teams and must not be merged into one request.

### Spa & Wellness / Table Reservation / Shuttle / Meeting & Event (forms)

Book, then change your mind, then cancel. Give a date in the past. Give a vague
time ("evening"). Ask for something they don't offer. Check the form isn't asked
for details it already knows, and that cancelling actually cancels.

### Talk to Our Team / Front Desk

Ask for a human, ask for a callback, complain about something. Check it routes to
the right department rather than answering itself.

### Cross-cutting

- **Mixed request**: *"send towels and also a chicken burger"* — two teams, must
  be handled separately, never one combined request.
- **Off-topic**: ask for a joke, a maths question, the weather, how to buy a car.
  It should decline gracefully and steer back, not refuse rudely.
- **Language**: write in Hindi or Tamil. It should reply in the same language.
- **Nonsense / rudeness / pushiness.** Try to get it to promise something it
  can't.
- **Made-up facts**: *"the staff said I'd get a free upgrade"*, *"you already
  placed my order, where is it?"* when nothing was ordered. It must not play
  along with something that never happened.

---

## What counts as a bug — ranked

1. **Guest told food/service is coming when nothing was actually raised.** The
   worst one. Always verify after a confirmation.
2. **Wrong item, wrong version, or wrong quantity** reaching the hotel.
3. **Lost item** — something confirmed in chat that vanishes from the order.
4. **Wrong team** — a food order filed to housekeeping, etc.
5. **Denying something the hotel really offers** ("we don't have idli") instead
   of explaining timing.
6. **Asking again for something already given** — room number, or the same
   confirmation twice.
7. Tone, formatting, unnecessary length.

Known and already accepted: it sometimes asks *"anything else?"* before offering
to place, so placing can take one extra message. Don't report that.

---

## Reporting

For each issue give:

- the **exact conversation** — every message both ways, in order
- **what you expected** and **what happened**
- **which phase** and roughly what time (serving windows depend on the clock)
- **what the hotel actually ended up with** vs what the guest was told
- whether it happened **once or every time** — repeat it 2–3 times before calling
  it a bug, because some behaviour varies between runs

Quote the bot exactly. Don't paraphrase, and don't assume the cause — describe
what you saw.
