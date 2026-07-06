import json, os

tests = []

# MR-001: Swap Sprite for Coke
tests.append({
    "id": "mr_001",
    "title": "Modifier replace — Change Sprite to Coke (item swap)",
    "category": "modifier_replace",
    "tags": ["llm", "replace_item", "item_swap", "cart_integrity"],
    "notes": "CSB and Sprite in cart. Customer swaps Sprite for Coke. Cart must have CSB + Coke only.",
    "turns": [
        {"message": "Can I get a Classic Smash Burger and a Sprite",
         "expect": {"state": "CONFIRMING_ORDER", "cart": [{"name": "Classic Smash Burger", "quantity": 1}, {"name": "Sprite (330ml)", "quantity": 1}]}},
        {"message": "Change the Sprite to a Coke",
         "llm": {"action": "replace_item", "message": "Swapped Sprite for Coca-Cola done!",
                 "items": [{"remove": "Sprite (330ml)", "add": "Coca-Cola (330ml)", "quantity": 1, "options": {}, "add_ons": [], "special_instructions": ""}]},
         "expect": {"state": "CONFIRMING_ORDER", "cart": [{"name": "Classic Smash Burger", "quantity": 1}, {"name": "Coca-Cola (330ml)", "quantity": 1}], "response_contains": ["coca-cola"]}},
        {"message": "Yes", "expect": {"state": "CHOOSING_ORDER_MODE"}},
        {"message": "Pickup", "expect": {"state": "COLLECTING_DETAILS"}},
        {"message": "Thabo", "expect": {"state": "COLLECTING_DETAILS"}},
        {"message": "0821234567", "expect": {"state": "ORDER_PLACED", "cart": []}}
    ],
    "final_order": {"items": [{"name": "Classic Smash Burger", "quantity": 1}, {"name": "Coca-Cola (330ml)", "quantity": 1}], "subtotal_cents": 9500, "total_cents": 9500, "order_mode": "PICKUP", "customer_name": "Thabo"}
})

# MR-002: Swap Chips for Onion Rings
tests.append({
    "id": "mr_002",
    "title": "Modifier replace — Swap Chips for Onion Rings",
    "category": "modifier_replace",
    "tags": ["llm", "replace_item", "item_swap", "cart_integrity"],
    "notes": "CSB + Chips. Customer swaps Chips for Onion Rings. Final: CSB + Onion Rings. Subtotal R75+R30=R105.",
    "turns": [
        {"message": "Can I get a Classic Smash Burger and Chips",
         "expect": {"state": "CONFIRMING_ORDER", "cart": [{"name": "Classic Smash Burger", "quantity": 1}, {"name": "Chips", "quantity": 1}]}},
        {"message": "Swap the Chips for Onion Rings",
         "llm": {"action": "replace_item", "message": "Swapped Chips for Onion Rings done!",
                 "items": [{"remove": "Chips", "add": "Onion Rings", "quantity": 1, "options": {}, "add_ons": [], "special_instructions": ""}]},
         "expect": {"state": "CONFIRMING_ORDER", "cart": [{"name": "Classic Smash Burger", "quantity": 1}, {"name": "Onion Rings", "quantity": 1}], "response_contains": ["onion rings"]}},
        {"message": "Yes", "expect": {"state": "CHOOSING_ORDER_MODE"}},
        {"message": "Pickup", "expect": {"state": "COLLECTING_DETAILS"}},
        {"message": "Zanele", "expect": {"state": "COLLECTING_DETAILS"}},
        {"message": "0829876543", "expect": {"state": "ORDER_PLACED", "cart": []}}
    ],
    "final_order": {"items": [{"name": "Classic Smash Burger", "quantity": 1}, {"name": "Onion Rings", "quantity": 1}], "subtotal_cents": 10500, "total_cents": 10500, "order_mode": "PICKUP", "customer_name": "Zanele"}
})

