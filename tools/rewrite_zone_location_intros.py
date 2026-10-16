"""Rewrite Zone's curated city-intro bodies (new_bot_location_intros, hotel 67).

Why: the seeded bodies were 220-475 words each and every one of the 21 ended by
asking the guest for their travel preference ("Are you planning a business trip,
a heritage tour...?") even when the same message had ALREADY recommended a
property. That reads as a repetition to the guest.

What changes per row:
  - the trailing preference question is removed (the guest decides; we only
    recommend again when they explicitly ask)
  - the body is compressed to ~120-170 words with the same facts
  - the three multi-property cities (Coimbatore, Kolkata, Goa) get an explicit
    per-hotel "Best for:" differentiator and end on a single choice line, so the
    guest can pick instead of being interviewed

Facts (distances, outlet names, offer percentages) are carried over verbatim
from the previous bodies — nothing new is invented here.

Usage:
    python tools/rewrite_zone_location_intros.py --dry-run
    python tools/rewrite_zone_location_intros.py            # apply
    python tools/rewrite_zone_location_intros.py --restore backups/<file>.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

import pymysql

HOTEL_ID = 67

DB = dict(
    host=os.getenv("MYSQL_HOST", "172.16.5.32"),
    port=int(os.getenv("MYSQL_PORT", "3306")),
    user=os.getenv("MYSQL_USER", "root"),
    password=os.getenv("MYSQL_PASSWORD", "CHANGE_ME"),
    database=os.getenv("MYSQL_DATABASE", "GHN_PROD_BAK"),
    charset="utf8mb4",
    connect_timeout=15,
)

# location_id -> new body
BODIES: dict[str, str] = {}

BODIES["chennai"] = """**Zone by The Park, Chennai ORR** — a vibrant 4-star hotel built for business travellers, families and relaxed city getaways.

🏨 **Relax & Recharge**
Swimming pool, spa treatments and a fitness centre.

🍽️ **Dining**
Multi-cuisine and live cooking stations at **Bazaar**, cocktails and evening entertainment at **Z-Bar**, kababs and barbeques by the pool at **Playa by Zone**.

🛏️ **Rooms & Suites**
Zone Rooms, Zone Suites with bathtubs, and Zone Suite Plus with a private terrace.

📍 **Location**
Just **10 km from Chennai Airport**.

👨‍👩‍👧 **Families**
Pool with a dedicated kids' area, and children up to 7 stay free without an extra bed.

⭐ **Current Offers**

• **20% off** when you book direct on the official website
• AMEX Cardholders: **20% off rooms**, **25% off suites**
• Early Bird: **15% off** the Best Available Rate

Ask me anything about the rooms, dining or the area."""

BODIES["coimbatore"] = """Coimbatore has two options, each suited to a different kind of stay.

### 🏨 Zone by The Park, Coimbatore

**Best for:** longer business or leisure stays, with the most dining and event space.

🍽️ Multi-cuisine and local Kongunadu specialities at **Bazaar**, drinks at **Z Bar** and the casual **Orange Zone Lounge**.
💼 Meeting rooms, banquet spaces and a business centre on **Avinashi Road**.
👨‍👩‍👧 A dedicated **FunZone** for kids, plus connecting rooms for group stays.
📍 **7–8 km from Coimbatore International Airport**, **3 km from the railway station**, **200 metres from the central bus stand**.

### ✈️ Zone Connect by The Park, Coimbatore

**Best for:** short stays, early flights and anyone who wants to be next to the airport.

📍 Just **1 km from Coimbatore International Airport**, handy for **Codissia Trade Fair Complex** and the nearby IT parks.
🍽️ Indian, South Indian, Chinese and Continental at **Cafe C**, drinks at **Co Bar**.
👨‍👩‍👧 Rooms and spacious suites, with a **FunZone** for families.
🏨 Non-smoking rooms throughout and complimentary Wi-Fi.

⭐ **Offers at both properties**

• **20% off** direct bookings on the official website
• AMEX Cardholders: **20% off rooms**, **25% off suites**
• Early Bird: **15% off** the Best Available Rate

