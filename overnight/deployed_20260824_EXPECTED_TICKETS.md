# Deployed run 2026-08-24 — expected tickets in the external API

Env: https://bot.kepsla.ai/chat · hotel `sarovar_portico_sriperumbudur` · phase `during_stay`
Guest: Venkat / guest_id 993543 / room 1400 · entity+org 6311 · source whatsapp_bot
Timestamps are **UTC** (IST = +5:30). Window: 07:33–08:02 UTC = 13:03–13:32 IST.

Filter the external API by `conversation_uid` or the created_at window above.

## Session 1 — `session_1787556646542_uy7zu7a8`
uid `1a032adce8e-f6f931a5-5a4ea402` — expect exactly **7** tickets

| Ref | Expected issue | Dept | Expected status |
|-----|----------------|------|-----------------|
| 10959 | In-room dining: 1 Butter Chicken, 2 Garlic Naan — **no Agni Mule** (removed by guest) | Food & Beverage | open |
| 10960 | AC not cooling, room 1400 | Engineering | open (priority high) |
| 10967 | Table Reservation — **Outlet: Chola Lounge**, 4 guests, 2026-08-25 20:00 (created as SPR Kitchen, moved) | Food & Beverage | open |
| 10971 | 2 extra towels + 2 extra pillows, room 1400 | Housekeeping | open |
| 10972 | Spa & Wellness — Massage, 2026-08-25 18:00, "female therapist if possible, medium pressure" | Front Office | **cancelled** |
| 10973 | TV remote not working (batteries), room 1400 | Engineering | open |
| 10974 | Talk to team — unrecognised charge on bill | Front Office | open |

## Session 2 — `session_1787558143523_df4qo93k`
expect exactly **3** tickets

| Ref | Expected issue | Dept | Expected status |
|-----|----------------|------|-----------------|
| 10975 | Request for extra blanket, room 1400 | Housekeeping | open |
| 10976 | In-room dining: 1 Margherita Pizza with Pepperoni | Food & Beverage | **cancelled** |
| 10977 | Table Reservation — **Outlet: Chola Lounge**, 4 guests, 2026-08-25 20:00 (created as SPR Kitchen, moved) | Food & Beverage | open |

## Must NOT exist (negative checks)
- No ticket for **late checkout** (form opened twice, cancelled both times via "Cancel request").
- No **second** table reservation in either session — the venue move must edit 10967 / 10977 in place.
- No ticket for: the joke, 25x4, breakfast timing, gym/pool/wifi, pets, nearby restaurants,
  "what did I order", the Hindi "haan bhej dijiye" (must not duplicate the food order),
  or the made-up "Kadak Grill" outlet.
- Refs 10961-10966 and 10968-10970 are **not ours** — if any carry the session ids above,
  that means orphan/duplicate tickets were created and is a bug.

## KNOWN MISMATCH to check in the external API
Session 2: the guest was told at 13:28 IST that **Kozhi Chettinadu (Rs800) + Butter Naan (Rs225),
total Rs1025, is "on its way"** — but **no ticket for those items exists**. 10975 is the blanket,
10976 was pizza-only and is cancelled. If the external API also has no F&B ticket carrying
Kozhi Chettinadu, the kitchen never received an order the guest believes is coming.
