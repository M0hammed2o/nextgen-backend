# Product variants (clothing / retail) - backend contract

Migration `0066_product_variants`. Reuses the shared POS, the single pricing engine, the Order model and the
inventory ledger; nothing restaurant-specific changed (an item with no variants behaves exactly as before).

## Model
- `product_variants`: `business_id`, `menu_item_id`, `sku` (unique per business), `barcode` (unique per business when
  not null), `attributes` (JSONB string map, e.g. `{"size":"M","colour":"Black"}`), `price_cents` (null = item base
  price), `is_active`, `sort_order`, `ingredient_id`.
- Stock: each tracked variant owns one `ingredients` row (unit `UNIT`, 1 each = 1000 milli, category "Retail stock").
  Stocktake, wastage, goods receipt, par levels and the ledger therefore work per variant unchanged.
- `order_items`: `variant_id` (SET NULL), `variant_label_snapshot`, `sku_snapshot`, `returned_quantity`.
- `orders.exchange_of_order_id`.
- Variant stock is only deducted when `businesses.inventory_enabled` (same rule as recipe stock; event vendors have it on).

## Endpoints (all under `/v1`, business_id always from the JWT)
| Method | Path | Role |
|---|---|---|
| GET | `/business/menu/items/{item_id}/variants?include_inactive=false` | staff+ |
| POST | `/business/menu/items/{item_id}/variants` | owner/manager |
| POST | `/business/menu/items/{item_id}/variants/matrix` | owner/manager |
| PATCH | `/business/menu/variants/{variant_id}` | owner/manager |
| DELETE | `/business/menu/variants/{variant_id}` (soft deactivate, returns the variant) | owner/manager |
| GET | `/business/pos/lookup?barcode=..` or `?sku=..` (exactly one; active only) | staff+, POS enabled |
| GET | `/business/analytics/variants?range=7d&menu_item_id=` | owner/manager |

`VariantCreate`: `{sku, barcode?, attributes{str:str}, price_cents?, cost_cents?, sort_order?, track_stock=true, opening_stock?}`.
`MatrixCreate`: `{sku_prefix, sizes[], colours[], price_cents?, cost_cents?, track_stock=true, opening_stock?}` ->
SKU `<PREFIX>-<SIZE>-<COLOUR>` upper-cased, spaces removed; all-or-nothing; max 200 combinations.
`VariantUpdate`: any of `sku, barcode (null clears), attributes, price_cents (null = base price), cost_cents, sort_order, is_active`.
`VariantResponse`: `{id, menu_item_id, sku, barcode, attributes, label, price_cents, effective_price_cents, cost_cents,
is_active, sort_order, ingredient_id, stock_on_hand (units, null if untracked)}`.
`LookupResponse`: `{menu_item_id, menu_item_name, base_price_cents, variant}`.
Errors: 409 `SKU_TAKEN` / `BARCODE_TAKEN` / `VARIANT_CONFLICT`; 422 `SKU_PATTERN_COLLISION`, `LOOKUP_KEY_REQUIRED`; 404 for
anything not in the caller's business.

## Selling
`POST /business/pos/orders` item gains `variant_id` (optional). Rules: item with active variants and no `variant_id` ->
422 `VARIANT_REQUIRED`; inactive variant -> 422 `VARIANT_INACTIVE`; unknown / other business's / other item's variant ->
404. Unit price = variant `price_cents` if set else item price, then the unchanged option/add-on/discount logic.
Order items return `variant_id`, `variant_label_snapshot`, `sku_snapshot`, `returned_quantity`.

## Returns and exchanges
- `POST /business/pos/orders/{id}/refund` gains `return_items: [{order_item_id, quantity}]`. Amount defaults to the
  returned units' value pro-rated for order discount / loyalty / exclusive VAT, and may only be lower
  (`REFUND_EXCEEDS_RETURNED_VALUE`); `RETURN_EXCEEDS_SOLD` guards quantity. With `restore_stock=true`, returned variant
  units go back to stock (one `SALE_REVERSAL` per line); a full return restores everything via the existing reversal.
- A partial amount-only refund with `restore_stock=true` on an order containing variant lines is refused
  (`RETURN_ITEMS_REQUIRED`) - it used to reverse the whole order's stock.
- Exchange = refund with `return_items` (+ `restore_stock`) then a new `POST /business/pos/orders` with
  `exchange_of_order_id` (must be a refunded POS order of the same business, else 422 `EXCHANGE_ORIGINAL_NOT_REFUNDED` /
  404). Two calls, not one transaction. An order can be refunded once (existing rule: only `PAID` orders).

## Bounds and restock source (souk review L-1, L-3)
- `price_cents`, `cost_cents`, `sort_order` (create, update, matrix) are capped at 2147483647 (the INTEGER ceiling);
  `opening_stock x cost_cents` must also stay below it (it is the opening movement's rand value). Out of range is a
  422. A POS order whose priced lines sum past the ceiling is a 422 `ORDER_TOTAL_TOO_LARGE`.
- Each variant order line snapshots the ingredient it consumed (`order_items.stock_ingredient_id_snapshot`, migration
  0068). A partial-return restock posts to that ingredient, not to the variant's current link, so re-linking a variant
  after a sale cannot redirect returned stock. Lines created before 0068 fall back to the current link.