Which one would you like to explore?"""

BODIES["darjeeling"] = """**Zone by The Park, Darjeeling** — a hillside hotel that blends modern comfort with the charm of the Queen of the Hills.

🌄 **Stunning Hill Stay**
Select rooms and suites offer **valley views**, private balconies and spacious interiors.

🍽️ **Relaxed Dining**
Multi-cuisine dishes at **Bazaar**, the all-day dining restaurant, plus in-room dining.

📍 **Explore with Ease**
Near **Mall Road and Chowrasta**, with easy access to **Mahakal Temple**, **Tiger Hill**, **Happy Valley Tea Garden** and the **Darjeeling Himalayan Railway**.

👨‍👩‍👧 **Families & Getaways**
A dedicated **FunZone** and **Khel Zone** with indoor activities, and spacious suites for longer stays.

⭐ **Current Offers**

• **15% off** with the Early Bird Offer when you book at least **7 days ahead** for stays longer than one night
• Exclusive direct booking benefits on the official website

Happy to go deeper on the rooms, dining or what's nearby."""

BODIES["digha"] = """**Zone by The Park, Digha** — a contemporary hotel for family vacations, weekend escapes and celebrations, minutes from Digha's main attractions.

🏨 **Relax, Refresh & Recharge**
Swimming pool, treatments at **Vitalia Spa**, and a fully equipped fitness centre.

🍽️ **Dining for Every Taste**
Multi-cuisine meals at **Bazaar**, beverages at **Liquiteria**, poolside dining at **Playa**. Pure vegetarian and Jain options available.

👨‍👩‍👧 **Great for Families**
Spacious rooms and a dedicated **FunZone for kids**.

📍 **Close to Everything**
Just **2 km from Digha Railway Station**, near **Udaypur Beach**, **Jagannath Temple** and **Amarabati Park**.

⭐ **Current Offers**

• **15% off** with Early Bird when you book **7 days ahead** for stays longer than one night
• **Up to 8% off** on stays of **3 nights or more** with Zone Extended Stays
• **Flat 8% off** on Friday, Saturday or Sunday stays

Ask me anything about the rooms, dining or the beach."""

BODIES["gopalpur"] = """**Zone by The Park, Gopalpur** — a contemporary beachfront hotel for family vacations, weekend getaways and destination celebrations.

🌊 **Beachside Setting**
Just **500 metres from Gopalpur Beach and the Gopalpur Lighthouse**.

🏨 **Relax & Rejuvenate**
Swimming pool, treatments at **Vitalia Spa**, and a fully equipped fitness centre.

🍽️ **Dining with a View**
Multi-cuisine at **Bazaar**, grills at the rooftop **Urban Spice Box**, cocktails at **Z Bar**, poolside dining at **Playa**.

👨‍👩‍👧 **Families & Leisure**
Spacious rooms, a **Kids Play Zone**, **FunZone**, **Khel Zone** and connecting rooms.

⭐ **Current Offers**

• **20% off** direct bookings on the official website
• AMEX Cardholders: **20% off rooms**, **25% off suites**
• Seasonal **Monsoon Offers** with added benefits

Happy to tell you more about the rooms, dining or the beach."""

BODIES["jaipur"] = """**Zone by The Park, Jaipur** — a stylish 4-star hotel combining contemporary comfort with warm hospitality, in **Bani Park**.

🏨 **Relax & Rejuvenate**
A **rooftop swimming pool**, a health club and spa treatments.

🍽️ **Dining with a Local Touch**
Multi-cuisine at **Bazaar**, grills at the rooftop **Playa by Zone**, handcrafted cocktails at **Z Bar**.

📍 **Discover the Pink City**
Just **2 km from Jaipur Railway Station**, within easy reach of **Hawa Mahal** and the **Old Pink City**.

👨‍👩‍👧 **For Every Kind of Stay**
Spacious rooms, family rooms and accessible rooms.

⭐ **Current Offers**

• **20% off** direct bookings on the official website
• AMEX Cardholders: **20% off rooms**, **25% off suites**
• Early Bird: **15% off** the Best Available Rate

Ask me anything about the rooms, dining or the city."""

