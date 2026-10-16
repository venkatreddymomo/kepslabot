"""Seed the per-property "you tapped this hotel" messages, and retune the
multi-property chooser tails.

WHY THESE ARE PRE-WRITTEN. The same job was first built as an instruction to the
answerer ("tell them about the hotel, not the city"). It did not hold: tapping
New Market still opened with "Located in the vibrant heart of Kolkata near
cultural landmarks like Indian Museum, Howrah Bridge, Victoria Memorial" — a
description of the city the guest had read one message earlier. Pre-written
copy is exact, costs no model call, and renders instantly.

Every line is lifted from the curated city intros already in
new_bot_location_intros, which were themselves written off the KB. Nothing here
is invented. Zone Connect Coimbatore carries no facilities line because the KB
lists none for it.

Stored in the SAME table as the city intros, keyed "prop::<property_slug>", so
no migration and the existing TTL cache covers both.

    python tools/seed_property_intros.py          # write
    python tools/seed_property_intros.py --dry    # show what would change
"""
from __future__ import annotations

import sys

import pymysql

HOTEL_ID = 67


def msg(head: str, rooms: str, food: str, offers: str, facilities: str = "") -> str:
    """Name, then rooms, food, facilities, offers. Nothing after.

    It ended on a question ("Want to see the rooms, the menus, or check your
    dates?", later trimmed to just the rooms). Both versions pushed the guest
    somewhere instead of letting them read what they had asked for. The four
    lines answer the tap; the guest can say what they want next.
    """
    out = [head, ""]
    out.append(f"🛏️ **Rooms** — {rooms}")
    out.append(f"🍽️ **Eat & drink** — {food}")
    if facilities:
        out.append(f"✨ **Facilities** — {facilities}")
    out.append(f"⭐ **Offers** — {offers}")
    return "\n".join(out)


OFFERS_STD = "**20% off** direct bookings · AMEX **20% rooms / 25% suites** · Early Bird **15% off**"
OFFERS_GOA = "**20% off** direct bookings · Early Bird **15% off** · seasonal offers depending on your dates"
OFFERS_KOL = ("Early Bird **15% off** · AMEX **20% off rooms**, plus suite benefits where "
              "applicable · direct booking and seasonal offers")

