from __future__ import annotations

import html
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from icalendar import Calendar, Event
from zoneinfo import ZoneInfo

BASE_URL = "https://www.spielerplus.de"
LOGIN_PATH = "/de/site/login"
TEAM_SELECT_PATH = "/de/site/select-team"
EVENTS_PATH = "/de/events/calendar"

CALENDAR_EVENTS_RE = re.compile(r'"events"\s*:\s*(\[[\s\S]*?\])\s*[,}]', re.S)
URL_KIND_RE = re.compile(r"/(event|game|training|tournament)/view", re.I)
TITLE_DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")


@dataclass
class Membership:
    team_id: str
    team_name: str


@dataclass
class FeedItem:
    team_name: str
    event_id: str
    kind: str
    title: str
    url: str
    start: datetime | date | None
    end: datetime | date | None
    location: str = ""


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or not str(value).strip():
        raise RuntimeError(f"Missing required environment variable: {name}")
    return str(value).strip()


def optional_env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def get_selected_team_name() -> str | None:
    return optional_env("SPIELERPLUS_TEAM_NAME")


def get_calendar_team_name() -> str | None:
    return optional_env("CALENDAR_TEAM_NAME")


def local_tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("TIMEZONE", "Europe/Berlin"))


def tzify(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=local_tz())


def parse_iso_local(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def combine_date_time(d: date | None, hhmm: str | None) -> datetime | None:
    if d is None:
        return None
    if not hhmm:
        return datetime(d.year, d.month, d.day)
    m = re.match(r"(\d{1,2}):(\d{2})", hhmm.strip())
    if not m:
        return datetime(d.year, d.month, d.day)
    return datetime(d.year, d.month, d.day, int(m.group(1)), int(m.group(2)))


def clean_location(text: str) -> str:
    txt = text.replace("\xa0", " ").strip()
    txt = re.sub(r"^Adresse\s*", "", txt, flags=re.I).strip()
    if "keine adresse" in txt.lower():
        return ""
    return txt


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.5",
        }
    )
    return session