BODIES["jammu"] = """**Zone by The Park, Jammu** — a contemporary 4-star hotel with modern comfort and smart technology, minutes from the railway station.

🏨 **Comfort for Every Stay**
Spacious rooms and suites with smart technology and comfortable workspaces.

🍽️ **Dining for Every Occasion**
Multi-cuisine and live cooking stations at **Bazaar**, cocktails at **Z-Bar**, light bites at the **Orange Zone / V-Lounge**.

📍 **Convenient Location**
**500 metres from Jammu Railway Station**, **6–7 km from Jammu Airport**, near **Raghunath Temple** and **Bahu Fort** — and a handy stop en route to **Katra**.

💼 **Business & Celebrations**
Banquet halls, meeting spaces and outdoor event areas, plus complimentary high-speed Wi-Fi and a 24-hour fitness centre.

⭐ **Current Offers**

• **20% off** direct bookings on the official website
• AMEX Cardholders: **20% off rooms**, **25% off suites**
• Early Bird: **15% off** the Best Available Rate

Happy to go deeper on the rooms, dining or the area."""

BODIES["kolkata"] = """Kolkata has two stays, each suited to a different kind of trip.

### 🏨 Zone by The Park, Kolkata (New Town)

**Best for:** business travel and premium, resort-style facilities.

🏊 **Rooftop pool**, **Vitalia Spa**, a fitness centre and a **FunZone**.
🍽️ Regional and international cuisine at **Bazaar**, cocktails at **Z-Bar**, plus the **Lobby Bar**.
📍 **10 minutes from Kolkata Airport**, minutes from **Biswa Bangla Convention Centre**, **Sector V**, **Ecospace** and **Tata Medical Centre**.
💼 Multiple banquet halls, boardrooms and event spaces.

### 🏛️ Zone by The Park, New Market Kolkata

**Best for:** heritage, shopping and being in the middle of the city.

🏙️ Moments from **New Market**, the **Indian Museum**, **Victoria Memorial** and **Howrah Bridge**.
🍽️ Global and local cuisine at **Bazaar**, plus the heritage tearoom **Flurys** in the hotel lobby.
🎉 A business centre, meeting facilities and a rooftop event space.
🛏️ Thoughtfully designed rooms, complimentary Wi-Fi and in-room dining.

⭐ **Offers at both properties**

• **15% off** with the Early Bird Offer when you book in advance
• AMEX Cardholders: **20% off rooms**, plus suite benefits where applicable
• Direct booking and seasonal offers may also be available

Which one would you like to explore?"""

BODIES["pathankot"] = """**Zone by The Park Pathankot** — a contemporary hotel and the gateway to **Jammu & Kashmir and Himachal Pradesh**.

🏨 **Comfort for Every Journey**
Well-appointed rooms and spacious suites with modern amenities.

🍽️ **Dining**
Multi-cuisine dishes and regional flavours at **Bazaar**, drinks at **Z Bar**.

📍 **Gateway to Northern India**
**5.6 km from Pathankot Airport**, close to **Pathankot Junction** and **Pathankot Cantt** stations, with easy access to **Dalhousie**, **Dharamshala**, **Nurpur Fort** and **Ranjit Sagar Dam**.

👨‍👩‍👧 **Business & Celebrations**
Versatile banquet spaces, business facilities, a **FunZone** for kids and a terrace mini gym.

⭐ **Current Offers**

• **20% off** direct bookings on the official website
• AMEX Cardholders: **20% off rooms**, **25% off suites**
• Early Bird: **15% off** the Best Available Rate
• **25% off food and soft beverages** with the seasonal dining offer

Ask me anything about the rooms, dining or the hill routes."""

