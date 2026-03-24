# Naru Booking Bot

Playwright automation for the Naru AirMenus booking flow.

The bot currently:
- opens the AirMenus order page immediately when you run it
- works from the saved `zero_page`, `first_page`, `second_page`, and payment flow structure
- filters booking dates with `NARU_BOOKING_DAYS`
- filters booking times with `NARU_BOOKING_TIME`
- iterates visible time slots from latest to earliest when time is `all`
- fills checkout details from `.env` or CLI flags
- checks the required house-rules checkbox
- can continue into Razorpay and enter your UPI ID
- handles the booked-out checkout modal by clicking `MODIFY BOOKING` and continuing with remaining combinations
- supports `--dry-run`, which stops at the payment page before UPI automation

## Files

- `naru_booking_bot.py`: main script
- `.env`: your local booking details
- `zero_page.html/png`: calendar and seating-card reference
- `first_page.html/png`: time-slot and guest-counter reference
- `second_page.html/png`: checkout-page reference
- `booked_out_page.html/png`: checkout booked-out modal reference

## Setup

1. Install Playwright:

```bash
pip install playwright
```

2. Install Chromium for Playwright:

```bash
playwright install chromium
```

3. Create `.env` and set your values:

```env
NARU_NAME=Your Name
NARU_EMAIL=you@example.com
NARU_PHONE=+91-9999999999
NARU_BOOKING_UPI=name@bank
NARU_BOOKING_DAYS=all
NARU_BOOKING_TIME=all
NARU_GUEST_COUNT=4
NARU_SEATING_TYPE=bar
```

Required fields:
- `NARU_NAME`
- `NARU_EMAIL`
- `NARU_PHONE`
- `NARU_GUEST_COUNT`

Optional fields:
- `NARU_BOOKING_UPI`
- `NARU_BOOKING_DAYS`
- `NARU_BOOKING_TIME`
- `NARU_SEATING_TYPE`

`NARU_UPI_ID` is still accepted as a fallback for older env files.

## Booking Filters

`NARU_BOOKING_DAYS`
- `all`: tries Sunday back through Tuesday
- specific values: `sunday`, `saturday`, `2026-03-29`, `29`

`NARU_BOOKING_TIME`
- `all`: tries all visible slots from latest to earliest
- specific values: comma-separated list such as `2:30,8:30`

`NARU_SEATING_TYPE`
- `bar`
- `table`

## Flow

The current flow is:

1. Open `https://bookings.airmenus.in/eatnaru/order`
2. Restore the calendar page if AirMenus reopens inside a nested booking state
3. Select the requested date on the calendar
4. Open the requested seating card
5. Iterate time slots
6. Select guest count with the `+` / `-` counter
7. Click `CONTINUE`
8. Fill name, email, and mobile
9. Check the house-rules checkbox
10. Click `PROCEED`
11. If Razorpay opens and UPI is configured, select UPI and fill the UPI ID

If checkout shows the booked-out modal from `booked_out_page.html`, the bot clicks `MODIFY BOOKING` and continues trying the remaining slot/date combinations instead of stopping.

## Running

Run with env values:

```bash
python3 naru_booking_bot.py
```

Run headless:

```bash
python3 naru_booking_bot.py --headless
```

Run a dry run to the payment page:

```bash
python3 naru_booking_bot.py --dry-run
```

Override values on the command line:

```bash
python3 naru_booking_bot.py \
  --name "Your Name" \
  --email "you@example.com" \
  --phone "+91-9999999999" \
  --guest-count 4 \
  --seating bar \
  --booking-days sunday \
  --booking-time 8:30
```

Run with UPI handoff:

```bash
python3 naru_booking_bot.py \
  --name "Your Name" \
  --email "you@example.com" \
  --phone "+91-9999999999" \
  --guest-count 4 \
  --upi-id "name@bank"
```

## Notes

- The browser profile is stored in `.playwright-airmenus-profile/`.
- The mobile field is currently targeted through `input[type="tel"]` first, matching the saved checkout page.
- The bot treats actual page transitions as the source of truth. For example, the seating step is only considered successful if the slot page actually opens.
- Razorpay is dynamic and cross-origin. The UPI step is best-effort; if the live layout differs, the browser pauses on the payment page for manual completion.
- AirMenus is a React app and can leave stale DOM around. The recovery logic is tuned to the current live behavior and the saved HTML snapshots.

## Typical Logs

```text
Attempting date 2026-03-29
Date 2026-03-29 selected; waiting for date state to settle before checking seating cards
Selecting bar seating option from seating cards
Attempting seating card RAMEN BAR SEATING
Inspecting slot 2026-03-29 08:30 PM
Slot 2026-03-29 08:30 PM available; attempting click
Adjusting guest counter toward 4: clicking + 1/4
Filled name using input[name="name"]
Filled email using input[name="email"]
Filled phone using input[type="tel"]
Clicked modify booking
Selected slot 2026-03-29 08:30 PM was booked out during checkout. Trying remaining options.
```