PROPERTY_INTROS: dict[str, str] = {
    "zone_park_chennai_orr": msg(
        "🏨 **Zone by The Park, Chennai ORR**",
        "**Zone Room / Zone Bay Window** with a window seat over the city, the **Zone Suite** "
        "with a bathtub, and **Zone Suite Plus** with its own private terrace",
        "live cooking at **Bazaar**, cocktails at **Z-Bar**, kababs by the pool at **Playa by Zone**",
        OFFERS_STD,
        "pool with a kids' area, spa and fitness centre; children up to 7 stay free",
    ),
    "zone_park_coimbatore": msg(
        "🏨 **Zone by The Park, Coimbatore**",
        "**Zone Room** and **Zone Deluxe** in king or twin, the Deluxe with a city view, and the "
        "**Zone Suite** with a bathtub and its own living area",
        "Kongunadu specialities at **Bazaar**, drinks at **Z Bar**, the casual **Orange Zone Lounge**",
        OFFERS_STD,
        "meeting rooms, banquet space, a business centre, kids' **FunZone** and connecting rooms",
    ),
    "zone_connect_park_coimbatore": msg(
        "✈️ **Zone Connect by The Park, Coimbatore**",
        "**Connect Room (Standard)** and **Connect Premium Room** for a night or two, and the "
        "**Suite Room** with a separate living area",
        "Indian, South Indian, Chinese and Continental at **Cafe C**, drinks at **Co Bar**",
        OFFERS_STD,
        # no facilities line: the KB lists none for this property
    ),
    "zone_park_darjeeling": msg(
        "🏨 **Zone by The Park, Darjeeling**",
        "six choices — **Zone Room**, **Deluxe**, **Luxe**, **Junior Suite**, **Executive Suite** "
        "and **Luxe Suite**; select rooms open onto valley views and private balconies",
        "multi-cuisine all day at **Bazaar**, plus in-room dining for slow mornings",
        "Early Bird **15% off** when you book **7 days ahead** for stays over one night · "
        "direct booking benefits",
        "a **FunZone** and **Khel Zone** for when the mist rolls in",
    ),
    "zone_connect_park_saket_delhi": msg(
        "🏨 **Zone Connect by The Park Saket Delhi**",
        "**Connect Room** in queen or twin, the **Premium Room** with more room to spread out, and "
        "the **Luxe Room** looking over green city views; connecting rooms for families and groups",
        "regional and international plates at **Cafe C**, open-air evenings at the "
        "**Terrace Lounge (Co-Bar)**",
        OFFERS_STD,
        "the **Townhall** conference space",
    ),
    "zone_park_digha": msg(
        "🏨 **Zone by The Park, Digha**",
        "**Zone Room** with a queen bed, the **Deluxe** with a touch more space, the **Luxe** for "
        "longer stays, and a **Suite** with its own living area — all generous, this being the seaside",
        "**Bazaar**, drinks at **Liquiteria**, poolside plates at **Playa**; pure vegetarian and "
        "Jain options always",
        "Early Bird **15% off** booking 7 days ahead · **up to 8% off** on 3+ night stays · "
        "**flat 8% off** Friday, Saturday or Sunday",
        "pool, **Vitalia Spa**, fitness centre and a kids' **FunZone**",
    ),
    "zone_niathu_park_dimapur": msg(
        "🏨 **Zone Niathu by The Park, Dimapur**",
        "**Zone Room** with a queen bed, the **Deluxe** with a king, a **Suite** with a separate "
        "living area, and a **Presidential Villa** to yourself — every one with a garden view and "
        "private balcony",
        "five places: **Bazaar** for multi-cuisine and **Naga delicacies**, **Machang** for "
        "traditional Naga, **Ruby Cafe**, poolside **Playa by Zone** and **Splash Bar**",
        "**20% off** direct bookings · AMEX **20% rooms / 25% suites** · **Hornbill Festival "
        "packages**, 1–10 December",
        "pool, **Vitalia Spa**, fitness centre, Children's Park, courts for badminton, futsal, "
        "volleyball, tennis, basketball and pickleball, plus **200,000 sq. ft.** of outdoor event "
        "space and an indoor Town Hall",
    ),
    "zone_connect_park_mount_siniolchu_gangtok": msg(
        "🏨 **Zone Connect by The Park Mount Siniolchu, Gangtok**",
        "a cosy **Connect Room**, the **Deluxe** in queen or twin, a **Premium Room** with more "
        "room to enjoy it, and the largest **Suite** — nearly all facing the **Kanchenjunga range**",
        "multi-cuisine and Sikkimese flavours at **Cafe C**, drinks and music at **Co Bar**",
        "Early Bird **15% off** · direct booking benefits · seasonal offers through the year",
        "kids' play area and complimentary Wi-Fi",
    ),
    "zone_connect_park_calangute_goa": msg(
        "🌴 **Zone Connect by The Park, Calangute Goa**",
        "**Connect Room** and **Premium Room** for couples, then the **Junior Suite** with a sofa "
        "cum bed, and the **Executive Suite**",
        "Goan and multi-cuisine at **Cafe C** — the **Goan Fish Curry Rice** is the one to order — "
        "with cocktails at **Co-Bar**",
        OFFERS_GOA,
        "adult and kids' pools, plus the poolside **Plunge**",
    ),
    "zone_connect_park_parra_goa": msg(
        "🌿 **Zone Connect by The Park Parra Goa**",
        "**Connect Rooms** and **Premium Rooms** for two, the **Family Area** with two queen beds, "
        "and a **Suite** for the most space",
        "multi-cuisine at **Cafe C** with Goan favourites — **Chamucas**, **Chanya Tonak**, "
        "**Ros Omelette**",
        OFFERS_GOA,
        "pool, kids' pool, jacuzzi and an on-site spa",
    ),
    "zone_park_gopalpur": msg(
        "🏨 **Zone by The Park, Gopalpur**",
        "**Zone Room** in king or twin with a city view, the **Zone Deluxe** with more space, and a "
        "**Zone Suite** with a separate living area; connecting rooms for larger groups",
        "**Bazaar**, rooftop grills at **Urban Spice Box**, cocktails at **Z Bar**, poolside **Playa**",
        "**20% off** direct bookings · AMEX **20% rooms / 25% suites** · seasonal **Monsoon Offers**",
        "pool, **Vitalia Spa**, fitness centre, plus **Kids Play Zone**, **FunZone** and **Khel Zone**",
    ),
    "zone_park_jaipur": msg(
        "🏨 **Zone by The Park, Jaipur**",
        "**Zone Deluxe** for two, the **Deluxe Trio** sleeping three, the **Deluxe Family Room** "
        "with two double beds sleeping four, a **Zone Suite Room** with its own living space, and "
        "**Suite Celebration** for occasions; accessible rooms available",
        "**Bazaar**, rooftop grills at **Playa by Zone**, cocktails at **Z Bar**",
        OFFERS_STD,
        "a **rooftop pool**, health club and spa",
    ),
    "zone_connect_park_jaisalmer": msg(
        "🏨 **Zone Connect by The Park Jaisalmer**",
        "five choices — **Zone Connect Room** and **Premium Room** for couples, **Luxe Room** and "
        "**Suite** for more space, and a **Family Room** for everyone together; king and twin available",
        "Indian and Chinese at **Cafe C** — **Dal Baati Churma**, **Chicken Rajasthani** — plus "
        "room service",
        "**20% off** direct bookings · AMEX **20% rooms / 25% suites** · Early Bird offers subject "
        "to availability",
        "an outdoor pool for after a day in the sun, indoor and outdoor games, banquet facilities, "
        "outdoor event spaces and wedding venues",
    ),
    "zone_park_jammu": msg(
        "🏨 **Zone by The Park, Jammu**",
        "**Standard / Zone Room** with a queen bed and park, city or station views, a **Deluxe Room** "
        "with more space, and a **Suite** with a separate living area — all with smart technology "
        "and a proper place to work",
        "live cooking at **Bazaar**, cocktails at **Z-Bar**, light bites at **Orange Zone / V-Lounge**",
        OFFERS_STD,
        "banquet halls, meeting spaces and a 24-hour fitness centre",
    ),
    "zone_connect_park_katra": msg(
        "🏨 **Zone Connect by The Park, Katra**",
        "**Connect Room** with a king bed, **Connect Premium** with a large queen, the roomier "
        "**Luxe Room**, a **Trio Room** sleeping three, and a **Family Room** with two large beds — "
        "sized for families and pilgrim groups",
        "pure vegetarian at **Cafe C**, including **Kashmiri Dum Aloo**, with **24-hour room "
        "service** for late returns from the yatra",
        "Early Bird **15% off** · direct booking benefits · seasonal offers through the year",
        "**Townhall** for gatherings, **Grey Zone** for meetings, **FunZone** for children",
    ),
    "zone_park_kolkata": msg(
        "🏨 **Zone by The Park, Kolkata (New Town)**",
        "**Zone Deluxe** and **Zone Luxury** in king or twin, and the roomier **Zone Suite** "
        "with its own living area",
        "**Bazaar** for regional and international cooking, cocktails at **Z-Bar**, and the "
        "**Lobby Bar**",
        OFFERS_KOL,
        "rooftop pool, **Vitalia Spa**, fitness centre, **FunZone**, plus banquet halls and boardrooms",
    ),
    "zone_park_new_market_kolkata": msg(
        "🏛️ **Zone by The Park, New Market Kolkata**",
        "**Zone Deluxe** for an easy city stay, and the roomier **Zone Luxe**",
        "**Bazaar** for global and local cooking, and the heritage tearoom **Flurys** right in the lobby",
        "Early Bird **15% off** · AMEX **20% off rooms** · direct booking and seasonal offers",
        "business centre, meeting facilities and a rooftop event space",
    ),
    "zone_connect_park_lansdowne": msg(
        "🏨 **Zone Connect by The Park, Lansdowne**",
        "a snug **Connect Room** with a queen bed, a **Premium Room** with a **valley view**, the "
        "**Luxe Room** to stretch out in, a **Family Room** with two double beds, and a **Suite** "
        "with a separate living area",
        "multi-cuisine at **Cafe C**, and a traditional **Pahadi Thali** on request",
        OFFERS_STD,
        "a plunge pool, forest treks, bird watching and adventure activities — and **pets are welcome**",
    ),
    "zone_connect_park_mussoorie": msg(
        "🏨 **Zone Connect by The Park, Mussoorie**",
        "**Connect Room** with a double plus a sofa cum bed, **Premium** and **Luxe Rooms** with "
        "their own terrace, a **Luxe Room with Balcony**, and a **Family Room** with two double "
        "beds — most open onto a terrace or balcony",
        "multi-cuisine at **Cafe C**, including the signature **Pahadi Chicken**",
        "**20% off** direct bookings · web-only rates with **early check-in and late check-out** · "
        "Early Bird **15% off**",
        "a **FunZone** for kids and banquet facilities; pets welcome in **Luxe Rooms** on a "
        "chargeable basis",
    ),
    "zone_park_pathankot": msg(
        "🏨 **Zone by The Park Pathankot**",
        "a neat **Zone Room** with a queen bed, a **Deluxe** with more room to unpack, and a "
        "**Suite** with a separate living space",
        "multi-cuisine and regional flavours at **Bazaar**, drinks at **Z Bar**",
        "**20% off** direct bookings · AMEX **20% rooms / 25% suites** · Early Bird **15% off** · "
        "**25% off** food and soft beverages",
        "banquet spaces, business facilities, a kids' **FunZone** and a terrace mini gym",
    ),
    "zone_connect_park_patna": msg(
        "🏨 **Zone Connect by The Park Patna**",
        "**Connect Twin / Premium Room** in twin or queen with plenty of room, a **Cottage Room** of "
        "your own by the pool, and the largest **Suite Room** with a queen bed",
        "regional and global cooking at **Cafe C** — **Bihari Mutton Curry**, **Martan ka Paneer** — "
        "plus coffee and fresh bakes at **The Bakery**",
        "**20% off** direct bookings · early check-in and late check-out subject to availability · "
        "Early Bird **15% off**",
        "a plunge pool, fitness centre, a dedicated **FunZone**, a **6,000 sq. ft. Townhall** and a "
        "**20,000 sq. ft.** landscaped lawn for weddings",
    ),
    "zone_palace_park_phalodi": msg(
        "🏛️ **Zone Palace By The Park Phalodi**",
        "four kinds within the **1760** haveli — **Heritage Chambers**, **Haveli Rooms**, "
        "**Royal Heritage Rooms**, and the **Maharaja Suite** in king or twin",
        "traditional vegetarian Rajasthani cooking at **Bazaar** — **Dal Baati Churma**, "
        "**Gulabjamun Ki Sabji**, **Methi Papad Ki Sabzi** — plus **Z Bar**",
        "**20% off** direct bookings · AMEX **20% rooms / 25% suites** · Early Bird and seasonal offers",
        "an outdoor pool, a **FunZone** for children, and **Dhada's Heritage Museum of Jain & "
        "Oriental Arts** attached",
    ),
    "zone_connect_park_port_blair": msg(
        "🏨 **Zone Connect by The Park Sri Vijaya Puram (Port Blair)**",
        "**Connect Room** in king or twin sleeping two, a **Premium Room** with **lake view**, sofa "
        "cum bed and balcony sleeping three, and the largest **Luxe Room** with lake view and "
        "balcony sleeping four",
        "Indian, Chinese, Continental and Tandoor at **Cafe C**, cocktails at **Co-Bar**",
        "**20% off** direct bookings · early check-in and late check-out subject to availability · "
        "Early Bird **15% off**",
        "a plunge pool, a kids' pool and an on-site spa",
    ),
    "zone_connect_park_udaipur": msg(
        "🏨 **Zone Connect by The Park Udaipur**",
        "**Connect Room** in king or twin with pool-view rooms available, a **Premium Room** with a "
        "queen bed and city view, and the largest **Suite** in king or twin",
        "pure vegetarian, vegan and Jain-friendly cooking at **Cafe C**, plus poolside outdoor dining",
        "**20% off** direct bookings · early check-in and late check-out · Early Bird **15% off** "
        "with early check-in **10:00–11:00 AM** subject to availability",
        "a plunge pool, foosball, a spacious **Townhall** and a poolside lawn; rooms set up for work "
        "as much as leisure",
    ),
}

