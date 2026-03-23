#!/usr/bin/env python3
"""
Reservation bot for Naru, Bangalore via AirMenus.

Flow:
- Wait until the next Monday at 8:00 PM in Asia/Kolkata unless --dry-run is set.
- Open Naru's AirMenus order page.
- Select the upcoming Sunday relative to the run time.
- Select an 8:30 PM slot.
- Select any visible table option that is not bar seating.
- Optionally fill common guest-detail fields and submit.

Setup:
1. pip install playwright
2. playwright install chromium
3. Run:
   python3 naru_booking_bot.py --dry-run
   python3 naru_booking_bot.py --name "Your Name" --phone "9999999999" --guest-names "Guest One, Guest Two" --guest-increments 1

Notes:
- AirMenus can change its DOM. Selectors are intentionally broad and the text
  matching is centralized so you can tune it after one dry run.
- The script pauses when it reaches a point where the live UI needs inspection.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from playwright.sync_api import (
    BrowserContext,
    Error,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)


IST = ZoneInfo("Asia/Kolkata")
PROFILE_DIR = Path(".playwright-airmenus-profile")
ENV_FILE = Path(".env")
PREFERRED_SLOT_TIMES = ((20, 30), (18, 30), (16, 30), (14, 30), (12, 30))


@dataclass(frozen=True)
class BookingConfig:
    order_url: str = "https://bookings.airmenus.in/eatnaru/order"
    order_unavailable_text_markers: tuple[str, ...] = (
        "There are no available timings",
        "Please check other dates",
        "No slots available",
        "No timings available",
        "No tables available",
    )
    date_option_selectors: tuple[str, ...] = (
        'button',
        '[role="button"]',
        'a',
        'div[class*="date"]',
        'div[class*="day"]',
    )
    time_option_selectors: tuple[str, ...] = (
        'button',
        '[role="button"]',
        'a',
        'div[class*="time"]',
        'div[class*="slot"]',
    )
    table_option_selectors: tuple[str, ...] = (
        'button',
        '[role="button"]',
        'a',
        'div[class*="table"]',
        'div[class*="seat"]',
        'div[class*="option"]',
    )
    continue_selectors: tuple[str, ...] = (
        'button:has-text("Continue")',
        'button:has-text("Next")',
        'button:has-text("Book")',
        'button:has-text("Confirm")',
    )
    name_selectors: tuple[str, ...] = (
        'input[name="name"]',
        'input[placeholder*="Name"]',
        'input[type="text"]',
    )
    phone_selectors: tuple[str, ...] = (
        'input[name="phone"]',
        'input[name="mobile"]',
        'input[type="tel"]',
    )
    guest_names_selectors: tuple[str, ...] = (
        'input[name="guest_names"]',
        'input[name="guestNames"]',
        'input[placeholder*="Guest"]',
        'textarea[name="guest_names"]',
        'textarea[name="notes"]',
        'textarea[placeholder*="Guest"]',
        'textarea[placeholder*="Special"]',
        'textarea',
    )
    guest_increment_selectors: tuple[str, ...] = (
        'button[aria-label*="increase"]',
        'button[aria-label*="increment"]',
        'button[aria-label*="add"]',
        'button:has-text("+")',
        '[role="button"][aria-label*="increase"]',
        '[role="button"]:has-text("+")',
    )
    guest_decrement_selectors: tuple[str, ...] = (
        'button[aria-label*="decrease"]',
        'button[aria-label*="decrement"]',
        'button[aria-label*="remove"]',
        'button:has-text("-")',
        '[role="button"][aria-label*="decrease"]',
        '[role="button"]:has-text("-")',
    )
    guest_count_display_selectors: tuple[str, ...] = (
        'input[name="guest_count"]',
        'input[name="guests"]',
        'input[type="number"]',
        '[aria-live="polite"]',
        '[data-testid*="guest"]',
        'span',
        'div',
    )
    submit_selectors: tuple[str, ...] = (
        'button[type="submit"]',
        'button:has-text("Confirm")',
        'button:has-text("Reserve")',
        'button:has-text("Book")',
    )


@dataclass(frozen=True)
class ReservationRequest:
    name: str
    phone: str
    guest_names: str
    guest_count: int
    seating_type: str
    auto_submit: bool


@dataclass(frozen=True)
class SlotChoice:
    day: date
    label: str
    time_value: datetime


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Book a Naru reservation from the AirMenus order page."
    )
    parser.add_argument("--name", help="Guest name.", required=False)
    parser.add_argument("--phone", help="Guest phone number.", required=False)
    parser.add_argument("--guest-count", help="Target guest count for the booking counter.", required=False)
    parser.add_argument(
        "--guest-names",
        help='Additional guest names, for example "Guest One, Guest Two, Guest Three".',
        required=False,
    )
    parser.add_argument(
        "--auto-submit",
        action="store_true",
        help="Click the final submit/confirm button if it is detected.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not wait until Monday 8 PM. Open the order page immediately.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Chromium headless. Leave this off while stabilizing selectors.",
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=1,
        help="Refresh interval after the booking window opens.",
    )
    parser.add_argument(
        "--seating",
        choices=("table", "bar"),
        required=False,
        help="Choose which seating type to book.",
    )
    return parser.parse_args()


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def next_monday_8pm(now: datetime) -> datetime:
    days_until_monday = (7 - now.weekday()) % 7
    target = (now + timedelta(days=days_until_monday)).replace(
        hour=20, minute=0, second=0, microsecond=0
    )
    if target <= now:
        target += timedelta(days=7)
    return target


def upcoming_sunday(reference: datetime) -> date:
    days_until_sunday = (6 - reference.weekday()) % 7
    if days_until_sunday == 0:
        days_until_sunday = 7
    return (reference + timedelta(days=days_until_sunday)).date()


def upcoming_sunday_830pm(reference: datetime) -> datetime:
    return datetime.combine(
        upcoming_sunday(reference),
        datetime.min.time().replace(hour=20, minute=30),
        tzinfo=IST,
    )


def candidate_booking_dates(reference: datetime) -> list[date]:
    sunday = upcoming_sunday(reference)
    return [sunday - timedelta(days=offset) for offset in range(7)]


def wait_until_opening() -> None:
    while True:
        now = datetime.now(IST)
        target = next_monday_8pm(now)
        seconds_left = (target - now).total_seconds()
        if seconds_left <= 0:
            return

        if seconds_left > 3600:
            sleep_for = 1800
        elif seconds_left > 300:
            sleep_for = 60
        elif seconds_left > 30:
            sleep_for = 5
        else:
            sleep_for = 1

        logging.info(
            "Current time: %s | waiting for opening at %s",
            now.strftime("%Y-%m-%d %H:%M:%S %Z"),
            target.strftime("%Y-%m-%d %H:%M:%S %Z"),
        )
        time.sleep(min(sleep_for, seconds_left))


def launch_context(headless: bool) -> BrowserContext:
    PROFILE_DIR.mkdir(exist_ok=True)
    playwright = sync_playwright().start()
    try:
        return playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR.resolve()),
            headless=headless,
            no_viewport=True,
            args=["--start-maximized"],
        )
    except Exception:
        playwright.stop()
        raise


def page_text(page: Page) -> str:
    try:
        return page.locator("body").inner_text(timeout=3000)
    except (Error, PlaywrightTimeoutError):
        return ""


def dismiss_banners(page: Page) -> None:
    selectors = (
        'button:has-text("Allow")',
        'button:has-text("Accept")',
        'button:has-text("Close")',
        'button:has-text("Dismiss")',
        'button:has-text("OK")',
    )
    for selector in selectors:
        try:
            page.locator(selector).first.click(timeout=800)
        except (PlaywrightTimeoutError, Error):
            continue


def open_order_page(page: Page, config: BookingConfig) -> None:
    page.goto(config.order_url, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    dismiss_banners(page)


def candidate_date_strings(target: date) -> list[str]:
    weekday_short = target.strftime("%a")
    weekday_long = target.strftime("%A")
    month_short = target.strftime("%b")
    month_long = target.strftime("%B")
    day = target.day
    zero_day = target.strftime("%d")
    return [
        f"{weekday_short} {day}",
        f"{weekday_short}, {day}",
        f"{weekday_long} {day}",
        f"{weekday_long}, {day}",
        f"{day} {month_short}",
        f"{day} {month_long}",
        f"{month_short} {day}",
        f"{month_long} {day}",
        f"{weekday_short} {day} {month_short}",
        f"{weekday_long} {day} {month_long}",
        f"{weekday_short} {zero_day}",
        f"{weekday_long} {zero_day}",
        target.strftime("%Y-%m-%d"),
        target.strftime("%d/%m/%Y"),
        target.strftime("%d-%m-%Y"),
    ]


def parse_time_label(value: str, day: date) -> Optional[datetime]:
    normalized = normalize_text(value)
    normalized = normalized.replace(".", ":")
    normalized = re.sub(r"\b(\d{1,2})\s+(\d{2})\b", r"\1:\2", normalized)
    normalized = normalized.upper()
    match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(AM|PM)\b", normalized)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2) or "0")
        meridiem = match.group(3)
        if hour == 12:
            hour = 0
        if meridiem == "PM":
            hour += 12
        return datetime.combine(day, datetime.min.time().replace(hour=hour, minute=minute), tzinfo=IST)

    match = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", normalized)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
        return datetime.combine(day, datetime.min.time().replace(hour=hour, minute=minute), tzinfo=IST)
    return None


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def click_locator(locator: Locator, description: str) -> bool:
    try:
        locator.scroll_into_view_if_needed(timeout=1000)
        locator.evaluate(
            """node => {
                node.scrollIntoView({block: "center", inline: "center", behavior: "instant"});
                let parent = node.parentElement;
                while (parent) {
                    const style = window.getComputedStyle(parent);
                    const canScrollY = /(auto|scroll)/.test(style.overflowY) && parent.scrollHeight > parent.clientHeight;
                    const canScrollX = /(auto|scroll)/.test(style.overflowX) && parent.scrollWidth > parent.clientWidth;
                    if (canScrollY || canScrollX) {
                        parent.scrollTop = Math.max(0, node.offsetTop - parent.clientHeight / 2);
                        parent.scrollLeft = Math.max(0, node.offsetLeft - parent.clientWidth / 2);
                    }
                    parent = parent.parentElement;
                }
                window.scrollBy(0, -120);
            }"""
        )
        locator.click(timeout=1500)
        logging.info("Clicked %s", description)
        return True
    except (PlaywrightTimeoutError, Error):
        try:
            locator.evaluate(
                """node => node.scrollIntoView({block: "center", inline: "center", behavior: "instant"})"""
            )
            locator.click(timeout=1500, force=True)
            logging.info("Clicked %s after forced scroll retry", description)
            return True
        except (PlaywrightTimeoutError, Error):
            return False


def scroll_page_down(page: Page, step: int = 900) -> None:
    try:
        page.evaluate(
            """distance => {
                const roots = [document.scrollingElement, document.documentElement, document.body].filter(Boolean);
                for (const root of roots) {
                    root.scrollTop = Math.min(root.scrollTop + distance, root.scrollHeight);
                }
                const all = Array.from(document.querySelectorAll("*"));
                for (const el of all) {
                    const style = window.getComputedStyle(el);
                    const canScrollY = /(auto|scroll)/.test(style.overflowY) && el.scrollHeight > el.clientHeight;
                    if (canScrollY) {
                        el.scrollTop = Math.min(el.scrollTop + distance, el.scrollHeight);
                    }
                }
            }""",
            step,
        )
    except Error:
        return


def click_first_text_match(
    page: Page,
    selectors: Iterable[str],
    candidates: Iterable[str],
    description: str,
) -> bool:
    normalized_candidates = [normalize_text(candidate) for candidate in candidates]
    for selector in selectors:
        locator = page.locator(selector)
        try:
            count = min(locator.count(), 80)
        except Error:
            continue
        for index in range(count):
            node = locator.nth(index)
            try:
                text = normalize_text(node.inner_text(timeout=500))
            except (PlaywrightTimeoutError, Error):
                continue
            if not text:
                continue
            if any(candidate in text for candidate in normalized_candidates):
                if click_locator(node, f"{description} '{text}' via {selector}"):
                    return True
    return False


def click_first_filtered_match(
    page: Page,
    selectors: Iterable[str],
    include_terms: Iterable[str],
    exclude_terms: Iterable[str],
    description: str,
) -> bool:
    normalized_includes = [normalize_text(term) for term in include_terms]
    normalized_excludes = [normalize_text(term) for term in exclude_terms]
    for selector in selectors:
        locator = page.locator(selector)
        try:
            count = min(locator.count(), 100)
        except Error:
            continue
        for index in range(count):
            node = locator.nth(index)
            try:
                text = normalize_text(node.inner_text(timeout=500))
            except (PlaywrightTimeoutError, Error):
                continue
            if not text:
                continue
            if normalized_includes and not any(term in text for term in normalized_includes):
                continue
            if any(term in text for term in normalized_excludes):
                continue
            if click_locator(node, f"{description} '{text}' via {selector}"):
                return True
    return False


def select_seating_option(page: Page, config: BookingConfig, seating_type: str) -> bool:
    if seating_type == "bar":
        preferred_terms = ("ramen bar seating", "ramen bar")
        excluded_terms = ("table - 1", "table - 2", "table - 3")
        card_priority = ("ramen bar seating", "ramen bar")
    else:
        preferred_terms = ("table - 1", "table - 2", "table - 3")
        excluded_terms = ("ramen bar",)
        card_priority = ("table - 1", "table - 2", "table - 3")

    logging.info("Selecting %s seating option from seating cards", seating_type)
    card_selectors = ("div", "article", "section", "li")
    seen_cards: set[str] = set()
    matching_cards: dict[str, Locator] = {}

    for pass_index in range(6):
        logging.info("Scanning seating cards on scroll pass %s", pass_index + 1)
        for selector in card_selectors:
            locator = page.locator(selector)
            try:
                count = min(locator.count(), 200)
            except Error:
                continue
            for index in range(count):
                card = locator.nth(index)
                try:
                    raw_text = card.inner_text(timeout=400)
                except (PlaywrightTimeoutError, Error):
                    continue

                normalized_text = normalize_text(raw_text)
                if not normalized_text or normalized_text in seen_cards:
                    continue
                seen_cards.add(normalized_text)

                if "book" not in normalized_text:
                    continue
                if "sold out" in normalized_text:
                    logging.info("Skipping sold out seating card: %s", raw_text.strip())
                    continue

                logging.info("Found seating card candidate: %s", raw_text.strip())

                if any(term in normalized_text for term in excluded_terms):
                    logging.info("Skipping seating card '%s': excluded by seating mode %s", raw_text.strip(), seating_type)
                    continue

                matched_priority = next((term for term in card_priority if term in normalized_text), None)
                if not matched_priority:
                    if any(term in normalized_text for term in preferred_terms):
                        matched_priority = next(term for term in preferred_terms if term in normalized_text)
                    else:
                        logging.info("Skipping seating card '%s': label does not match %s seating", raw_text.strip(), seating_type)
                        continue

                matching_cards.setdefault(matched_priority, card)
        if all(label in matching_cards for label in card_priority):
            break
        scroll_page_down(page)
        page.wait_for_timeout(250)

    for label in card_priority:
        card = matching_cards.get(label)
        if not card:
            logging.info("No selectable seating card found for %s", label.upper())
            continue

        logging.info("Attempting seating card %s", label.upper())
        book_button = card.locator('button:has-text("BOOK"), [role="button"]:has-text("BOOK")').first
        if click_locator(book_button, f"{seating_type} seating book button for '{label.upper()}'"):
            logging.info("Seating card %s selected successfully", label.upper())
            return True
        logging.info("Seating card %s matched but its BOOK button click failed", label.upper())

    logging.info("No %s seating card was selected", seating_type)
    return False


def click_best_time_option(
    page: Page,
    config: BookingConfig,
    day: date,
    latest_allowed_time: datetime,
) -> Optional[SlotChoice]:
    choices_by_time: dict[tuple[int, int], tuple[SlotChoice, Locator]] = {}
    seen_labels: set[str] = set()
    inspected_slots: dict[tuple[int, int], str] = {}

    for selector in config.time_option_selectors:
        locator = page.locator(selector)
        try:
            count = min(locator.count(), 100)
        except Error:
            continue
        for index in range(count):
            node = locator.nth(index)
            try:
                text = node.inner_text(timeout=500)
            except (PlaywrightTimeoutError, Error):
                continue
            normalized_text = normalize_text(text)
            if not normalized_text or normalized_text in seen_labels:
                continue
            seen_labels.add(normalized_text)
            parsed_time = parse_time_label(text, day)
            if not parsed_time:
                continue
            slot_key = (parsed_time.hour, parsed_time.minute)
            if slot_key not in {(hour, minute) for hour, minute in PREFERRED_SLOT_TIMES}:
                continue
            if parsed_time > latest_allowed_time:
                continue
            if "sold out" in normalized_text or "unavailable" in normalized_text:
                inspected_slots[slot_key] = "sold out"
                continue
            inspected_slots[slot_key] = "available"
            choices_by_time[(parsed_time.hour, parsed_time.minute)] = (
                SlotChoice(day=day, label=text.strip(), time_value=parsed_time),
                node,
            )

    for hour, minute in PREFERRED_SLOT_TIMES:
        slot_label = datetime.combine(
            day,
            datetime.min.time().replace(hour=hour, minute=minute),
            tzinfo=IST,
        ).strftime("%I:%M %p")
        logging.info("Checking slot %s %s", day.isoformat(), slot_label)
        choice_entry = choices_by_time.get((hour, minute))
        if not choice_entry:
            status = inspected_slots.get((hour, minute), "not visible")
            logging.info("Slot %s %s unavailable: %s", day.isoformat(), slot_label, status)
            continue
        best_choice, best_locator = choice_entry
        if click_locator(best_locator, f"best available time '{best_choice.label}'"):
            logging.info("Slot %s %s selected successfully", day.isoformat(), slot_label)
            return best_choice
        logging.info("Slot %s %s was available but click failed", day.isoformat(), slot_label)
    return None


def order_page_unavailable(page: Page, config: BookingConfig) -> bool:
    body = page_text(page).lower()
    return any(marker.lower() in body for marker in config.order_unavailable_text_markers)


def poll_until_target_bookable(
    page: Page,
    config: BookingConfig,
    candidate_dates: list[date],
    poll_seconds: int,
) -> SlotChoice:
    poll_iteration = 0
    logging.info(
        "Polling order page for the best available slot from %s back to %s.",
        candidate_dates[0].isoformat(),
        candidate_dates[-1].isoformat(),
    )
    while True:
        poll_iteration += 1
        logging.info("Polling iteration %s started", poll_iteration)
        open_order_page(page, config)
        unavailable = order_page_unavailable(page, config)
        for candidate_day in candidate_dates:
            latest_allowed_time = datetime.combine(
                candidate_day,
                datetime.min.time().replace(hour=20, minute=30),
                tzinfo=IST,
            )
            logging.info("Attempting date %s", candidate_day.isoformat())
            date_visible = click_first_text_match(
                page,
                config.date_option_selectors,
                candidate_date_strings(candidate_day),
                f"target date {candidate_day.isoformat()}",
            )
            if not date_visible:
                logging.info("Date %s not visible or not selectable", candidate_day.isoformat())
                continue
            logging.info("Date %s selected; checking time slots", candidate_day.isoformat())
            page.wait_for_timeout(700)
            choice = click_best_time_option(page, config, candidate_day, latest_allowed_time)
            if choice:
                return choice
            logging.info("No eligible time slot found for date %s; moving to next date", candidate_day.isoformat())
            open_order_page(page, config)
        if unavailable:
            logging.info("No matching slot is bookable yet. Retrying in %s second(s).", poll_seconds)
        else:
            logging.info("Date options loaded, but no selectable time slot matched yet. Retrying in %s second(s).", poll_seconds)
        time.sleep(max(1, poll_seconds))


def maybe_click_continue(page: Page, config: BookingConfig) -> None:
    for selector in config.continue_selectors:
        if click_locator(page.locator(selector).first, f"continue button {selector}"):
            return


def detect_payment_page(page: Page) -> bool:
    payment_markers = (
        "pay",
        "payment",
        "card number",
        "credit card",
        "debit card",
        "upi",
        "netbanking",
        "cvv",
        "expiry",
        "razorpay",
        "cashfree",
        "payu",
        "stripe",
    )
    current_url = page.url.lower()
    if any(marker in current_url for marker in payment_markers):
        return True

    body = page_text(page).lower()
    if any(marker in body for marker in payment_markers):
        return True

    for frame in page.frames:
        try:
            frame_url = frame.url.lower()
        except Error:
            continue
        if any(marker in frame_url for marker in payment_markers):
            return True
    return False


def fill_first_visible(page: Page, selectors: tuple[str, ...], value: str, label: str) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=1200)
            locator.fill(value, timeout=1200)
            logging.info("Filled %s using %s", label, selector)
            return True
        except (PlaywrightTimeoutError, Error):
            continue
    return False


def click_first_visible(page: Page, selectors: tuple[str, ...], label: str) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=1200)
            locator.click(timeout=1200)
            logging.info("Clicked %s using %s", label, selector)
            return True
        except (PlaywrightTimeoutError, Error):
            continue
    return False


def first_two_guest_names(raw_value: str) -> str:
    names = [part.strip() for part in raw_value.split(",") if part.strip()]
    if not names:
        return raw_value.strip()
    return ", ".join(names[:2])


def current_guest_count(page: Page, config: BookingConfig) -> Optional[int]:
    for selector in config.guest_count_display_selectors:
        locator = page.locator(selector)
        try:
            count = min(locator.count(), 40)
        except Error:
            continue
        for index in range(count):
            node = locator.nth(index)
            try:
                text = node.input_value(timeout=400)
            except (PlaywrightTimeoutError, Error):
                try:
                    text = node.inner_text(timeout=400)
                except (PlaywrightTimeoutError, Error):
                    continue
            normalized = normalize_text(text)
            if not normalized:
                continue
            match = re.fullmatch(r"\d+", normalized)
            if not match:
                continue
            value = int(normalized)
            if 1 <= value <= 20:
                logging.info("Detected current guest counter value: %s", value)
                return value
    return None


def adjust_guest_counter(page: Page, config: BookingConfig, request: ReservationRequest) -> None:
    current_count = current_guest_count(page, config)
    if current_count is None:
        logging.warning("Could not detect current guest counter value; assuming 1")
        current_count = 1

    if current_count == request.guest_count:
        logging.info("Guest counter already matches target: %s", request.guest_count)
        return

    if current_count < request.guest_count:
        diff = request.guest_count - current_count
        for index in range(diff):
            logging.info(
                "Adjusting guest counter toward %s: increment %s/%s",
                request.guest_count,
                index + 1,
                diff,
            )
            if not click_first_visible(page, config.guest_increment_selectors, "guest increment"):
                logging.warning("Guest increment control was not found")
                break
            page.wait_for_timeout(300)
        return

    diff = current_count - request.guest_count
    for index in range(diff):
        logging.info(
            "Adjusting guest counter toward %s: decrement %s/%s",
            request.guest_count,
            index + 1,
            diff,
        )
        if not click_first_visible(page, config.guest_decrement_selectors, "guest decrement"):
            logging.warning("Guest decrement control was not found")
            break
        page.wait_for_timeout(300)


def fill_guest_details(page: Page, config: BookingConfig, request: Optional[ReservationRequest]) -> None:
    if not request:
        return
    fill_first_visible(page, config.name_selectors, request.name, "name")
    fill_first_visible(page, config.phone_selectors, request.phone, "phone")
    fill_first_visible(page, config.guest_names_selectors, request.guest_names, "guest names")
    adjust_guest_counter(page, config, request)


def maybe_submit(page: Page, config: BookingConfig, auto_submit: bool) -> bool:
    if not auto_submit:
        return False
    for selector in config.submit_selectors:
        if click_locator(page.locator(selector).first, f"submit button {selector}"):
            return True
    return False


def validate_request(args: argparse.Namespace) -> Optional[ReservationRequest]:
    name = args.name or os.environ.get("NARU_NAME")
    phone = args.phone or os.environ.get("NARU_PHONE")
    guest_names = args.guest_names or os.environ.get("NARU_GUEST_NAMES")
    guest_count_raw = args.guest_count or os.environ.get("NARU_GUEST_COUNT")
    seating_type = args.seating or os.environ.get("NARU_SEATING_TYPE") or "table"

    missing_fields = []
    if not name:
        missing_fields.append("NARU_NAME/--name")
    if not phone:
        missing_fields.append("NARU_PHONE/--phone")
    if not guest_names:
        missing_fields.append("NARU_GUEST_NAMES/--guest-names")
    if not guest_count_raw:
        missing_fields.append("NARU_GUEST_COUNT/--guest-count")
    if missing_fields:
        raise ValueError(
            "Missing booking details. Set these in .env or pass CLI flags: "
            + ", ".join(missing_fields)
        )

    try:
        guest_count = int(guest_count_raw)
    except ValueError as exc:
        raise ValueError(
            "Guest count must be an integer. Use NARU_GUEST_COUNT/--guest-count."
        ) from exc

    if guest_count <= 0:
        raise ValueError("Guest count must be greater than zero.")

    if seating_type not in {"table", "bar"}:
        raise ValueError("Invalid seating type. Use --seating table or --seating bar.")

    if seating_type == "bar":
        guest_names = first_two_guest_names(guest_names)
        if guest_count > 2:
            logging.info("Bar mode requested with guest count %s; forcing guest count to 2", guest_count)
            guest_count = 2

    return ReservationRequest(
        name=name,
        phone=phone,
        guest_names=guest_names,
        guest_count=guest_count,
        seating_type=seating_type,
        auto_submit=args.auto_submit,
    )


def main() -> int:
    load_dotenv(ENV_FILE)
    args = parse_args()
    request = validate_request(args)
    config = BookingConfig()
    now = datetime.now(IST)
    target_datetime = upcoming_sunday_830pm(now)
    candidate_dates = candidate_booking_dates(now)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    logging.info("Target booking selection: %s", target_datetime.strftime("%Y-%m-%d %I:%M %p %Z"))
    logging.info("Seating mode: %s", request.seating_type)
    logging.info("Guest names configured: %s", request.guest_names)
    logging.info("Target guest count configured: %s", request.guest_count)

    context = launch_context(headless=args.headless)
    try:
        page = context.new_page()

        if not args.dry_run:
            wait_until_opening()

        selected_slot = poll_until_target_bookable(
            page,
            config,
            candidate_dates=candidate_dates,
            poll_seconds=max(1, args.poll_seconds),
        )
        logging.info(
            "Selected slot: %s",
            selected_slot.time_value.strftime("%Y-%m-%d %I:%M %p %Z"),
        )

        page.wait_for_timeout(1000)
        if not select_seating_option(page, config, request.seating_type):
            logging.warning(
                "A slot opened, but no %s seating option matched the current selectors. "
                "Inspect the page and update BookingConfig.table_option_selectors."
                % request.seating_type
            )
            page.pause()
            return 1

        page.wait_for_timeout(1000)
        maybe_click_continue(page, config)
        page.wait_for_timeout(1000)
        if detect_payment_page(page):
            logging.info("Payment page reached in the Playwright browser.")

        fill_guest_details(page, config, request)

        if request and maybe_submit(page, config, auto_submit=request.auto_submit):
            page.wait_for_timeout(1500)
            if detect_payment_page(page):
                logging.info("Payment page reached after submit in the Playwright browser.")
            logging.info("Submission attempted. Verify the booking confirmation manually.")
            return 0

        if detect_payment_page(page):
            logging.info("Payment page reached and browser is paused for manual completion.")
        logging.info("Reached the booking flow. Browser paused for manual review/final confirmation.")
        page.pause()
        return 0
    finally:
        context.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