# MR-003: Upgrade burger conversationally
tests.append({
    "id": "mr_003",
    "title": "Modifier replace — Upgrade burger (conversational item swap)",
    "category": "modifier_replace",
    "tags": ["llm", "replace_item", "item_swap", "conversational"],
    "notes": "CSB in cart. Customer says 'Actually make it a Double Smash Burger'. No duplicate burger.",
    "turns": [
        {"message": "Can I get a Classic Smash Burger",
         "expect": {"state": "CONFIRMING_ORDER", "cart": [{"name": "Classic Smash Burger", "quantity": 1}]}},
        {"message": "Actually make it a Double Smash Burger",
         "llm": {"action": "replace_item", "message": "Upgraded to Double Smash Burger R95 done!",
                 "items": [{"remove": "Classic Smash Burger", "add": "Double Smash Burger", "quantity": 1, "options": {}, "add_ons": [], "special_instructions": ""}]},
         "expect": {"state": "CONFIRMING_ORDER", "cart": [{"name": "Double Smash Burger", "quantity": 1}], "response_contains": ["double smash burger"]}},
        {"message": "Yes", "expect": {"state": "CHOOSING_ORDER_MODE"}},
        {"message": "Pickup", "expect": {"state": "COLLECTING_DETAILS"}},
        {"message": "Kabelo", "expect": {"state": "COLLECTING_DETAILS"}},
        {"message": "0824567890", "expect": {"state": "ORDER_PLACED", "cart": []}}
    ],
    "final_order": {"items": [{"name": "Double Smash Burger", "quantity": 1}], "subtotal_cents": 9500, "total_cents": 9500, "order_mode": "PICKUP", "customer_name": "Kabelo"}
})

# MR-004: Compound remove cheese + add patty (LLM replace_item)
tests.append({
    "id": "mr_004",
    "title": "Modifier replace — Remove extra cheese and add extra patty (compound LLM)",
    "category": "modifier_replace",
    "tags": ["llm", "replace_item", "addon_swap", "compound", "cart_integrity", "pricing"],
    "notes": "LSB + Extra Cheese (R85). Compound: remove cheese + add patty. LLM replace_item. Final: LSB + Extra Patty (R100).",
    "turns": [
        {"message": "Loaded Smash Burger with extra cheese",
         "llm": {"action": "add_items", "message": "LSB with Extra Cheese R85 done!",
                 "items": [{"name": "Loaded Smash Burger", "quantity": 1, "options": {}, "add_ons": [{"name": "Extra Cheese", "quantity": 1}], "special_instructions": ""}]},
         "expect": {"state": "CONFIRMING_ORDER", "cart": [{"name": "Loaded Smash Burger", "quantity": 1}]}},
        {"message": "Remove extra cheese and add extra patty",
         "llm": {"action": "replace_item", "message": "Done R100 now!",
                 "items": [{"remove": "Loaded Smash Burger", "add": "Loaded Smash Burger", "quantity": 1, "options": {}, "add_ons": [{"name": "Extra Patty", "quantity": 1}], "special_instructions": ""}]},
         "expect": {"state": "CONFIRMING_ORDER", "cart": [{"name": "Loaded Smash Burger", "quantity": 1}], "response_contains": ["extra patty"]}},
        {"message": "Yes", "expect": {"state": "CHOOSING_ORDER_MODE"}},
        {"message": "Pickup", "expect": {"state": "COLLECTING_DETAILS"}},
        {"message": "Sipho", "expect": {"state": "COLLECTING_DETAILS"}},
        {"message": "0834567890", "expect": {"state": "ORDER_PLACED", "cart": []}}
    ],
    "final_order": {"items": [{"name": "Loaded Smash Burger", "quantity": 1}], "subtotal_cents": 10000, "total_cents": 10000, "order_mode": "PICKUP", "customer_name": "Sipho"}
})

# MR-005: Replace cheese with bacon
tests.append({
    "id": "mr_005",
    "title": "Modifier replace — Replace extra cheese with extra bacon",
    "category": "modifier_replace",
    "tags": ["llm", "replace_item", "addon_swap", "cart_integrity", "pricing"],
    "notes": "LSB + Extra Cheese (R85). Replace with Extra Bacon (R15). Final: LSB + Extra Bacon (R90).",
    "turns": [
        {"message": "Loaded Smash Burger with extra cheese",
         "llm": {"action": "add_items", "message": "LSB with Extra Cheese R85 done!",
                 "items": [{"name": "Loaded Smash Burger", "quantity": 1, "options": {}, "add_ons": [{"name": "Extra Cheese", "quantity": 1}], "special_instructions": ""}]},
         "expect": {"state": "CONFIRMING_ORDER", "cart": [{"name": "Loaded Smash Burger", "quantity": 1}]}},
        {"message": "Replace the extra cheese with extra bacon",
         "llm": {"action": "replace_item", "message": "Swapped Extra Cheese for Extra Bacon R90 done!",
                 "items": [{"remove": "Loaded Smash Burger", "add": "Loaded Smash Burger", "quantity": 1, "options": {}, "add_ons": [{"name": "Extra Bacon", "quantity": 1}], "special_instructions": ""}]},
         "expect": {"state": "CONFIRMING_ORDER", "cart": [{"name": "Loaded Smash Burger", "quantity": 1}], "response_contains": ["extra bacon"]}},
        {"message": "Yes", "expect": {"state": "CHOOSING_ORDER_MODE"}},
        {"message": "Pickup", "expect": {"state": "COLLECTING_DETAILS"}},
        {"message": "Lungelo", "expect": {"state": "COLLECTING_DETAILS"}},
        {"message": "0843210987", "expect": {"state": "ORDER_PLACED", "cart": []}}
    ],
    "final_order": {"items": [{"name": "Loaded Smash Burger", "quantity": 1}], "subtotal_cents": 9000, "total_cents": 9000, "order_mode": "PICKUP", "customer_name": "Lungelo"}
})