# The chooser tail on a multi-property welcome. Framed by the KIND OF TRIP, with
# the hotel's own rooms/food/facilities as the reason — an earlier version sold
# the neighbourhood instead, which says nothing about the hotel you are picking.
CHOOSER_TAILS: dict[str, str] = {
    "goa": (
        "**Still deciding?**\n"
        "💑 A romantic break or a lively beach weekend — suites for two, cocktails at Co-Bar, "
        "the Calangute strip close by → **Zone Connect by The Park, Calangute Goa**\n"
        "👨‍👩‍👧 A family holiday or a slow, quiet one — a Family Area with two queen beds, kids' "
        "pool, jacuzzi and an on-site spa → **Zone Connect by The Park, Parra Goa**\n\n"
        "Tap a name below and I'll take you through it."
    ),
    "kolkata": (
        "**Still deciding?**\n"
        "🎉 A celebration, a family stay with room to spread out, or a work trip — three room types "
        "up to the Zone Suite, rooftop pool, Vitalia Spa, Bazaar plus two bars, banquet halls "
        "and boardrooms → **Zone by The Park, Kolkata (New Town)**\n"
        "🛍️ A shopping-and-sightseeing city break — two easy room types, Bazaar, and the heritage "
        "Flurys tearoom in the lobby → **Zone by The Park, New Market Kolkata**\n\n"
        "Tap a name below and I'll take you through it."
    ),
    "coimbatore": (
        "**Still deciding?**\n"
        "👨‍👩‍👧 A family stay or a longer work trip — a Zone Suite with a bathtub and living area, "
        "three places to eat, banquets, connecting rooms and a FunZone → "
        "**Zone by The Park, Coimbatore**\n"
        "✈️ A quick overnight or an early flight — straightforward Connect rooms and a Suite, with "
        "Cafe C and Co Bar → **Zone Connect by The Park, Coimbatore**\n\n"
        "Tap a name below and I'll take you through it."
    ),
}


