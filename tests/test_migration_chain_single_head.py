"""
The migration chain must have exactly one head, and no two migration files may
share a number. Two branches both adding a `0059_*` on top of `0058` (the
marketplace `0059_delivery_kitchen_flow` and the WhatsApp routing fix
`0059_whatsapp_routing_generation`) would leave Alembic with two heads, and
`alembic upgrade head` would refuse to run at deploy time. Whichever branch
merges second must renumber its migration on top of the other's.
"""
import os
import re
from collections import Counter

from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _script() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(os.path.join(ROOT, "alembic.ini")))


def test_exactly_one_head():
    heads = _script().get_heads()
    assert len(heads) == 1, f"multiple Alembic heads: {heads}"


def test_no_two_migrations_share_a_number():
    versions = os.path.join(ROOT, "shared", "migrations", "versions")
    numbers = Counter(m.group(1) for f in os.listdir(versions) if (m := re.match(r"^(\d{4})_.*\.py$", f)))
    duplicates = {n: c for n, c in numbers.items() if c > 1}
    assert not duplicates, f"duplicate migration numbers: {duplicates}"