def login(session: requests.Session, email: str, password: str) -> None:
    login_url = urljoin(BASE_URL, LOGIN_PATH)
    resp = session.get(login_url, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    form = soup.find("form")
    if form is None:
        raise RuntimeError("Login form not found on SpielerPlus login page.")

    action = urljoin(BASE_URL, form.get("action") or LOGIN_PATH)
    data: dict[str, str] = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        typ = (inp.get("type") or "").lower()
        value = inp.get("value") or ""
        if typ in {"hidden", "submit"}:
            data[name] = value

    data.setdefault("_csrf", "")
    data["LoginForm[email]"] = email
    data["LoginForm[password]"] = password
    data["LoginForm[rememberMe]"] = "1"

    csrf_input = form.select_one('input[name="_csrf"]')
    if csrf_input and csrf_input.get("value"):
        data["_csrf"] = csrf_input["value"]
    else:
        meta = soup.select_one('meta[name="csrf-token"]')
        if meta and meta.get("content"):
            data["_csrf"] = meta["content"]

    if not data.get("_csrf"):
        data.pop("_csrf", None)

    post = session.post(
        action,
        data=data,
        headers={"Referer": login_url},
        timeout=30,
        allow_redirects=True,
    )
    post.raise_for_status()

    if post.url.rstrip("/").endswith("/site/login"):
        raise RuntimeError(
            "Login failed. Please check SPIELERPLUS_USERNAME and SPIELERPLUS_PASSWORD."
        )

    cookie_names = {c.name for c in session.cookies}
    if not any(name in cookie_names for name in ("_identity", "_identity-spielerplus", "PHPSESSID")):
        raise RuntimeError("Login did not yield a usable SpielerPlus session cookie.")


def discover_memberships(session: requests.Session) -> list[Membership]:
    url = urljoin(BASE_URL, TEAM_SELECT_PATH)
    resp = session.get(url, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    memberships: list[Membership] = []
    for item in soup.select(".select-team-item"):
        a = item.select_one('a[href*="switch-user?id="]')
        if not a:
            continue
        href = a.get("href") or ""
        m = re.search(r"switch-user\?id=(\d+)", href)
        if not m:
            continue
        team_id = m.group(1)
        name_el = item.select_one(".select-team-item-meta h4") or item.select_one("h4")
        team_name = name_el.get_text(" ", strip=True) if name_el else team_id
        memberships.append(Membership(team_id=team_id, team_name=team_name))
    return memberships


def switch_user(session: requests.Session, team_id: str) -> None:
    url = urljoin(BASE_URL, f"/de/site/switch-user?id={team_id}")
    resp = session.get(url, timeout=30)
    resp.raise_for_status()


def calendar_page_html(session: requests.Session) -> str:
    url = urljoin(BASE_URL, EVENTS_PATH)
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text


def detail_page_html(session: requests.Session, kind: str, event_id: str) -> str:
    url = urljoin(BASE_URL, f"/de/{kind}/view?id={event_id}")
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse_calendar_events(html_text: str) -> list[dict[str, Any]]:
    match = CALENDAR_EVENTS_RE.search(html_text)
    if not match:
        return []

    raw = json.loads(match.group(1))
    items: list[dict[str, Any]] = []
    for item in raw:
        url = item.get("url") or ""
        if not URL_KIND_RE.search(url):
            continue
        kind_match = URL_KIND_RE.search(url)
        if not kind_match:
            continue

        start = parse_iso_local(item.get("start"))
        end = parse_iso_local(item.get("end"))
        if start and end:
            end = normalize_end(start, end)

        items.append(
            {
                "id": str(item.get("id", "")),
                "kind": kind_match.group(1).lower(),
                "title": str(item.get("title", "")).strip(),
                "start": start,
                "end": end,
                "url": url,
            }
        )
    return items


def normalize_end(start: datetime, end: datetime) -> datetime:
    fixed = start.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if fixed <= start:
        fixed += timedelta(days=1)
    return fixed


def parse_detail_page(
    html_text: str,
    fallback_start: datetime | None,
    fallback_end: datetime | None,
) -> tuple[datetime | None, datetime | None, str, bool, bool]:
    soup = BeautifulSoup(html_text, "html.parser")

    title_tag = soup.find("title")
    event_date: date | None = None
    if title_tag:
        m = TITLE_DATE_RE.search(title_tag.get_text(" ", strip=True))
        if m:
            event_date = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))

    time_values: dict[str, str] = {}
    for item in soup.select(".event-time-item"):
        label = item.select_one(".event-time-label")
        value = item.select_one(".event-time-value")
        if label and value:
            time_values[label.get_text(strip=True)] = value.get_text(strip=True)

    start = fallback_start
    end = fallback_end

    if event_date:
        treff = None
        beginn = None
        ende = None

        if "Treffen" in time_values:
            treff = combine_date_time(event_date, time_values.get("Treffen"))

        if "Beginn" in time_values:
            beginn = combine_date_time(event_date, time_values.get("Beginn"))

        if "Ende" in time_values:
            ende = combine_date_time(event_date, time_values.get("Ende"))

        # Start: Treffzeit, sonst Beginnzeit
        start = treff or beginn or fallback_start

        # Ende: nur auf Basis von Beginnzeit, nicht Treffzeit
        if ende is not None:
            end = ende
        elif beginn is not None:
            end = beginn + timedelta(hours=2)
        else:
            end = fallback_end

    if start and end and end <= start:
        end = end + timedelta(days=1)

    if start is None and event_date is not None:
        start = datetime(event_date.year, event_date.month, event_date.day)

    if end is None and start is not None:
        end = start + timedelta(hours=2)

    location_el = soup.select_one(".info-area-content small") or soup.select_one(".info-area-content")
    location = ""
    if location_el:
        location = clean_location(location_el.get_text(" ", strip=True))

    page_text = soup.get_text(" ", strip=True).lower()
    is_home = "heimspiel" in page_text
    is_away = "auswärtsspiel" in page_text or "auswaertsspiel" in page_text

    return start, end, location, is_home, is_away


def clean_title(text: str) -> str:
    cleaned = html.unescape(text or "")
    cleaned = cleaned.replace("\xa0", " ")
    cleaned = re.sub(r"\s*\|\s*.*$", "", cleaned)
    cleaned = re.sub(r"\s*[–—:-]+\s*.*$", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" -–—:|")