def main() -> int:
    dry = "--dry" in sys.argv
    conn = pymysql.connect(host="172.16.5.32", port=3306, user="root",
                           password="CHANGE_ME", database="GHN_PROD_BAK", connect_timeout=20)
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT property_slug, name FROM new_bot_properties
                           WHERE hotel_id=%s AND is_active=1 AND property_slug<>'shared'""",
                        (HOTEL_ID,))
            live = {r[0]: r[1] for r in cur.fetchall()}

            missing = sorted(set(live) - set(PROPERTY_INTROS))
            unknown = sorted(set(PROPERTY_INTROS) - set(live))
            if missing:
                print(f"!! {len(missing)} live properties have NO message: {missing}")
            if unknown:
                print(f"!! {len(unknown)} messages target no live property: {unknown}")
            print(f"covering {len(set(PROPERTY_INTROS) & set(live))}/{len(live)} live properties\n")

            for slug, body in sorted(PROPERTY_INTROS.items()):
                if slug not in live:
                    continue
                key = f"prop::{slug}"
                label = live[slug]
                cur.execute("""SELECT id FROM new_bot_location_intros
                               WHERE hotel_id=%s AND location_id=%s""", (HOTEL_ID, key))
                row = cur.fetchone()
                if dry:
                    print(f"[dry] {'update' if row else 'insert'}  {key}  ({len(body)} chars)")
                    continue
                if row:
                    cur.execute("""UPDATE new_bot_location_intros
                                   SET body=%s, location_label=%s, is_active=1 WHERE id=%s""",
                                (body, label, row[0]))
                    print(f"[upd] {key}  ({len(body)} chars)")
                else:
                    cur.execute("""INSERT INTO new_bot_location_intros
                                   (hotel_id, location_id, location_label, body, is_active)
                                   VALUES (%s,%s,%s,%s,1)""", (HOTEL_ID, key, label, body))
                    print(f"[ins] {key}  ({len(body)} chars)")

            print()
            for lid, tail in CHOOSER_TAILS.items():
                cur.execute("""SELECT id, body FROM new_bot_location_intros
                               WHERE hotel_id=%s AND location_id=%s AND is_active=1""",
                            (HOTEL_ID, lid))
                row = cur.fetchone()
                if not row:
                    print(f"[skip] {lid}: no intro row")
                    continue
                rid, body = row
                cut = body.find("**Still deciding?**")
                if cut == -1:
                    print(f"[skip] {lid}: no chooser tail to replace")
                    continue
                new_body = body[:cut] + tail
                if dry:
                    print(f"[dry] chooser {lid}: {len(body)} -> {len(new_body)} chars")
                    continue
                cur.execute("UPDATE new_bot_location_intros SET body=%s WHERE id=%s",
                            (new_body, rid))
                print(f"[upd] chooser {lid}: {len(body)} -> {len(new_body)} chars")
        if not dry:
            conn.commit()
            print("\ncommitted")
        else:
            print("\ndry run — nothing written")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
