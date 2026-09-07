"""
Inventory domain - ingredients, recipes, the stock ledger, and variance.

Everything in this package obeys one storage rule: stock quantities are
integers counting thousandths ("milli") of an ingredient's canonical unit
(gram, millilitre, or whole unit). See units.py for why.
"""
