# In-Room Dining — full menu availability classification

**Sarovar Portico Sriperumbudur · 164 dishes · generated 2026-08-27**

Every dish on the menu, classified by when it is served. Produced by walking the
menu itself (`overnight/classify_menu.py`), so it can be regenerated whenever the
menu changes rather than maintained by hand.

This is what `config/ird_serving_windows.json` encodes and what the bot reads.

---

## The three periods

| period | hours | dishes |
|---|---|---|
| `breakfast` | 07:00 – 10:30 | **18** |
| `tandoor` | 12:00 – 15:00 and 19:00 – 23:00 | **9** |
| `all_day` | any hour | **137** |

Only the first two are restrictions. Everything else can be ordered at any hour.

---

## BREAKFAST — 07:00 to 10:30 only


**BREAKFAST › WESTERN DELICACIES**

- Baker's Basket
- Bircher Muesli
- Cereal
- Egg to Order
- Freshly Squeezed Juices
- Pancake / Waffle / French Toast
- Seasonal Sliced Fruit Platter
- Smoothie Bowl
- Yogurt

**IN ROOM DINING MENU › BREAKFAST**

- American Breakfast
- Continental Breakfast
- Indian Breakfast

**IN ROOM DINING MENU › INDIAN DELICACIES**

- Dosa (Plain or Masala)
- Idli
- Medu Vada
- Paratha (Aloo or Paneer)
- Poori Bhaji
- Uttappam

## TANDOOR — 12:00–15:00 and 19:00–23:00 only


**INDIAN SELECTION › APPETIZERS**

- Ajwaini Jhinga
- Jaitooni Paneer Tikka
- Kasundi Macchi Tikka
- Mushroom Galouti
- Tandoori Murgh
- Zaffrani Murgh Tikka

**INDIAN SELECTION › BREAD**

- Kulcha
- Naan
- Tandoori Roti

---

## ALL DAY — no restriction

137 dishes, never refused on timing.


**CLASSIC HOT CHOCOLATE › TEA MENU**

- Classic Matcha Tea
- Classic Teas
- Herbal & Wellness Teas (Caffeine-Free)
- Premium Leaf Teas
- Seasonal Iced Teas
- Signature Infusions

**COMFORT MEAL IN A BOWL › ARTFUL DESSERTS**

- Ada Pradhaman
- Blue Berry Cheese Cake
- Chocolate Walnut Brownie
- Elaneer Payasam (Sarovar Signature)
- Ice Cream
- Rasmalai
- Tiramisu

**COMFORT MEAL IN A BOWL › INDIAN**

- Butter Chicken Bowl
- Paneer Aur Shrikewale Pyaz Ki Handi Bowl

**COMFORT MEAL IN A BOWL › INDONESIAN SATAY BOWL**

- Indonesian Satay Bowl

**COMFORT MEAL IN A BOWL › JAPANESE**

- Chicken Yakitori Bowl
- Seafood Yakitori Bowl
- Vegetable Yakitori Bowl

**COMFORT MEAL IN A BOWL › KOREAN**

- Chicken Korean Bowl
- Vegetable Korean Bowl

**COMFORT MEAL IN A BOWL › ORIENTAL**

- Kung Pao Chicken pao chicken Bowl
- Mapo Tofu Bowl

**COMFORT MEAL IN A BOWL › SOUTH INDIAN**

- Kalan Pattani Chettinadu Bowl
- Kozhi Chettinadu Bowl

**COMFORT MEAL IN A BOWL › THAI**

- Chicken Thai Bowl
- Prawn Thai Bowl
- Vegetable Thai Bowl

**FRESH COLD PRESSED JUICES › HOT COFFEES**

- Americano
- Cappuccino
- Espresso
- Filter Coffee (South Indian Style)
- Flat White
- Latte

**INDIAN SELECTION › BREAD**

- Kerala Paratha
- Lachha Paratha
- Phulka

**INDIAN SELECTION › MAIN COURSE**

- Aloo Anardana (Sarovar Signature Dish)
- Amchuri Bhindi
- Bhuna Gosht
- Butter Chicken
- Khade Masale Ka Murgh
- Mirch Makai Hari Pyaz
- Paneer Aur Shrikewale Pyaz Ki Handi
- Raan (Serves 4) (Sarovar Signature Dish)

**INDIAN SELECTION › RICE**

- Curd Rice
- Dal Khichdi
- Jeera Matar Pulao
- Murgh Dum Biryani
- Steamed Rice
- Subz Dum Biryani

**INDIAN SELECTION › STAPLES**

- Dal Makhani
- Dal Tadka
- Pindi Chole

**INTERNATIONAL SELECTION › APPETIZERS**

- Avocado on Toast
- Bruschetta
- Mezze Platter
- Peri-Peri Prawn
- Som Tam (Prawn)
- Som Tam (Vegetable)

**INTERNATIONAL SELECTION › BAO SLIDERS**

- Sriracha Chicken Tempura Bao
- Tofu Hoisin Bao

**INTERNATIONAL SELECTION › CRAFT YOUR OWN PIZZA**

- Margherita Pizza (Base Pizza)

**INTERNATIONAL SELECTION › MAIN COURSE**

- Ratatouille Cannelloni

**INTERNATIONAL SELECTION › PASTA**

- AOP (Aglio Olio Peperoncino)
- Alfredo
- Arrabbiata
- Bolognaise
- Carbonara
- Pasta of Your Choice
- Pesto

**INTERNATIONAL SELECTION › REGIONAL SPECIALITIES**