BODIES["goa"] = """Goa has two stays, each with its own character.

### 🌴 Zone Connect by The Park, Calangute Goa

**Best for:** the lively beach scene and a vibrant resort atmosphere.

🏖️ **1.4 km from Calangute Beach** and **3.6 km from Baga Beach**.
🏊 An **adult and kids' swimming pool**, plus the poolside **Plunge**.
🍽️ Goan and multi-cuisine favourites at **Cafe C** — including **Goan Fish Curry Rice** — and cocktails at **Co-Bar**.
👨‍👩‍👧 Connecting rooms, a kids' play area, baby pool and spacious suites.

### 🌿 Zone Connect by The Park, Parra Goa

**Best for:** a quieter, more relaxed escape that's still close to the beaches.

🌴 In the peaceful village of **Parra**, near **Calangute Beach**, **Baga Beach** and **Aguada Fort**.
🏊 A **swimming pool**, **kids' pool**, **jacuzzi** and an on-site spa.
🍽️ Multi-cuisine at **Cafe C** with Goan favourites like **Chamucas**, **Chanya Tonak** and **Ros Omelette**.
👨‍👩‍👧 Family rooms, spacious suites, complimentary Wi-Fi and a **FunZone**.

⭐ **Offers at both properties**

• **20% off** direct bookings on the official website
• Early Bird: **15% off** the Best Available Rate
• Seasonal offers may also apply depending on your dates

Which one would you like to explore?"""

BODIES["jaisalmer"] = """**Zone Connect by The Park Jaisalmer** — a contemporary hotel in the **Golden City of Rajasthan**, close to the fort and the desert.

🏜️ **Discover the Golden City**
**5 km from Jaisalmer Airport** and **2 km from the railway station**, with easy access to **Jaisalmer Fort**, **Gadisar Lake**, the **War Museum** and the **Old City**.

🏊 **Relax After Exploring**
An **outdoor swimming pool**, complimentary Wi-Fi, and indoor and outdoor games.

🍽️ **Flavours of Rajasthan**
Indian and Chinese cuisine at **Cafe C**, including **Dal Baati Churma** and **Chicken Rajasthani**, plus room service.

🎉 **Leisure & Celebrations**
Banquet facilities, outdoor event spaces and wedding venues.

⭐ **Current Offers**

• **20% off** direct bookings on the official website
• AMEX Cardholders: **20% off rooms**, **25% off suites**
• Early Bird offers subject to availability

Happy to tell you more about the rooms, dining or the desert trips."""

BODIES["katra"] = """**Zone Connect by The Park, Katra** — a contemporary hotel minutes from the pilgrimage route to the **Shri Mata Vaishno Devi Shrine**.

🛕 **Built for Pilgrims**
**1 km from Katra Railway Station** and close to the shrine, with easy access to **Ban Ganga**, **Nau Devi Cave Temple**, **Baba Dhansar** and **Shivkhori**.

🍽️ **Pure Vegetarian Dining**
Indian and Chinese cuisine at **Cafe C**, including **Kashmiri Dum Aloo**, with **24-hour room service**.

👨‍👩‍👧 **Families & Groups**
**Family Rooms** and **Trio Rooms**, complimentary Wi-Fi and a **FunZone** for children.

🎉 **Events & Gatherings**
**Townhall** for larger celebrations and **Grey Zone** for meetings.

⭐ **Current Offers**

• **15% off** with the Early Bird Offer when you book in advance
• Direct booking benefits depending on your dates
• Seasonal offers through the year

Ask me anything about the rooms, dining or the yatra."""

BODIES["lansdowne"] = """**Zone Connect by The Park, Lansdowne** — a 4-star retreat in the pine forests, for guests who want to unwind in nature with modern comforts.

🌿 **A Peaceful Mountain Escape**
On **Tarkeshwar Road**, with valley views and easy access to **Bhulla Lake**, **Tip In Top Point**, **Saint Thomas Church**, **Tarkeshwar Temple** and **Kalagarh Tiger Reserve**.

🏊 **Relax & Reconnect**
A **plunge pool**, plus **forest trekking**, **bird watching** and adventure activities.

🍽️ **Comforting Dining**
Multi-cuisine meals at **Cafe C**, and a traditional **Pahadi Thali** on request.

🐾 **Pet-Friendly**
Pets are welcome — a good choice if you'd rather not leave them behind.

🎉 **Small Gatherings & Retreats**
Event spaces surrounded by greenery, suited to intimate celebrations and corporate off-sites.

⭐ **Current Offers**

• **20% off** direct bookings on the official website
• AMEX Cardholders: **20% off rooms**, **25% off suites**
• Early Bird: **15% off** the Best Available Rate

Happy to go deeper on the rooms, dining or the treks."""

