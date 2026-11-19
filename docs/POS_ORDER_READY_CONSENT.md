# POS "order ready" WhatsApp messages: consent change and compatibility

Status: on branch `feat/souk-event-foundation` (commit `8262b24`, migration `0065_order_notify_when_ready`). **Not deployed.** This change affects restaurants as well as souk vendors.

## What changed

| | Production today (`main`) | After this release |
|---|---|---|
| POS order with a phone number that matches an existing WhatsApp customer, e.g. typed for loyalty points | That customer gets a WhatsApp message on each status change the staff make: accepted, being prepared, ready, delivered, collected, cancelled | No message, unless the cashier ticked **Notify customer when ready** |
| POS order where the cashier ticked the consent box and entered a number | Not possible | Exactly **one** message, when the order is marked READY |
| Walk-up customer who never messaged the business | Never messaged | Messaged at READY only with ticked consent (see the 24-hour window limit below) |
| Orders that came in through WhatsApp | Status messages as today | **Unchanged** |
| Uber Eats / other sources | As today | **Unchanged** |

Code:
- `backend/app/api/v1/routes_orders.py`, `update_order_status`, the "Notify customer via WhatsApp" block;
- `routes_pos.py`, `POSOrderCreateRequest.notify_when_ready`, which requires `customer_phone`;
- column `orders.notify_when_ready`: NOT NULL, default false. Existing rows become false.

## Why

A phone number typed for loyalty points is not consent to be messaged. Before this change, any POS sale linked to a loyalty customer sent that customer every status update, and no one at the till had asked them. POPIA requires a lawful basis for direct contact. Explicit consent at the till is the simplest one to demonstrate.

## Compatibility implications for live restaurants

1. **Restaurants on the current staff app stop sending POS status messages.**
   - When the backend is deployed before the staff app update reaches a till, that till sends no `notify_when_ready`. The default is false, so loyalty-linked POS customers stop receiving status messages.
   - Nothing breaks and no error shows. The messages simply stop.
   - Once the new staff app loads, the cashier gets the tick-box.
2. **Only READY is sent now.** Restaurants that relied on the accepted, preparing, collected or cancelled messages for POS orders lose them, including the cancellation notice. This is deliberate: one message per consent.
3. **The 24-hour window (Meta rule; pre-existing gap BUG-0183).**
   - A free-form message only reaches a customer who messaged the business in the last 24 hours. A walk-up POS customer usually has not, so their READY message needs an approved Meta template.
   - The template fallback is not wired automatically today (BUG-0183). A READY message outside the window is logged as missed, not delivered.
   - This was already true of the old behaviour for loyalty customers outside the window.
   - The WhatsApp souk workstream (`feat/souk-whatsapp-gate`) addresses the template path. See that branch's docs for status.
4. **Souk event vendors:** the tick-box is hidden for them until their own WhatsApp number is connected and allowed (BUG-0210).
5. **No data migration risk.** The column is additive with a default. Rolling the code back without rolling the schema back is safe. Downgrading 0065 drops the column, and the consent record with it.

## What to tell restaurant customers (suggested release note)

> At the till, tick **Notify customer when ready** and enter the customer's WhatsApp number if they want a message when their order is ready. A number entered only for loyalty points no longer triggers WhatsApp messages.

## Owner decision recorded

The owner listed this as a behaviour change needing acknowledgement (overnight report 2026-11-19, §8 item 10). The 2026-11-19 approval asked for it to be documented, not reverted. Revisit it if restaurants report missing messages.
