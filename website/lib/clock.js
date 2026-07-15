// Club clock for the static site — the JS mirror of agent/clock.py.
//
// The club operates in one timezone (US Central) and meeting dates/times are stored LOCAL, so
// "today" and "is this meeting upcoming?" must be judged in America/Chicago — not UTC (which flips
// a day early in the evening) and not the build machine's local zone. A meeting is upcoming until
// its start + a buffer (≈ when it wraps) has passed; there is no placeholder flag.

const TZ = "America/Chicago";
const DEFAULT_MEETING_HOUR = 18; // evening; used when a meeting has no explicit start time
const ROLL_BUFFER_MIN = 3 * 60; // a meeting stays "upcoming" this long after it starts

// Wall-clock parts of `date` in the club timezone.
function centralParts(date = new Date()) {
  const fmt = new Intl.DateTimeFormat("en-US", {
    timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  });
  const p = {};
  for (const { type, value } of fmt.formatToParts(date)) p[type] = value;
  return {
    y: +p.year, m: +p.month, d: +p.day,
    H: +(p.hour === "24" ? "0" : p.hour), M: +p.minute,
  };
}

const pad = (n) => String(n).padStart(2, "0");

// Today's date in the club timezone as 'YYYY-MM-DD'.
function centralToday(now = new Date()) {
  const { y, m, d } = centralParts(now);
  return `${y}-${pad(m)}-${pad(d)}`;
}

function addDaysIso(iso, days) {
  const [y, m, d] = iso.split("-").map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d + days));
  return `${dt.getUTCFullYear()}-${pad(dt.getUTCMonth() + 1)}-${pad(dt.getUTCDate())}`;
}

// True if the meeting has not yet passed — now (Central wall clock) is before start + buffer.
// Both sides are compared as Central wall-clock 'YYYY-MM-DDTHH:MM' strings (lexicographic).
function isUpcoming(meetingDate, startTime, now = new Date()) {
  if (!meetingDate) return false;
  const day = String(meetingDate).slice(0, 10);
  let hh = DEFAULT_MEETING_HOUR, mm = 0;
  if (startTime && /^\d{2}:\d{2}/.test(startTime)) {
    hh = Number(startTime.slice(0, 2));
    mm = Number(startTime.slice(3, 5));
  }
  let mins = hh * 60 + mm + ROLL_BUFFER_MIN;
  let endDay = day;
  while (mins >= 24 * 60) { mins -= 24 * 60; endDay = addDaysIso(endDay, 1); }
  const endKey = `${endDay}T${pad(Math.floor(mins / 60))}:${pad(mins % 60)}`;
  const { y, m, d, H, M } = centralParts(now);
  const nowKey = `${y}-${pad(m)}-${pad(d)}T${pad(H)}:${pad(M)}`;
  return nowKey < endKey;
}

module.exports = { TZ, centralToday, isUpcoming };