BODIES["gangtok"] = """**Zone Connect by The Park Mount Siniolchu, Gangtok** — a mountain hotel with views of the **Kanchenjunga range**.

🏔️ **Wake Up to Himalayan Views**
Most rooms look out over the **Kanchenjunga range**.

🍽️ **Dining with a View**
Multi-cuisine and local Sikkimese flavours at **Cafe C**, plus drinks and music at **Co Bar**.

📍 **Close to the Attractions**
**280 metres from Enchey Monastery** and **2.7 km from MG Marg**, with easy access to **Ganesh Tok**, **Hanuman Tok**, **Banjhakri Falls** and **Do Drul Chorten**.

👨‍👩‍👧 **Comfortable Mountain Stay**
Designed for the cool climate, with complimentary Wi-Fi and a kids' play area.

⭐ **Current Offers**

• **15% off** with the Early Bird Offer when you book in advance
• Direct booking benefits depending on your dates
• Seasonal offers through the year

Ask me anything about the rooms, dining or the sightseeing."""

BODIES["mussoorie"] = """**Zone Connect by The Park, Mussoorie** — a stylish 4-star hotel steps from Mall Road in the **Queen of the Hills**.

🏔️ **Right by the Attractions**
**100 metres from Mall Road**, with easy access to **Gun Hill**, **Picture Palace**, **Landour Lal Tibba**, **George Everest Peak**, **Kempty Falls**, **Jharipani Falls** and **Cloud's End**.

🍽️ **Dining with Local Flavours**
Multi-cuisine meals at **Cafe C**, including the signature **Pahadi Chicken**, plus **outdoor dining** with prior reservation.

🐾 **Pet-Friendly**
Pets are welcome in **Luxe Rooms** on a chargeable basis.

👨‍👩‍👧 **Families & Leisure**
Mountain-view terraces or balconies, complimentary Wi-Fi, a **FunZone** for kids and banquet facilities.

⭐ **Current Offers**

• **20% off** direct bookings on the official website
• Web-only rates with **early check-in and late check-out** (subject to availability)
• Early Bird: **15% off** the Best Available Rate
• Seasonal offers depending on your dates

Happy to tell you more about the rooms, dining or the sightseeing."""

BODIES["patna"] = """**Zone Connect by The Park Patna** — a contemporary hotel with stylish rooms, modern amenities and expansive event spaces.

🏨 **Comfort Meets Convenience**
Spacious rooms, poolside cottages and suites, with complimentary high-speed Wi-Fi and comfortable workspaces.

🏊 **Relax & Recharge**
A **plunge pool**, a **fitness centre** and a dedicated **FunZone**. *(The spa is under development and expected to open soon.)*

🍽️ **Regional Flavours & Fresh Bakes**
Regional and global cuisine at **Cafe C** — including **Bihari Mutton Curry** and **Martan ka Paneer** — plus coffee and fresh bakes at **The Bakery**.

📍 **Explore Patna**
Easy access to **Golghar**, **Patna Museum** and **Gandhi Maidan**, with good airport and railway connectivity.

🎉 **Weddings & Events**
A **6,000 sq. ft. Townhall**, a **20,000 sq. ft. landscaped lawn** and meeting rooms.

⭐ **Current Offers**

• **20% off** direct bookings on the official website
• **Early check-in and late check-out** (subject to availability) on direct bookings
• Early Bird: **15% off** the Best Available Rate

Ask me anything about the rooms, dining or the event spaces."""