# MR-006: Add add-on via DET
tests.append({
    "id": "mr_006",
    "title": "Modifier replace — Add extra cheese via DET in CONFIRMING_ORDER",
    "category": "modifier_replace",
    "tags": ["det", "addon_add", "cart_integrity", "pricing"],
    "notes": "Plain LSB (R75). 'Extra cheese please' in CONFIRMING_ORDER. DET_ADDON_ADD fires. Total R85.",
    "turns": [
        {"message": "Can I get a Loaded Smash Burger",
         "expect": {"state": "CONFIRMING_ORDER", "cart": [{"name": "Loaded Smash Burger", "quantity": 1}]}},
        {"message": "Extra cheese please",
         "expect": {"state": "CONFIRMING_ORDER", "cart": [{"name": "Loaded Smash Burger", "quantity": 1}], "response_contains": ["extra cheese"]}},
        {"message": "Yes", "expect": {"state": "CHOOSING_ORDER_MODE"}},
        {"message": "Pickup", "expect": {"state": "COLLECTING_DETAILS"}},
        {"message": "Noxolo", "expect": {"state": "COLLECTING_DETAILS"}},
        {"message": "0856789012", "expect": {"state": "ORDER_PLACED", "cart": []}}
    ],
    "final_order": {"items": [{"name": "Loaded Smash Burger", "quantity": 1}], "subtotal_cents": 8500, "total_cents": 8500, "order_mode": "PICKUP", "customer_name": "Noxolo"}
})

# MR-007: Remove add-on via DET
tests.append({
    "id": "mr_007",
    "title": "Modifier replace — Remove extra cheese via DET Sub-case B2",
    "category": "modifier_replace",
    "tags": ["det", "addon_remove", "cart_integrity", "pricing"],
    "notes": "LSB + Extra Cheese (R85). 'Remove the extra cheese' — DET Sub-case B2. Final: LSB only (R75). Burger MUST survive.",
    "turns": [
        {"message": "Loaded Smash Burger with extra cheese",
         "llm": {"action": "add_items", "message": "LSB with Extra Cheese R85 done!",
                 "items": [{"name": "Loaded Smash Burger", "quantity": 1, "options": {}, "add_ons": [{"name": "Extra Cheese", "quantity": 1}], "special_instructions": ""}]},
         "expect": {"state": "CONFIRMING_ORDER", "cart": [{"name": "Loaded Smash Burger", "quantity": 1}]}},
        {"message": "Remove the extra cheese",
         "expect": {"state": "CONFIRMING_ORDER", "cart": [{"name": "Loaded Smash Burger", "quantity": 1}], "response_contains": ["removed"]}},
        {"message": "Yes", "expect": {"state": "CHOOSING_ORDER_MODE"}},
        {"message": "Pickup", "expect": {"state": "COLLECTING_DETAILS"}},
        {"message": "Siphamandla", "expect": {"state": "COLLECTING_DETAILS"}},
        {"message": "0845678901", "expect": {"state": "ORDER_PLACED", "cart": []}}
    ],
    "final_order": {"items": [{"name": "Loaded Smash Burger", "quantity": 1}], "subtotal_cents": 7500, "total_cents": 7500, "order_mode": "PICKUP", "customer_name": "Siphamandla"}
})

