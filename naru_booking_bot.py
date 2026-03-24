#!/usr/bin/env python3
"""
Reservation bot for Naru, Bangalore via AirMenus.

Flow:
- Open Naru's AirMenus order page.
- Select the upcoming Sunday relative to the run time.
- Select an 8:30 PM slot.
- Select any visible table option that is not bar seating.
- Fill name, email, phone, and the required rules checkbox on checkout.
- Continue into Razorpay, switch to UPI, and fill the configured UPI ID.
- In dry-run mode, stop once the payment page opens.

Setup:
1. pip install playwright
2. playwright install chromium
3. Run:
   python3 naru_booking_bot.py --name "Your Name" --email "you@example.com" --phone "9999999999" --guest-count 2 --upi-id "name@bank"

Notes:
- AirMenus can change its DOM. Selectors are intentionally broad and the text
  matching is centralized so you can tune it after a manual test run.
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
    Frame,
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
        'button.react-calendar__tile',
        '.Reserve_dates_wrpr__zukx9 button',
        'button',
        '[role="button"]',
        'a',
        'div[class*="date"]',
        'div[class*="day"]',
    )
    time_option_selectors: tuple[str, ...] = (
        '.Slots_time_box__hAnID',
        'button',
        '[role="button"]',
        'a',
        'div[class*="time"]',
        'div[class*="slot"]',
        'p[class*="time_box"]',
    )
    table_option_selectors: tuple[str, ...] = (
        '.GroupCards_group_wrpr__w0Q4Z',
        '.GroupCards_book__i5hfH',
        'button',
        '[role="button"]',
        'a',
        'div[class*="table"]',
        'div[class*="seat"]',
        'div[class*="option"]',
    )
    continue_selectors: tuple[str, ...] = (
        'button.Slots_continue_btn__3zieR',
        'button:has-text("CONTINUE")',
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
    email_selectors: tuple[str, ...] = (
        'input[name="email"]',
        'input[placeholder*="Email"]',
        'input[type="email"]',
    )
    phone_selectors: tuple[str, ...] = (
        'input[type="tel"]',
        'input[name="mobile"]',
    )
    rules_checkbox_selectors: tuple[str, ...] = (
        'input[name="readNotes"]',
        '#rulesCheck',
        'label[for="rulesCheck"]',
        '.Checkout_form_check__9rTJQ input[type="checkbox"]',
        '.Checkout_form_check__9rTJQ label',
    )
    guest_increment_selectors: tuple[str, ...] = (
        'button.Slots_action_btns__tE1RQ:has([aria-label="plus"])',
        '.Slots_counter__AaCp8 button:has([aria-label="plus"])',
        'button[aria-label*="increase"]',
        'button[aria-label*="increment"]',
        'button[aria-label*="add"]',
        'button:has-text("+")',
        '[role="button"][aria-label*="increase"]',
        '[role="button"]:has-text("+")',
    )
    guest_decrement_selectors: tuple[str, ...] = (
        'button.Slots_action_btns__tE1RQ:has([aria-label="minus"])',
        '.Slots_counter__AaCp8 button:has([aria-label="minus"])',
        'button[aria-label*="decrease"]',
        'button[aria-label*="decrement"]',
        'button[aria-label*="remove"]',
        'button:has-text("-")',
        '[role="button"][aria-label*="decrease"]',
        '[role="button"]:has-text("-")',
    )
    guest_count_display_selectors: tuple[str, ...] = (
        '.Slots_count__2yReW',
        'input[name="guest_count"]',
        'input[name="guests"]',
        'input[type="number"]',
        '[aria-live="polite"]',
        '[data-testid*="guest"]',
        'span',
        'div',
    )
    submit_selectors: tuple[str, ...] = (
        'button.Checkout_continue_btn__96WZe',
        'button[type="submit"]',
        'button:has-text("Proceed")',
        'button:has-text("Confirm")',
        'button:has-text("Reserve")',
        'button:has-text("Book")',
    )
    payment_frame_url_markers: tuple[str, ...] = (
        "api.razorpay.com",
        "checkout.razorpay.com",
        "razorpay",
    )
    payment_submit_selectors: tuple[str, ...] = (
        'button:has-text("Verify and Pay")',
        'button:has-text("Pay Now")',
        'button:has-text("Pay")',
        'button:has-text("Proceed")',
        'button:has-text("Continue")',
    )


@dataclass(frozen=True)
class ReservationRequest:
    name: str
    email: str
    phone: str
    upi_id: Optional[str]
    guest_count: int
    seating_type: str
    booking_days: str
    booking_times: Optional[tuple[str, ...]]
    auto_submit: bool


@dataclass(frozen=True)
class SlotChoice:
    day: date
    label: str
    time_value: datetime


@dataclass(frozen=True)
class SlotCandidate:
    choice: SlotChoice
    status: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Book a Naru reservation from the AirMenus order page."
    )
    parser.add_argument("--name", help="Guest name.", required=False)
    parser.add_argument("--email", help="Guest email address.", required=False)
    parser.add_argument("--phone", help="Guest phone number.", required=False)
    parser.add_argument("--upi-id", help="UPI ID to use on the payment page.", required=False)
    parser.add_argument("--guest-count", help="Target guest count for the booking counter.", required=False)
    parser.add_argument(
        "--booking-days",
        help="Booking day filter. Use 'all' or a specific day/date such as 'sunday' or '2026-03-29'.",
        required=False,
    )
    parser.add_argument(
        "--booking-time",
        help="Booking time filter. Use 'all' or a comma-separated list such as '2:30,8:30'.",
        required=False,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the full booking flow immediately and stop once the payment page opens.",
    )
    parser.add_argument(
        "--auto-submit",
        action="store_true",
        help="Click the final submit/confirm button if it is detected.",
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
        help="Retry interval while polling for a bookable slot.",
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


def upcoming_sunday(reference: datetime) -> date:
    days_until_sunday = (6 - reference.weekday()) % 7
    if days_until_sunday == 0:
        days_until_sunday = 7
    return (reference + timedelta(days=days_until_sunday)).date()


def candidate_booking_dates(reference: datetime) -> list[date]:
    sunday = upcoming_sunday(reference)
    return [sunday - timedelta(days=offset) for offset in range(6)]


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


def wait_for_any_visible(page: Page, selectors: Iterable[str], timeout_ms: int = 1200) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        for selector in selectors:
            try:
                if page.locator(selector).first.is_visible(timeout=100):
                    return True
            except (PlaywrightTimeoutError, Error):
                continue
        page.wait_for_timeout(50)
    return False


def wait_for_booking_surface(page: Page, timeout_ms: int = 1200) -> bool:
    return wait_for_any_visible(
        page,
        (
            'button.react-calendar__tile',
            '.GroupCards_group_wrpr__w0Q4Z',
            '.Slots_time_box__hAnID',
            'button.Checkout_continue_btn__96WZe',
        ),
        timeout_ms=timeout_ms,
    )


def wait_for_calendar_page(page: Page, timeout_ms: int = 3000) -> bool:
    return wait_for_any_visible(page, ('button.react-calendar__tile',), timeout_ms=timeout_ms)


def on_calendar_page(page: Page) -> bool:
    try:
        return page.locator('button.react-calendar__tile').count() > 0
    except Error:
        return False


def on_checkout_page(page: Page) -> bool:
    try:
        proceed_button = page.locator('button.Checkout_continue_btn__96WZe')
        name_input = page.locator('input[name="name"]')
        return proceed_button.count() > 0 or name_input.count() > 0
    except Error:
        return False


def booking_back_button(page: Page) -> Locator:
    selectors = (
        'button.jsx-b836407cc4619c4.btn.btn-sm.icon:has(svg[data-icon="arrow-left"])',
        'button:has(svg[data-icon="arrow-left"])',
    )
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() > 0:
                return locator
        except Error:
            continue
    return page.locator('button:has(svg[data-icon="arrow-left"])').first


def clear_booking_storage(page: Page) -> None:
    try:
        page.evaluate(
            """() => {
                try { window.localStorage.clear(); } catch (error) {}
                try { window.sessionStorage.clear(); } catch (error) {}
            }"""
        )
        logging.info("Cleared booking page local/session storage")
    except Error:
        logging.info("Could not clear booking page local/session storage")


def ensure_calendar_page(page: Page, config: BookingConfig) -> bool:
    dismiss_banners(page)
    if on_calendar_page(page):
        logging.info("Calendar page confirmed")
        return True

    if detect_booked_out_checkout(page) or on_checkout_page(page):
        if click_first_visible(
            page,
            (
                'button.Checkout_ok_rules__YNORS',
                'button:has-text("MODIFY BOOKING")',
            ),
            "modify booking",
        ):
            wait_for_calendar_page(page, timeout_ms=3000)
            dismiss_banners(page)
            if on_calendar_page(page):
                logging.info("Calendar page restored through modify booking")
                return True

    if on_slot_selection_page(page):
        if click_locator(booking_back_button(page), "booking back button"):
            wait_for_calendar_page(page, timeout_ms=3000)
            dismiss_banners(page)
            if on_calendar_page(page):
                logging.info("Calendar page restored through app back button")
                return True

    try:
        page.go_back(wait_until="domcontentloaded", timeout=2500)
        wait_for_calendar_page(page, timeout_ms=3000)
        dismiss_banners(page)
        if on_calendar_page(page):
            logging.info("Calendar page restored through browser history")
            return True
    except (PlaywrightTimeoutError, Error):
        pass

    return on_calendar_page(page)


def open_order_page(page: Page, config: BookingConfig) -> None:
    page.goto(config.order_url, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    wait_for_calendar_page(page, timeout_ms=3500)
    if on_calendar_page(page):
        return

    if on_slot_selection_page(page) or on_checkout_page(page):
        logging.info("Order page reopened in a nested booking state; clearing page storage and retrying fresh load")
        clear_booking_storage(page)
        page.goto(config.order_url, wait_until="load")
        page.wait_for_timeout(3000)
        wait_for_calendar_page(page, timeout_ms=3500)
        dismiss_banners(page)
        if on_calendar_page(page):
            logging.info("Calendar page restored after clearing booking storage")
            return

    if not ensure_calendar_page(page, config):
        logging.warning("Order page did not return to the calendar view cleanly")


def return_to_order_page(page: Page, config: BookingConfig) -> None:
    if ensure_calendar_page(page, config):
        return
    open_order_page(page, config)


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


def calendar_aria_labels(target: date) -> tuple[str, ...]:
    day = target.day
    month_long = target.strftime("%B")
    month_short = target.strftime("%b")
    year = target.strftime("%Y")
    return (
        f"{day} {month_long} {year}",
        target.strftime("%d %B %Y"),
        f"{day} {month_short} {year}",
        target.strftime("%d %b %Y"),
        f"{day} {month_long}, {year}",
        target.strftime("%d %B, %Y"),
    )


def normalize_choice_token(value: str) -> str:
    return normalize_text(value).replace(".", ":")


def date_aliases(target: date) -> tuple[str, ...]:
    return tuple(
        normalize_choice_token(value)
        for value in (
            target.isoformat(),
            target.strftime("%d/%m/%Y"),
            target.strftime("%d-%m-%Y"),
            target.strftime("%A"),
            target.strftime("%a"),
            target.strftime("%d"),
            str(target.day),
            f"{target.day} {target.strftime('%B')}",
            f"{target.day} {target.strftime('%b')}",
            f"{target.day} {target.strftime('%B')} {target.strftime('%Y')}",
            f"{target.day} {target.strftime('%b')} {target.strftime('%Y')}",
        )
    )


def filter_candidate_dates(candidate_dates: list[date], booking_days: str) -> list[date]:
    normalized = normalize_choice_token(booking_days)
    if not normalized or normalized == "all":
        return candidate_dates

    requested_tokens = [normalize_choice_token(token) for token in booking_days.split(",") if token.strip()]
    filtered = [
        candidate_day
        for candidate_day in candidate_dates
        if any(token in date_aliases(candidate_day) for token in requested_tokens)
    ]
    if filtered:
        return filtered

    allowed = ", ".join(candidate_day.isoformat() for candidate_day in candidate_dates)
    raise ValueError(
        f"NARU_BOOKING_DAYS/--booking-days did not match any candidate date. Allowed dates: {allowed}"
    )


def parse_booking_times(raw_value: str) -> Optional[tuple[str, ...]]:
    normalized = normalize_choice_token(raw_value)
    if not normalized or normalized == "all":
        return None
    times = tuple(normalize_choice_token(token) for token in raw_value.split(",") if token.strip())
    if not times:
        return None
    return times


def slot_time_aliases(slot_time: datetime) -> tuple[str, ...]:
    hour_24 = slot_time.hour
    minute = slot_time.minute
    hour_12 = hour_24 % 12 or 12
    meridiem = slot_time.strftime("%p")
    return tuple(
        normalize_choice_token(value)
        for value in (
            slot_time.strftime("%I:%M %p"),
            f"{hour_12}:{minute:02d} {meridiem}",
            slot_time.strftime("%H:%M"),
            f"{hour_12}:{minute:02d}",
            f"{hour_12}.{minute:02d}",
            f"{hour_24}:{minute:02d}",
        )
    )


def active_calendar_aria_label(page: Page) -> Optional[str]:
    try:
        locator = page.locator('button.react-calendar__tile--active abbr').first
        locator.wait_for(state="visible", timeout=800)
        value = locator.get_attribute("aria-label", timeout=500)
    except (PlaywrightTimeoutError, Error):
        return None
    if not value:
        return None
    return normalize_choice_token(value)


def calendar_label_matches_target(active_label: str, target: date) -> bool:
    active_tokens = {active_label, normalize_choice_token(active_label.replace(",", ""))}
    target_tokens = {normalize_choice_token(value) for value in calendar_aria_labels(target)}
    return any(token in target_tokens for token in active_tokens)


def click_calendar_date(page: Page, target: date) -> bool:
    current_active = active_calendar_aria_label(page)
    if current_active and calendar_label_matches_target(current_active, target):
        logging.info("Calendar date %s is already active", target.isoformat())
        return True

    target_labels = {normalize_choice_token(value) for value in calendar_aria_labels(target)}
    locator = page.locator('button.react-calendar__tile:not([disabled])')
    try:
        count = min(locator.count(), 60)
    except Error:
        count = 0

    for index in range(count):
        node = locator.nth(index)
        try:
            node.wait_for(state="visible", timeout=300)
            metadata = node.evaluate(
                """button => {
                    const abbr = button.querySelector('abbr');
                    return {
                        ariaLabel: abbr ? (abbr.getAttribute('aria-label') || '') : '',
                        text: button.innerText || '',
                        className: button.className || '',
                    };
                }"""
            )
        except (PlaywrightTimeoutError, Error):
            continue

        aria_label = normalize_choice_token(str(metadata.get("ariaLabel", "")))
        if not aria_label or aria_label not in target_labels:
            continue

        logging.info(
            "Found calendar tile candidate for %s with aria-label '%s'",
            target.isoformat(),
            metadata.get("ariaLabel", ""),
        )
        if not click_locator(node, f"calendar date {target.isoformat()} via scanned tile"):
            continue
        try:
            page.wait_for_function(
                """targetLabels => {
                    const active = document.querySelector('button.react-calendar__tile--active abbr');
                    if (!active) return false;
                    const value = (active.getAttribute('aria-label') || '').toLowerCase().replace(/\s+/g, ' ').replace(/\./g, ':').trim();
                    return targetLabels.includes(value);
                }""",
                arg=list(target_labels),
                timeout=1500,
            )
        except (PlaywrightTimeoutError, Error):
            pass
        page.wait_for_timeout(250)
        current_active = active_calendar_aria_label(page)
        if current_active and calendar_label_matches_target(current_active, target):
            logging.info("Calendar date %s activated successfully", target.isoformat())
            return True
        logging.warning(
            "Calendar click for %s did not activate the requested tile; active date is '%s'",
            target.isoformat(),
            current_active or "unknown",
        )

    logging.warning("Requested calendar date %s was not found or did not activate", target.isoformat())
    return False


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


def payment_frames(page: Page, config: BookingConfig) -> list[Frame]:
    frames: list[Frame] = []
    for frame in page.frames:
        try:
            frame_url = frame.url.lower()
        except Error:
            continue
        if any(marker in frame_url for marker in config.payment_frame_url_markers):
            frames.append(frame)
    return frames


def wait_for_payment_frame(page: Page, config: BookingConfig, timeout_ms: int = 20000) -> Optional[Frame]:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        frames = payment_frames(page, config)
        if frames:
            return frames[0]
        page.wait_for_timeout(500)
    return None


def click_first_visible_in_frame(frame: Frame, selectors: tuple[str, ...], label: str) -> bool:
    for selector in selectors:
        try:
            locator = frame.locator(selector).first
            locator.wait_for(state="visible", timeout=1500)
            locator.click(timeout=1500)
            logging.info("Clicked %s using %s", label, selector)
            return True
        except (PlaywrightTimeoutError, Error):
            continue
    return False


def click_first_frame_text_match(
    frame: Frame,
    selectors: Iterable[str],
    include_terms: Iterable[str],
    exclude_terms: Iterable[str],
    description: str,
) -> bool:
    normalized_includes = [normalize_text(term) for term in include_terms]
    normalized_excludes = [normalize_text(term) for term in exclude_terms]
    for selector in selectors:
        locator = frame.locator(selector)
        try:
            count = min(locator.count(), 120)
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


def fill_first_matching_input_in_frame(
    frame: Frame,
    value: str,
    hints: Iterable[str],
    label: str,
) -> bool:
    normalized_hints = [normalize_text(hint) for hint in hints]
    locator = frame.locator('input:not([type="hidden"]), textarea')
    fallback: Optional[Locator] = None

    try:
        count = min(locator.count(), 50)
    except Error:
        return False

    for index in range(count):
        node = locator.nth(index)
        try:
            node.wait_for(state="visible", timeout=300)
        except (PlaywrightTimeoutError, Error):
            continue

        if fallback is None:
            fallback = node

        try:
            metadata = node.evaluate(
                """node => ({
                    name: node.getAttribute("name") || "",
                    id: node.getAttribute("id") || "",
                    type: node.getAttribute("type") || "",
                    placeholder: node.getAttribute("placeholder") || "",
                    ariaLabel: node.getAttribute("aria-label") || "",
                    labels: node.labels ? Array.from(node.labels).map(label => label.innerText).join(" ") : "",
                })"""
            )
        except Error:
            metadata = {}

        haystack = normalize_text(" ".join(str(part) for part in metadata.values()))
        if normalized_hints and not any(hint in haystack for hint in normalized_hints):
            continue

        try:
            node.fill(value, timeout=1500)
            logging.info("Filled %s in payment frame", label)
            return True
        except (PlaywrightTimeoutError, Error):
            continue

    if fallback is None:
        return False

    try:
        fallback.fill(value, timeout=1500)
        logging.info("Filled %s in payment frame using the first visible input fallback", label)
        return True
    except (PlaywrightTimeoutError, Error):
        return False


def mask_upi_id(upi_id: str) -> str:
    if "@" not in upi_id:
        return upi_id[:2] + "***" if len(upi_id) > 2 else "***"
    name, handle = upi_id.split("@", 1)
    masked_name = name[:2] + "***" if len(name) > 2 else "***"
    return f"{masked_name}@{handle}"


def automate_upi_payment(page: Page, config: BookingConfig, upi_id: str) -> bool:
    frame = wait_for_payment_frame(page, config)
    if not frame:
        logging.warning("Payment frame was not found")
        return False

    logging.info("Attempting Razorpay UPI flow with %s", mask_upi_id(upi_id))

    click_first_frame_text_match(
        frame,
        selectors=("button", '[role="button"]', "div", "label"),
        include_terms=("upi",),
        exclude_terms=("upi qr", "scan the qr", "using as ", "offers on "),
        description="UPI payment method",
    )
    page.wait_for_timeout(250)

    if not fill_first_matching_input_in_frame(
        frame,
        upi_id,
        hints=("upi id", "upi", "vpa", "virtual payment"),
        label="UPI ID",
    ):
        click_first_frame_text_match(
            frame,
            selectors=("button", '[role="button"]', "div", "label", "span"),
            include_terms=("upi id", "enter upi id", "vpa"),
            exclude_terms=("qr", "scan"),
            description="UPI ID entry option",
        )
        page.wait_for_timeout(250)

        if not fill_first_matching_input_in_frame(
            frame,
            upi_id,
            hints=("upi id", "upi", "vpa", "virtual payment"),
            label="UPI ID",
        ):
            logging.warning("UPI ID input was not found in the payment frame")
            return False

    if not click_first_visible_in_frame(frame, config.payment_submit_selectors, "payment submit"):
        if not click_first_frame_text_match(
            frame,
            selectors=("button", '[role="button"]', "div"),
            include_terms=("verify and pay", "pay now", "pay", "continue", "proceed"),
            exclude_terms=("amazon pay", "google pay", "phonepe", "paytm", "scan the qr"),
            description="payment submit",
        ):
            logging.warning("Payment submit control was not found after entering the UPI ID")
            return False

    logging.info("UPI payment initiated. Approve the payment on your phone.")
    return True


def wait_for_payment_completion(page: Page, timeout_ms: int = 180000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if not detect_payment_page(page):
            logging.info("Payment page closed or transitioned away.")
            return True
        page.wait_for_timeout(1000)
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


def on_slot_selection_page(page: Page) -> bool:
    try:
        time_slots = page.locator('.Slots_time_box__hAnID')
        guest_counter = page.locator('.Slots_count__2yReW')
        continue_button = page.locator('button.Slots_continue_btn__3zieR')
        return time_slots.count() > 0 and guest_counter.count() > 0 and continue_button.count() > 0
    except Error:
        return False


def click_seating_book_button(page: Page, button: Locator, description: str) -> bool:
    try:
        button.scroll_into_view_if_needed(timeout=1000)
    except (PlaywrightTimeoutError, Error):
        pass

    try:
        button.click(timeout=2000)
        logging.info("Clicked %s", description)
        return True
    except (PlaywrightTimeoutError, Error):
        pass

    try:
        button.click(timeout=2000, force=True)
        logging.info("Clicked %s with force", description)
        return True
    except (PlaywrightTimeoutError, Error):
        pass

    try:
        box = button.bounding_box(timeout=1000)
    except (PlaywrightTimeoutError, Error):
        box = None
    if box:
        try:
            page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            logging.info("Clicked %s via page.mouse", description)
            return True
        except Error:
            pass

    try:
        button.dispatch_event("click", timeout=1000)
        logging.info("Clicked %s via dispatch_event", description)
        return True
    except (PlaywrightTimeoutError, Error):
        return False


def select_seating_option(page: Page, config: BookingConfig, seating_type: str) -> bool:
    if on_slot_selection_page(page):
        logging.info("Already on the slot selection page for %s seating; skipping seating card click", seating_type)
        return True

    if seating_type == "bar":
        preferred_terms = ("ramen bar seating", "ramen bar")
        excluded_terms = ("table - 1", "table - 2", "table - 3")
        card_priority = ("ramen bar seating", "ramen bar")
    else:
        preferred_terms = ("table - 1", "table - 2", "table - 3")
        excluded_terms = ("ramen bar",)
        card_priority = ("table - 1", "table - 2", "table - 3")

    logging.info("Selecting %s seating option from seating cards", seating_type)
    card_selectors = ('.GroupCards_group_wrpr__w0Q4Z',)
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


                if any(term in normalized_text for term in excluded_terms):
                    continue

                matched_priority = next((term for term in card_priority if term in normalized_text), None)
                if not matched_priority:
                    if any(term in normalized_text for term in preferred_terms):
                        matched_priority = next(term for term in preferred_terms if term in normalized_text)
                    else:
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
        click_attempts: tuple[tuple[str, Locator, str], ...] = (
            (
                "book_button",
                card.locator('button.GroupCards_book__i5hfH, button:has-text("BOOK"), [role="button"]:has-text("BOOK")').first,
                f"{seating_type} seating book button for '{label.upper()}'",
            ),
            (
                "generic",
                card.locator('.GroupCards_header__P_0wN').first,
                f"{seating_type} seating card header for '{label.upper()}'",
            ),
            (
                "generic",
                card,
                f"{seating_type} seating card wrapper for '{label.upper()}'",
            ),
        )
        for click_type, target, description in click_attempts:
            clicked = (
                click_seating_book_button(page, target, description)
                if click_type == "book_button"
                else click_locator(target, description)
            )
            if not clicked:
                continue
            page.wait_for_timeout(1500)
            wait_for_any_visible(
                page,
                (
                    '.Slots_time_box__hAnID',
                    '.Slots_count__2yReW',
                    'button.Slots_continue_btn__3zieR',
                ),
                timeout_ms=3000,
            )
            if on_slot_selection_page(page):
                logging.info("Seating card %s selected successfully", label.upper())
                return True
            logging.info(
                "Clicked %s, but the slot selection page did not open",
                description,
            )
        logging.info("Seating card %s matched but no click path opened the slot selection page", label.upper())

    logging.info("No %s seating card was selected", seating_type)
    return False


def click_best_time_option(
    page: Page,
    config: BookingConfig,
    day: date,
    latest_allowed_time: datetime,
    requested_times: Optional[tuple[str, ...]],
) -> list[SlotCandidate]:
    choices_by_time: dict[tuple[int, int], SlotCandidate] = {}
    seen_labels: set[str] = set()

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
            if parsed_time > latest_allowed_time:
                continue
            status = "available"
            if "sold out" in normalized_text or "unavailable" in normalized_text:
                status = "sold out"
            choices_by_time[slot_key] = SlotCandidate(
                choice=SlotChoice(day=day, label=text.strip(), time_value=parsed_time),
                status=status,
            )

    if not choices_by_time:
        logging.info("No visible time slots were detected for date %s", day.isoformat())
        return []

    if requested_times is None:
        ordered_entries = [choices_by_time[key] for key in sorted(choices_by_time.keys(), reverse=True)]
    else:
        ordered_entries = []
        for requested_time in requested_times:
            match = next(
                (
                    entry
                    for entry in choices_by_time.values()
                    if requested_time in slot_time_aliases(entry.choice.time_value)
                ),
                None,
            )
            if match is None:
                logging.info("Requested slot %s not visible for date %s", requested_time, day.isoformat())
                continue
            ordered_entries.append(match)

    if not ordered_entries:
        logging.info("No eligible requested time slots were visible for date %s", day.isoformat())
        return []

    return ordered_entries


def click_time_candidate(page: Page, config: BookingConfig, candidate: SlotCandidate) -> bool:
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
            parsed_time = parse_time_label(text, candidate.choice.day)
            if not parsed_time:
                continue
            if slot_time_aliases(parsed_time) != slot_time_aliases(candidate.choice.time_value):
                continue
            normalized_text = normalize_text(text)
            if "sold out" in normalized_text or "unavailable" in normalized_text:
                logging.info(
                    "Slot %s %s became unavailable before click",
                    candidate.choice.day.isoformat(),
                    candidate.choice.time_value.strftime("%I:%M %p"),
                )
                return False
            if click_locator(node, f"best available time '{candidate.choice.label}'"):
                logging.info(
                    "Slot %s %s selected successfully",
                    candidate.choice.day.isoformat(),
                    candidate.choice.time_value.strftime("%I:%M %p"),
                )
                return True
            logging.info(
                "Slot %s %s was available but click failed",
                candidate.choice.day.isoformat(),
                candidate.choice.time_value.strftime("%I:%M %p"),
            )
            return False
    logging.info(
        "Slot %s %s is no longer visible on the slot page",
        candidate.choice.day.isoformat(),
        candidate.choice.time_value.strftime("%I:%M %p"),
    )
    return False


def detect_booked_out_checkout(page: Page) -> bool:
    body = page_text(page).lower()
    return (
        "seats aren't available for the selected slots" in body
        or "modify your booking to decrease the number of guests" in body
        or ("modify booking" in body and "selected slots" in body)
    )


def click_modify_booking(page: Page) -> bool:
    return click_first_visible(
        page,
        (
            'button.Checkout_ok_rules__YNORS',
            'button:has-text("MODIFY BOOKING")',
        ),
        "modify booking",
    )


def return_to_slot_selection_page(page: Page, config: BookingConfig) -> str:
    for _ in range(3):
        if on_slot_selection_page(page):
            return "slot"
        if detect_booked_out_checkout(page):
            click_modify_booking(page)
            page.wait_for_timeout(1200)
            if on_slot_selection_page(page):
                return "slot"
            if on_calendar_page(page):
                return "calendar"
        if on_checkout_page(page) or detect_booked_out_checkout(page):
            if click_locator(booking_back_button(page), "booking back button to slot page"):
                page.wait_for_timeout(1500)
                if on_slot_selection_page(page):
                    return "slot"
                if on_calendar_page(page):
                    return "calendar"
        try:
            page.go_back(wait_until="domcontentloaded", timeout=3000)
            page.wait_for_timeout(1200)
            if on_slot_selection_page(page):
                return "slot"
            if on_calendar_page(page):
                return "calendar"
        except (PlaywrightTimeoutError, Error):
            continue
    return "failed"


def open_slot_selection_for_date(
    page: Page,
    config: BookingConfig,
    candidate_day: date,
    seating_type: str,
) -> bool:
    logging.info("Attempting date %s", candidate_day.isoformat())
    date_visible = click_calendar_date(page, candidate_day)
    if not date_visible:
        logging.info("Date %s not visible or not selectable", candidate_day.isoformat())
        return False
    logging.info("Date %s selected; waiting for date state to settle before checking seating cards", candidate_day.isoformat())
    page.wait_for_timeout(900)

    if not select_seating_option(page, config, seating_type):
        logging.info(
            "No selectable %s seating card found for date %s; moving to next date",
            seating_type,
            candidate_day.isoformat(),
        )
        return False

    wait_for_any_visible(page, ('.Slots_time_box__hAnID',), timeout_ms=3000)
    return on_slot_selection_page(page)


def attempt_checkout_for_current_slot(
    page: Page,
    config: BookingConfig,
    request: ReservationRequest,
    args: argparse.Namespace,
) -> str:
    page.wait_for_timeout(2000)
    wait_for_any_visible(
        page,
        (
            '.Slots_count__2yReW',
            'button.Slots_continue_btn__3zieR',
        ),
        timeout_ms=2500,
    )
    adjust_guest_counter(page, config, request)
    page.wait_for_timeout(500)
    maybe_click_continue(page, config)
    page.wait_for_timeout(1500)
    fill_guest_details(page, config, request)

    if detect_booked_out_checkout(page):
        logging.info("Checkout shows the booked-out message for the selected slot")
        return "booked_out"

    if request and maybe_submit(page, config, auto_submit=request.auto_submit):
        page.wait_for_timeout(2500)
        if detect_booked_out_checkout(page):
            logging.info("Checkout shows the booked-out message after submit")
            return "booked_out"
        if detect_payment_page(page):
            logging.info("Payment page reached after submit in the Playwright browser.")
            if args.dry_run:
                logging.info("Dry run reached the payment page. Browser paused before payment automation.")
                page.pause()
                return "success"
            if request.upi_id and automate_upi_payment(page, config, request.upi_id):
                if wait_for_payment_completion(page):
                    logging.info("Payment flow completed in the browser after phone approval.")
                    return "success"
                logging.info("Payment is still pending. Browser paused while approval completes on your phone.")
                page.pause()
                return "success"
            logging.info("Payment page reached. Browser paused for manual completion.")
            page.pause()
            return "success"
        logging.info("Submission attempted, but no payment page was reached for this slot.")
        return "continue"

    if detect_payment_page(page):
        logging.info("Payment page reached and browser is paused for manual completion.")
        page.pause()
        return "success"

    if not request.auto_submit:
        logging.info("Reached the booking flow. Browser paused for manual review/final confirmation.")
        page.pause()
        return "success"

    logging.info("Checkout did not advance for this slot; trying remaining options.")
    return "continue"


def order_page_unavailable(page: Page, config: BookingConfig) -> bool:
    body = page_text(page).lower()
    return any(marker.lower() in body for marker in config.order_unavailable_text_markers)


def poll_until_target_bookable(
    page: Page,
    config: BookingConfig,
    candidate_dates: list[date],
    request: ReservationRequest,
    args: argparse.Namespace,
    poll_seconds: int,
) -> Optional[SlotChoice]:
    logging.info(
        "Checking the order page for the best available slot from %s back to %s.",
        candidate_dates[0].isoformat(),
        candidate_dates[-1].isoformat(),
    )
    open_order_page(page, config)
    unavailable = order_page_unavailable(page, config)
    for candidate_day in candidate_dates:
        if not open_slot_selection_for_date(page, config, candidate_day, request.seating_type):
            continue
        latest_allowed_time = datetime.combine(
            candidate_day,
            datetime.min.time().replace(hour=20, minute=30),
            tzinfo=IST,
        )
        candidates = click_best_time_option(
            page,
            config,
            candidate_day,
            latest_allowed_time,
            requested_times=request.booking_times,
        )
        for candidate in candidates:
            slot_label = candidate.choice.time_value.strftime("%I:%M %p")
            logging.info("Inspecting slot %s %s", candidate.choice.day.isoformat(), slot_label)
            if candidate.status != "available":
                logging.info("Slot %s %s unavailable: %s", candidate.choice.day.isoformat(), slot_label, candidate.status)
                continue
            logging.info("Slot %s %s available; attempting click", candidate.choice.day.isoformat(), slot_label)
            if not click_time_candidate(page, config, candidate):
                continue
            attempt_status = attempt_checkout_for_current_slot(page, config, request, args)
            if attempt_status == "success":
                return candidate.choice
            if attempt_status == "booked_out":
                logging.info(
                    "Selected slot %s %s was booked out during checkout. Trying remaining options.",
                    candidate.choice.day.isoformat(),
                    slot_label,
                )
            recovery_state = return_to_slot_selection_page(page, config)
            if recovery_state == "slot":
                continue
            if recovery_state == "calendar":
                logging.info(
                    "Checkout recovery landed on the calendar page for %s; reopening that date without a full reload.",
                    candidate.choice.day.isoformat(),
                )
                if not open_slot_selection_for_date(page, config, candidate_day, request.seating_type):
                    break
                continue
            logging.info(
                "Could not restore the slot selection page for %s after checkout failure; reopening the date.",
                candidate.choice.day.isoformat(),
            )
            return_to_order_page(page, config)
            if not open_slot_selection_for_date(page, config, candidate_day, request.seating_type):
                break

        logging.info(
            "No eligible time slot led to a checkout success for %s seating on date %s; moving to next date",
            request.seating_type,
            candidate_day.isoformat(),
        )
        return_to_order_page(page, config)

    if unavailable:
        logging.info("No matching slot is bookable right now. Stopping without retry.")
    else:
        logging.info("Checked all requested dates and slots. No selectable slot matched; stopping without retry.")
    return None


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


def ensure_checkbox_checked(page: Page, selectors: tuple[str, ...], label: str) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=1200)
            try:
                if locator.is_checked(timeout=400):
                    logging.info("%s already checked via %s", label, selector)
                    return True
            except Error:
                pass

            locator.click(timeout=1200)

            try:
                if locator.is_checked(timeout=400):
                    logging.info("Checked %s using %s", label, selector)
                    return True
            except Error:
                logging.info("Clicked %s using %s", label, selector)
                return True
        except (PlaywrightTimeoutError, Error):
            continue
    return False


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
            if 0 <= value <= 20:
                logging.info("Detected current guest counter value: %s", value)
                return value
    return None


def adjust_guest_counter(page: Page, config: BookingConfig, request: ReservationRequest) -> None:
    current_count = current_guest_count(page, config)
    if current_count is None:
        logging.warning("Could not detect current guest counter value; assuming 0")
        current_count = 0

    if current_count == request.guest_count:
        logging.info("Guest counter already matches target: %s", request.guest_count)
        return

    if current_count < request.guest_count:
        diff = request.guest_count - current_count
        for index in range(diff):
            logging.info(
                "Adjusting guest counter toward %s: clicking + %s/%s",
                request.guest_count,
                index + 1,
                diff,
            )
            if not click_first_visible(page, config.guest_increment_selectors, "guest increment"):
                logging.warning("Guest increment control was not found")
                break
            page.wait_for_timeout(300)
        final_count = current_guest_count(page, config)
        if final_count is not None:
            logging.info("Guest counter after incrementing: %s", final_count)
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
    final_count = current_guest_count(page, config)
    if final_count is not None:
        logging.info("Guest counter after decrementing: %s", final_count)


def fill_guest_details(page: Page, config: BookingConfig, request: Optional[ReservationRequest]) -> None:
    if not request:
        return
    fill_first_visible(page, config.name_selectors, request.name, "name")
    fill_first_visible(page, config.email_selectors, request.email, "email")
    fill_first_visible(page, config.phone_selectors, request.phone, "tel")
    if not ensure_checkbox_checked(page, config.rules_checkbox_selectors, "house rules checkbox"):
        logging.warning("House rules checkbox was not found")


def maybe_submit(page: Page, config: BookingConfig, auto_submit: bool) -> bool:
    if not auto_submit:
        return False
    for selector in config.submit_selectors:
        if click_locator(page.locator(selector).first, f"submit button {selector}"):
            return True
    return False


def validate_request(args: argparse.Namespace) -> Optional[ReservationRequest]:
    name = args.name or os.environ.get("NARU_NAME")
    email = args.email or os.environ.get("NARU_EMAIL")
    phone = args.phone or os.environ.get("NARU_PHONE")
    upi_id = args.upi_id or os.environ.get("NARU_BOOKING_UPI") or os.environ.get("NARU_UPI_ID")
    guest_count_raw = args.guest_count or os.environ.get("NARU_GUEST_COUNT")
    seating_type = args.seating or os.environ.get("NARU_SEATING_TYPE") or "table"
    booking_days = args.booking_days or os.environ.get("NARU_BOOKING_DAYS") or "all"
    booking_times = parse_booking_times(args.booking_time or os.environ.get("NARU_BOOKING_TIME") or "all")

    missing_fields = []
    if not name:
        missing_fields.append("NARU_NAME/--name")
    if not email:
        missing_fields.append("NARU_EMAIL/--email")
    if not phone:
        missing_fields.append("NARU_PHONE/--phone")
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

    return ReservationRequest(
        name=name,
        email=email,
        phone=phone,
        upi_id=upi_id,
        guest_count=guest_count,
        seating_type=seating_type,
        booking_days=booking_days,
        booking_times=booking_times,
        auto_submit=args.auto_submit or bool(upi_id) or args.dry_run,
    )


def main() -> int:
    load_dotenv(ENV_FILE)
    args = parse_args()
    request = validate_request(args)
    config = BookingConfig()
    candidate_dates = filter_candidate_dates(
        candidate_booking_dates(datetime.now(IST)),
        request.booking_days,
    )

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    logging.info("Seating mode: %s", request.seating_type)
    logging.info("Dry run mode: %s", args.dry_run)
    logging.info("Email configured: %s", request.email)
    logging.info(
        "Booking day filter: %s | resolved dates: %s",
        request.booking_days,
        ", ".join(candidate_day.isoformat() for candidate_day in candidate_dates),
    )
    logging.info(
        "Booking time filter: %s",
        "all visible slots" if request.booking_times is None else ", ".join(request.booking_times),
    )
    if request.upi_id:
        logging.info("UPI ID configured: %s", mask_upi_id(request.upi_id))
    logging.info("Target guest count configured: %s", request.guest_count)

    context = launch_context(headless=args.headless)
    try:
        page = context.new_page()

        selected_slot = poll_until_target_bookable(
            page,
            config,
            candidate_dates=candidate_dates,
            request=request,
            args=args,
            poll_seconds=max(1, args.poll_seconds),
        )
        if not selected_slot:
            logging.info("No slot was selected. Browser paused for review.")
            page.pause()
            return 1
        logging.info(
            "Selected slot: %s",
            selected_slot.time_value.strftime("%Y-%m-%d %I:%M %p %Z"),
        )
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