BODIES["port_blair"] = """**Zone Connect by The Park Sri Vijaya Puram (Port Blair)** — a contemporary hotel surrounded by backwaters, with lake views and easy access to the islands' highlights.

🌊 **A Scenic Island Retreat**
**Lake and pool views**, **7 km from Veer Savarkar International Airport** and on the route to **Wandoor Beach**.

🏊 **Relax & Unwind**
A **plunge pool**, a **kids' pool** and an on-site spa.

🍽️ **Flavours for Every Taste**
Indian, Chinese, Continental and Tandoor at **Cafe C**, plus cocktails at **Co-Bar**.

📍 **Explore the Andamans**
Easy access to **Cellular Jail**, **Corbyn's Cove Beach**, **Wandoor Beach**, **Kalapani Museum** and **Mahatma Gandhi Marine National Park**.

🎉 **Celebrations & Business**
The **Grey Zone Boardroom** and a lakeside **Open Lawn**.

⭐ **Current Offers**

• **20% off** direct bookings on the official website
• **Early check-in and late check-out** (subject to availability) on direct bookings
• Early Bird: **15% off** the Best Available Rate
• Seasonal offers depending on your dates

Happy to go deeper on the rooms, dining or the island trips."""

BODIES["delhi"] = """**Zone Connect by The Park Saket Delhi** — a contemporary hotel in South Delhi, in the middle of the city's shopping and business district.

📍 **Heart of South Delhi**
In **Saket District Centre**, **100 metres from Select Citywalk** and next to **DLF Avenue** and **MGF Metropolitan Mall**. **Qutub Minar**, **Hauz Khas Village**, **Lotus Temple**, **Kiran Nadar Museum** and the **Museum of Illusions** are all close by.

🍽️ **Food & Rooftop Evenings**
Regional and international dishes at **Cafe C**, plus the open-air **Terrace Lounge (Co-Bar)**.

🛏️ **Smart, Comfortable Rooms**
Smart technology, complimentary high-speed Wi-Fi, spacious work areas and **connecting rooms** for families and groups.

🎉 **Business & Celebrations**
The **Townhall** conference space and the expansive **Terrace Lounge**.

⭐ **Current Offers**

• **20% off** direct bookings on the official website
• AMEX Cardholders: **20% off rooms**, **25% off suites**
• Early Bird: **15% off** the Best Available Rate

Ask me anything about the rooms, dining or getting around."""

BODIES["udaipur"] = """**Zone Connect by The Park Udaipur** — a contemporary hotel in **Fatehpura**, near the lakes and palaces of the **City of Lakes**.

🏰 **Discover the Best of Udaipur**
Easy access to **Fateh Sagar Lake**, **Pichola Lake**, **City Palace**, **Jagdish Temple**, **Saheliyon Ki Bari** and the **Vintage Car Museum**.

🍽️ **Pure Vegetarian Dining**
Multi-cuisine vegetarian, vegan and Jain-friendly dishes at **Cafe C**, plus **poolside outdoor dining**.

🏊 **Relax & Unwind**
A **plunge pool**, **foosball**, and rooms designed for both leisure and business.

🎉 **Celebrations & Events**
A spacious **Townhall** and a poolside lawn.

⭐ **Current Offers**

• **20% off** direct bookings on the official website
• **Early check-in and late check-out** (subject to availability) on direct bookings
• Early Bird: **15% off** the Best Available Rate, with early check-in between **10:00 AM and 11:00 AM** (subject to availability)
• Seasonal offers depending on your dates

Happy to tell you more about the rooms, dining or the lakes."""

BODIES["dimapur"] = """**Zone Niathu by The Park, Dimapur** — a resort-style hotel on **Asian Highway 1**, with the largest event grounds in Nagaland.

🌿 **A Resort in Nagaland**
Garden surroundings, **7 km from Dimapur Airport**.

🍽️ **Exceptional Dining**
• **Bazaar** — all-day multi-cuisine plus authentic **Naga delicacies**
• **Machang** — traditional Naga cuisine
• **Ruby Cafe** — coffee, light bites and an extended night menu
• **Playa by Zone** — poolside lounge
• **Splash Bar** — drinks and relaxed evenings

🏊 **Relax & Have Fun**
A **swimming pool**, **Vitalia Spa**, a **fitness centre**, and **badminton, futsal, volleyball, lawn tennis, basketball and pickleball**. Plus a **Children's Park**.

🎉 **Nagaland's Premier Event Destination**
**200,000 sq. ft.** of outdoor event space and an indoor **Town Hall**, with **Hornbill Festival packages** every **1–10 December**.

📍 **Explore Nagaland**
**Kohima Hill Station**, **Triple Falls**, **Kachari Ruins** and **Noune Resort**.

⭐ **Current Offers**

• **20% off** direct bookings on the official website
• AMEX Cardholders: **20% off rooms**, **25% off suites**
• Special **Hornbill Festival packages** (1–10 December)

Ask me anything about the rooms, dining or the festival packages."""