- Chicken Pallipalayam
- Era Thokku
- Kaipoo Varuval
- Kalan Pattani Chettinadu
- Kozhi Chettinadu
- Manga Meen Kozhambu
- Mushroom Pepper Fry
- Mutton Kola Urundai

**INTERNATIONAL SELECTION › RISOTTO**

- Balsamic Chicken Risotto
- Cajun Grilled Chicken
- Duo of Mushroom Risotto
- Fish & Chips
- Potato Spaghetti Fish
- Seafood Platter (Sarovar Signature Dish)

**INTERNATIONAL SELECTION › SANDWICHES AND MORE**

- Balsamic Grilled Vegetable & Brie in Ciabatta
- Grilled Sandwich in Panini
- Grilled Sandwich in Panini (Chicken)

**INTERNATIONAL SELECTION › SOUPS**

- Soup of the Day (Non-Vegetarian)
- Soup of the Day (Vegetarian)

**INTERNATIONAL SELECTION › THE ULTIMATE BURGER EXPERIENCE**

- Chicken Burger
- Vegetable Burger

**MID NIGHT MENU › DUM BIRYANI**

- Chicken Dum Biryani
- Vegetable Dum Biryani

**MID NIGHT MENU › SANDWICHES**

- Grilled Sandwich in Panini (Vegetarian)

**MID NIGHT MENU › SOUP OF THE DAY**

- Non-Vegetarian
- Vegetarian

**MIXOLOGIST INSPIRATION › FERMENT COLLECTION – SOUTH INDIAN AND UMAMI INSPIRATIONS**

- Beetroot Kanji
- Black Carrot Kanji
- Ginger Patham
- Hing Hustle
- Malabar Breeze

**PAN ASIAN › FRIED RICE**

- Chicken Fried Rice
- Prawn Fried Rice
- Vegetable Fried Rice

**PAN ASIAN › HAKKA NOODLE**

- Chicken Hakka Noodle
- Prawn Hakka Noodle
- Vegetable Hakka Noodle

**PAN ASIAN › MAIN COURSE**

- Braised Tofu Clay Pot
- Lotus Stem, Asparagus & Water Chestnut
- Mapo Tofu
- Pad KraProw Gai
- Steamed Indian Seabass
- Stir Fried Broccoli with Mushroom

**PAN ASIAN › SOUPS**

- Manchow Soup (Chicken)
- Manchow Soup (Prawn)
- Manchow Soup (Vegetable)
- Miso Soup (Chicken)
- Miso Soup (Prawn)
- Miso Soup (Vegetable)
- Sweet Corn Soup (Chicken)
- Sweet Corn Soup (Prawn)
- Sweet Corn Soup (Vegetable)
- Ton Kha Soup (Chicken)
- Ton Kha Soup (Prawn)
- Ton Kha Soup (Vegetable)

**PAN ASIAN › STARTERS**

- Chicken Siu Mai
- Chicken in Black Pepper Sauce
- Crispy Tofu
- Duck Spring Roll
- Edamame & Asparagus Dim Sum
- Golden Fried Prawn with Hot Garlic Sauce
- Mushroom Salt & Pepper
- Vietnamese Vegetable Roll

**PAN ASIAN › THAI CURRY**

- Thai Red & Green Curry (Chicken)
- Thai Red & Green Curry (Prawn)
- Thai Red & Green Curry (Vegetable)

---

## Needs the hotel to confirm

Three judgement calls. Everything else comes straight from the menu's own text.

### 1. Which appetizers are "Tandoori Kebabs"?

The menu says *"All Tandoori Kebabs will be available from 12 noon to 3 pm and
7 pm to 11 pm"* but never says which dishes it means. Only **Tandoori Murgh** has
the word in its name. All six are currently restricted — tick any that should NOT be:

- [ ] Mushroom Galouti
- [ ] Jaitooni Paneer Tikka
- [ ] Ajwaini Jhinga
- [ ] Kasundi Macchi Tikka
- [ ] Tandoori Murgh
- [ ] Zaffrani Murgh Tikka

### 2. Which breads are "Tandoori Breads"?

Same problem, same note. Currently split like this:

| bread | treated as | orderable |
|---|---|---|
| Naan | tandoor | 12–3 and 7–11 only |
| Tandoori Roti | tandoor | 12–3 and 7–11 only |
| Kulcha | tandoor | 12–3 and 7–11 only |
| Lachha Paratha | tawa | any hour |
| Phulka | tawa | any hour |
| Kerala Paratha | tawa | any hour |

### 3. When is the MID NIGHT MENU served?

The menu states **no hours at all** for it, so these are currently orderable at any
hour — including 2 pm. The property KB says *"24-hour room service, limited menu
after 23:00"*. If they should be late-night only, they move to a `late_night`
period — already built and tested, currently dormant.

- [ ] SOUP OF THE DAY › Vegetarian
- [ ] SOUP OF THE DAY › Non-Vegetarian
- [ ] SANDWICHES › Grilled Sandwich in Panini (Vegetarian)
- [ ] DUM BIRYANI › Vegetable Dum Biryani
- [ ] DUM BIRYANI › Chicken Dum Biryani

---

## It updates itself

Nothing is fixed to a time of day. Every turn resolves the clock afresh:

| time of day | dishes unavailable |
|---|---|
| 08:19 | 9 |
| 11:00 | 27 |
| 13:00 | 18 |
| 16:00 | 27 |
| 20:00 | 18 |
| 23:30 | 27 |

A dish flips the moment its window opens or closes, mid-conversation if need be.
No restart, no cache, no manual step.
