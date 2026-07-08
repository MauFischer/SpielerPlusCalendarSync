from pathlib import Path
import argparse
from icalendar import Calendar, Event
from datetime import datetime, timedelta, timezone

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    cal = Calendar()
    cal.add("prodid", "-//MySpielerPlusKalender//DE")
    cal.add("version", "2.0")

    event = Event()
    event.add("summary", "Testtermin")
    start = datetime.now(timezone.utc) + timedelta(days=1)
    end = start + timedelta(hours=1)
    event.add("dtstart", start)
    event.add("dtend", end)
    event.add("description", "Nur ein Test, damit der Workflow erstmal läuft.")
    cal.add_component(event)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("wb") as f:
        f.write(cal.to_ical())

if __name__ == "__main__":
    main()