# MR-008: Remove one of two add-ons, other survives
tests.append({
    "id": "mr_008",
    "title": "Modifier replace — Remove one of two add-ons (other must survive)",
    "category": "modifier_replace",
    "tags": ["det", "addon_remove", "multi_addon", "cart_integrity", "pricing"],
    "notes": "LSB + Extra Cheese + Extra Patty (R110). Remove only Extra Cheese. Final: LSB + Extra Patty (R100). Patty MUST survive.",
    "turns": [
        {"message": "Loaded Smash Burger with extra cheese and extra patty",
         "llm": {"action": "add_items", "message": "LSB with Extra Cheese and Extra Patty R110 done!",
                 "items": [{"name": "Loaded Smash Burger", "quantity": 1, "options": {}, "add_ons": [{"name": "Extra Cheese", "quantity": 1}, {"name": "Extra Patty", "quantity": 1}], "special_instructions": ""}]},
         "expect": {"state": "CONFIRMING_ORDER", "cart": [{"name": "Loaded Smash Burger", "quantity": 1}]}},
        {"message": "Remove extra cheese",
         "expect": {"state": "CONFIRMING_ORDER", "cart": [{"name": "Loaded Smash Burger", "quantity": 1}], "response_contains": ["removed"]}},
        {"message": "Yes", "expect": {"state": "CHOOSING_ORDER_MODE"}},
        {"message": "Pickup", "expect": {"state": "COLLECTING_DETAILS"}},
        {"message": "Andile", "expect": {"state": "COLLECTING_DETAILS"}},
        {"message": "0867890123", "expect": {"state": "ORDER_PLACED", "cart": []}}
    ],
    "final_order": {"items": [{"name": "Loaded Smash Burger", "quantity": 1}], "subtotal_cents": 10000, "total_cents": 10000, "order_mode": "PICKUP", "customer_name": "Andile"}
})

# MR-009: Remove Coke from multi-item cart
tests.append({
    "id": "mr_009",
    "title": "Modifier replace — Remove Coke from cart, burger survives",
    "category": "modifier_replace",
    "tags": ["det", "item_remove", "cart_integrity"],
    "notes": "CSB + Coke. 'Remove the Coke' — DET Sub-case A2. CSB must remain. Exactly 1 item after removal.",
    "turns": [
        {"message": "Can I get a Classic Smash Burger and a Coke",
         "expect": {"state": "CONFIRMING_ORDER", "cart": [{"name": "Classic Smash Burger", "quantity": 1}, {"name": "Coca-Cola (330ml)", "quantity": 1}]}},
        {"message": "Remove the Coke",
         "expect": {"state": "CONFIRMING_ORDER", "cart": [{"name": "Classic Smash Burger", "quantity": 1}], "response_contains": ["removed"]}},
        {"message": "Yes", "expect": {"state": "CHOOSING_ORDER_MODE"}},
        {"message": "Pickup", "expect": {"state": "COLLECTING_DETAILS"}},
        {"message": "Thandeka", "expect": {"state": "COLLECTING_DETAILS"}},
        {"message": "0878901234", "expect": {"state": "ORDER_PLACED", "cart": []}}
    ],
    "final_order": {"items": [{"name": "Classic Smash Burger", "quantity": 1}], "subtotal_cents": 7500, "total_cents": 7500, "order_mode": "PICKUP", "customer_name": "Thandeka"}
})

# MR-010: Multi-step add then remove, price returns to base
tests.append({
    "id": "mr_010",
    "title": "Modifier replace — Add then remove add-on (price returns to base)",
    "category": "modifier_replace",
    "tags": ["det", "addon_add", "addon_remove", "multi_step", "pricing"],
    "notes": "LSB plain (R75). Add Extra Cheese (DET, R85). Remove Extra Cheese (DET, R75). Price must return to R75. 1 LSB throughout.",
    "turns": [
        {"message": "Can I get a Loaded Smash Burger",
         "expect": {"state": "CONFIRMING_ORDER", "cart": [{"name": "Loaded Smash Burger", "quantity": 1}]}},
        {"message": "Add extra cheese",
         "expect": {"state": "CONFIRMING_ORDER", "cart": [{"name": "Loaded Smash Burger", "quantity": 1}], "response_contains": ["extra cheese"]}},
        {"message": "Remove the extra cheese",
         "expect": {"state": "CONFIRMING_ORDER", "cart": [{"name": "Loaded Smash Burger", "quantity": 1}], "response_contains": ["removed"]}},
        {"message": "Yes", "expect": {"state": "CHOOSING_ORDER_MODE"}},
        {"message": "Pickup", "expect": {"state": "COLLECTING_DETAILS"}},
        {"message": "Bongani", "expect": {"state": "COLLECTING_DETAILS"}},
        {"message": "0761234567", "expect": {"state": "ORDER_PLACED", "cart": []}}
    ],
    "final_order": {"items": [{"name": "Loaded Smash Burger", "quantity": 1}], "subtotal_cents": 7500, "total_cents": 7500, "order_mode": "PICKUP", "customer_name": "Bongani"}
})

for data in tests:
    fname = f'{data["id"]}.json'
    with open(fname, "w") as f:
        json.dump(data, f, indent=2)

print(f"Created {len(tests)} modifier replacement tests")
