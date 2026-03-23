# Naru Booking Bot

This project automates the Naru reservation flow on AirMenus using Playwright and a dedicated Chromium browser profile.

It:
- waits until the booking window opens unless you run with `--dry-run`
- searches for the best available slot from Sunday night backward through Monday
- prefers these times in order: `08:30 PM`, `06:30 PM`, `04:30 PM`, `02:30 PM`, `12:30 PM`
- skips slots that are marked sold out
- selects either table seating or bar seating based on configuration
- fills guest details from an `.env` file or CLI flags
- adjusts the guest counter to a target guest count
- logs each date, time slot, seating card, guest-counter click, and payment-page transition

## Files

- `naru_booking_bot.py`: main automation script
- `.env`: your local booking details
- `.env.example`: template for creating your own `.env`

## Setup

1. Install Python dependencies:

```bash
pip install playwright
```

2. Install the Playwright browser:

```bash
playwright install chromium
```

3. Create your local env file:

```bash
cp .env.example .env
```

4. Edit `.env` and set your values:

```env
NARU_NAME=Your Name
NARU_PHONE=+91-9999999999
NARU_GUEST_NAMES=Guest One, Guest Two, Guest Three
NARU_GUEST_COUNT=4
NARU_SEATING_TYPE=table
```

These values are required unless you pass the equivalent CLI flags at runtime.
`NARU_SEATING_TYPE` is optional and defaults to `table`.
Set `NARU_GUEST_COUNT` to the total number of guests you want to book for. The bot will detect the current counter value and click `+` or `-` until it reaches that number.

## How It Works

When you run the script, it launches its own Playwright-controlled Chromium window. You do not need to manually open the booking page first.

The script:
1. waits until Monday `8:00 PM` IST unless `--dry-run` is used
2. opens the AirMenus booking page
3. checks Sunday first, then Saturday through Monday
4. on each day, tries `08:30 PM`, then `06:30 PM`, `04:30 PM`, `02:30 PM`, `12:30 PM`
5. selects the correct seating card and clicks the `BOOK` button inside that card
6. fills guest details and adjusts the guest counter
7. logs when a payment page is reached and pauses if manual action is needed

## Running

Immediate dry run:

```bash
python3 naru_booking_bot.py --dry-run
```

Wait for the real booking window:

```bash
python3 naru_booking_bot.py
```

Run headless:

```bash
python3 naru_booking_bot.py --headless
```

Override env values on the command line:

```bash
python3 naru_booking_bot.py --dry-run --name "Your Name" --phone "+91-9999999999" --guest-names "Guest One, Guest Two, Guest Three" --guest-count 4
```

Run in table mode:

```bash
python3 naru_booking_bot.py --seating table
```

Run in bar mode:

```bash
python3 naru_booking_bot.py --seating bar
```

In `bar` mode, the bot trims the guest list down to the first two names and caps the target guest count at `2`.

Example `.env` for table seating:

```env
NARU_NAME=Your Name
NARU_PHONE=+91-9999999999
NARU_GUEST_NAMES=Guest One, Guest Two, Guest Three
NARU_GUEST_COUNT=4
NARU_SEATING_TYPE=table
```

Example `.env` for bar seating:

```env
NARU_NAME=Your Name
NARU_PHONE=+91-9999999999
NARU_GUEST_NAMES=Guest One, Guest Two
NARU_GUEST_COUNT=2
NARU_SEATING_TYPE=bar
```

## Logs You Will See

Examples:

```text
Polling iteration 1 started
Attempting date 2026-03-29
Date 2026-03-29 selected; checking time slots
Checking slot 2026-03-29 08:30 PM
Slot 2026-03-29 08:30 PM unavailable: sold out
Checking slot 2026-03-29 06:30 PM
Slot 2026-03-29 06:30 PM selected successfully
Found seating card candidate: TABLE - 1 (Seats 6) ...
Attempting seating card TABLE - 1
Seating card TABLE - 1 selected successfully
Detected current guest counter value: 1
Adjusting guest counter toward 4: increment 1/3
Payment page reached in the Playwright browser.
```

## Notes

- The browser session is stored in `.playwright-airmenus-profile/`.
- AirMenus can change its DOM, so selectors may need adjustment later.
- Full payment automation is not implemented; the script detects the payment page and leaves the final payment step for manual completion.