BODIES["phalodi"] = """**Zone Palace By The Park Phalodi** — a restored heritage hotel, originally built in **1760**, blending Rajasthani history with modern comforts.

🏛️ **Royal Heritage**
Rajasthani architecture and heritage interiors, attached to **Dhada's Heritage Museum of Jain & Oriental Arts**.

🍽️ **Authentic Vegetarian Cuisine**
Traditional Rajasthani flavours at **Bazaar**, including **Dal Baati Churma**, **Gulabjamun Ki Sabji**, **Methi Papad Ki Sabzi** and **Sarson Malai Phool**, plus drinks at **Z Bar**.

🏊 **Relax in a Heritage Setting**
An **outdoor pool** and a **FunZone** for children.

📍 **Discover Phalodi**
Near the **Jain Temple**, **Old Haveli**, the local **Market** and the **Khichan Bird Sanctuary**, where thousands of Demoiselle Cranes arrive each year.

🎉 **Celebrations**
Banquet facilities for up to **100 guests**.

⭐ **Current Offers**

• **20% off** direct bookings on the official website
• AMEX Cardholders: **20% off rooms**, **25% off suites**
• Early Bird offers during eligible booking periods
• Seasonal offers depending on your dates

Happy to go deeper on the rooms, dining or the bird sanctuary."""


def _backup_path() -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backups")
    os.makedirs(root, exist_ok=True)
    return os.path.join(root, f"zone_location_intros_{stamp}.json")


def _fetch(cur) -> list[dict]:
    cur.execute(
        "SELECT id, location_id, location_label, body FROM new_bot_location_intros "
        "WHERE hotel_id=%s ORDER BY id",
        (HOTEL_ID,),
    )
    return list(cur.fetchall())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--restore", default="")
    args = ap.parse_args()

    conn = pymysql.connect(**DB)
    cur = conn.cursor(pymysql.cursors.DictCursor)

    if args.restore:
        rows = json.load(open(args.restore, encoding="utf-8"))
        for row in rows:
            cur.execute(
                "UPDATE new_bot_location_intros SET body=%s WHERE id=%s AND hotel_id=%s",
                (row["body"], row["id"], HOTEL_ID),
            )
        conn.commit()
        print(f"restored {len(rows)} rows from {args.restore}")
        return 0

    current = _fetch(cur)
    by_loc = {r["location_id"]: r for r in current}

    missing = [k for k in by_loc if k not in BODIES]
    unknown = [k for k in BODIES if k not in by_loc]
    if missing:
        print(f"!! no new body written for: {missing}", file=sys.stderr)
    if unknown:
        print(f"!! new body has no DB row: {unknown}", file=sys.stderr)
    if missing or unknown:
        return 1

    backup = _backup_path()
    json.dump(current, open(backup, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"backup -> {backup}")

    for loc, body in BODIES.items():
        row = by_loc[loc]
        old_w, new_w = len(row["body"].split()), len(body.split())
        print(f"  {loc:12} {old_w:4} -> {new_w:4} words")
        if not args.dry_run:
            cur.execute(
                "UPDATE new_bot_location_intros SET body=%s WHERE id=%s AND hotel_id=%s",
                (body.strip(), row["id"], HOTEL_ID),
            )

    if args.dry_run:
        print("\nDRY RUN — nothing written")
        return 0

    conn.commit()
    print(f"\nupdated {len(BODIES)} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
