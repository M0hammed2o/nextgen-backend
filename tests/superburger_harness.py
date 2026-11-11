"""
Shared harness for the Super Burger multi-unit-cart regression tests
(2026-11-11 live-conversation incident).

Builds a Super-Burger-like fixture menu (option groups copied from
NextGen-Demo-SuperBurger/seed_super_burger.py: Patty Chicken R0 / Beef +R15 /
Wagyu +R100; included Side Fries / Onion rings / Potato skins at R0; plus the
STANDALONE "Onion Rings" R32 and "Regular Fries"/"Large Fries" items) and
drives it through the real ``pipeline._handle_message`` via the replay
framework's I/O fakes -- but turn by turn, so a test can script the LLM only
where the live behaviour depended on it and inspect the cart in between.
"""
from tests.replay.fixtures import FakeBusiness, FakeCustomer, FakeMenuItem
from tests.replay.framework import ReplayRunner
from backend.app.bot import state_machine


def _grp(gid, name, opts, required=True):
    return {
        "id": gid, "name": name, "required": required, "is_enabled": True,
        "options": [
            {"id": oid, "name": n, "price_delta_cents": c, "is_enabled": True}
            for oid, n, c in opts
        ],
    }


def _rm(*names):
    return [{"id": "rm-" + n.lower().replace(" ", "-"), "name": n} for n in names]


PATTY = _grp("grp-patty", "Patty",
             [("pt-chicken", "Chicken", 0), ("pt-beef", "Beef", 1500), ("pt-wagyu", "Wagyu", 10000)])
SIDE = _grp("grp-side", "Side",
            [("side-fries", "Fries", 0), ("side-rings", "Onion rings", 0), ("side-skins", "Potato skins", 0)])
GARNISH = _rm("Lettuce", "Onion", "Tomato", "Super Mayo")


def build_menu():
    return [
        FakeMenuItem("Super Burger", 12000, "Burgers", description="Big Korn bites and jalapenos. Served with a side.",
                     options_json={"option_groups": [PATTY, SIDE],
                                   "removable_ingredients": GARNISH + _rm("Jalapenos", "Big Korn bites")}),
        FakeMenuItem("Classic Burger", 10500, "Burgers",
                     options_json={"option_groups": [PATTY, SIDE], "removable_ingredients": GARNISH}),
        FakeMenuItem("Cheeseboy", 5000, "Burgers"),
        FakeMenuItem("Cheeseburger", 9500, "Burgers",
                     options_json={"removable_ingredients": GARNISH}),
        FakeMenuItem("Onion Rings", 3200, "Sides"),
        FakeMenuItem("Regular Fries", 3400, "Sides"),
        FakeMenuItem("Large Fries", 5500, "Sides"),
        FakeMenuItem("Classic Shake", 5500, "Desserts"),
        FakeMenuItem("Coke", 2500, "Cold Drinks"),
    ]


def attach_categories(items):
    """Real MenuCategory-like rows for the fixture's ``category_name`` values;
    also sets each item's category_id (FakeMenuItem has none)."""
    from types import SimpleNamespace
    cats: dict[str, SimpleNamespace] = {}
    for it in items:
        name = getattr(it, "_cat_name", None) or "Other"
        if name not in cats:
            cats[name] = SimpleNamespace(id=f"cat-{len(cats)}", name=name, sort_order=len(cats))
        it.category_id = cats[name].id
    return list(cats.values())


def build_big_menu():
    """~110 items in 11 categories with descriptions: far more than one
    WhatsApp message (the live Super Burger menu was several chunks)."""
    items = []
    base = build_menu()
    for it in base:
        it._cat_name = {"Burgers": "Burgers", "Sides": "Sides", "Desserts": "Desserts",
                        "Cold Drinks": "Cold Drinks"}.get(getattr(it, "_category_name", None) or "", None)
    # FakeMenuItem keeps no category name: derive from the fixture order
    names = ["Burgers"] * 4 + ["Sides"] * 3 + ["Desserts", "Cold Drinks"]
    for it, nm in zip(base, names):
        it._cat_name = nm
    items.extend(base)
    for n, cat in enumerate(["Steaks", "Smoked Meats", "Chicken", "Hot Dogs", "Platters", "Shakes", "Breakfast"]):
        for k in range(15):
            fi = FakeMenuItem(f"{cat[:-1] if cat.endswith('s') else cat} Special {k + 1}", 8000 + 100 * k, cat,
                              description="Slow cooked, hand finished and served with a generous side. " * 2)
            fi._cat_name = cat
            items.append(fi)
    return items, attach_categories(items)


def make_runner(menu=None):
    conv = {"id": "sb", "title": "super burger", "turns": []}
    r = ReplayRunner(conv, FakeBusiness(name="Super Burger"), FakeCustomer(), menu or build_menu())
    return r


class Chat:
    """Turn-by-turn driver. ``say(text, llm=None)`` -> bot reply text."""

    def __init__(self, menu=None, categories=None):
        from unittest.mock import patch
        self.r = make_runner(menu)
        if categories is not None:
            self.r.categories = categories
        self._p = patch("backend.app.bot.state_machine.flag_modified")
        self._p.start()
        self.replies: list[str] = []

    def close(self):
        self._p.stop()

    async def say(self, text, llm=None):
        if llm is not None:
            self.r.scripted_llm.enqueue(llm)
        reply = await self.r._execute(text)
        self.replies.append(reply)
        return reply

    @property
    def cart(self):
        return state_machine.get_cart(self.r.session)

    @property
    def ctx(self):
        return self.r.session.context_json or {}

    @property
    def state(self):
        return self.r.session.state

    def total(self):
        return sum(i["line_total_cents"] for i in self.cart)

    def summary(self):
        out = []
        for i in self.cart:
            opts = [(o.get("option_name") or o.get("name"), o.get("price_delta_cents")) for o in i.get("selected_options") or []]
            out.append((i["quantity"], i["name"], opts, i.get("unit_price_cents"), i["line_total_cents"], i.get("special_instructions")))
        return out