def build_summary(kind: str, title: str, is_home: bool, is_away: bool) -> str:
    kind = (kind or "").lower().strip()
    base_title = clean_title(title)

    if kind == "game":
        base_title = re.sub(r"(?i)^\s*heimspiel\s+", "", base_title).strip(" -–—:|")
        base_title = re.sub(r"(?i)^\s*ausw(?:ä|ae)rtsspiel\s+", "", base_title).strip(" -–—:|")
        base_title = re.sub(r"(?i)^\s*ausw(?:ä|ae)rt\s+", "", base_title).strip(" -–—:|")

        if is_home:
            return f"Heimspiel {base_title}".strip()
        if is_away:
            return f"Auswärtsspiel {base_title}".strip()

    return base_title


def build_ics(items: list[FeedItem], calendar_name: str) -> bytes:
    cal = Calendar()
    cal.add("prodid", f"-//{calendar_name}//DE")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", calendar_name)

    for item in sorted(
        items,
        key=lambda x: (x.start or datetime.max.replace(tzinfo=local_tz()), x.team_name, x.title),
    ):
        evt = Event()
        evt.add("uid", f"spielerplus-{item.team_name}-{item.event_id}@local")
        evt.add("summary", f"{item.title} | {item.team_name}".strip())

        if isinstance(item.start, datetime):
            evt.add("dtstart", tzify(item.start))
        elif isinstance(item.start, date):
            evt.add("dtstart", item.start)

        if isinstance(item.end, datetime):
            evt.add("dtend", tzify(item.end))
        elif isinstance(item.end, date):
            evt.add("dtend", item.end)

        if item.location:
            evt.add("location", item.location)

        description_parts = [f"SpielerPlus: {item.url}"]
        if item.location:
            description_parts.append(f"Ort: {item.location}")
        evt.add("description", "\n".join(description_parts))
        evt.add("url", item.url)
        evt.add("dtstamp", datetime.now(tz=local_tz()))
        cal.add_component(evt)

    return cal.to_ical()


def main() -> None:
    username = env("SPIELERPLUS_USERNAME")
    password = env("SPIELERPLUS_PASSWORD")
    output_filename = env("CALENDAR_FILENAME")
    calendar_name = os.environ.get("CALENDAR_NAME", "SpielerPlus Calendar").strip() or "SpielerPlus Calendar"

    selected_team_name = get_selected_team_name()
    calendar_team_name = get_calendar_team_name()

    session = make_session()
    login(session, username, password)

    memberships = discover_memberships(session)
    if not memberships:
        memberships = [Membership(team_id="", team_name="SpielerPlus")]

    if selected_team_name:
        needle = selected_team_name.strip().lower()
        memberships = [
            m for m in memberships
            if needle in m.team_name.strip().lower()
        ]

    if not memberships:
        if selected_team_name:
            raise RuntimeError(
                f'Kein Team gefunden, das "{selected_team_name}" enthält.'
            )
        raise RuntimeError("Keine Teams gefunden.")

    feed_items: list[FeedItem] = []
    for membership in memberships:
        if membership.team_id:
            switch_user(session, membership.team_id)

        page = calendar_page_html(session)
        events = parse_calendar_events(page)

        for ev in events:
            if not ev["id"]:
                continue

            detail_html = detail_page_html(session, ev["kind"], ev["id"])
            start, end, location, is_home, is_away = parse_detail_page(
                detail_html,
                fallback_start=ev["start"],
                fallback_end=ev["end"],
            )

            summary = build_summary(ev["kind"], ev["title"], is_home, is_away)

            feed_items.append(
                FeedItem(
                    team_name=calendar_team_name or membership.team_name or "SpielerPlus",
                    event_id=ev["id"],
                    kind=ev["kind"],
                    title=summary,
                    url=urljoin(BASE_URL, ev["url"]),
                    start=start,
                    end=end,
                    location=location,
                )
            )

    output_path = Path("public") / output_filename
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(build_ics(feed_items, calendar_name))
    print(f"Wrote {output_path} with {len(feed_items)} events.")


if __name__ == "__main__":
    main()
