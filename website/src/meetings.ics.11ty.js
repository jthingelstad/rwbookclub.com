// iCalendar (RFC 5545) feed of every club meeting — past and future — published at
// /meetings.ics so members can subscribe and see each meeting (book, picker, host) on their
// own calendar. Built from the meetings/books/members globals; no network, no new data files.
//
// Meetings store the correct LOCAL date + time (America/Chicago), so timed events are tagged
// TZID=America/Chicago and a VTIMEZONE block makes them DST-correct in every client.

const PRODID = "-//R/W Book Club//Meetings//EN";
const DEFAULT_DURATION_MIN = 120; // meetings don't store an end time

// Standard America/Chicago (US Central) definition, post-2007 DST rules.
const VTIMEZONE = [
  "BEGIN:VTIMEZONE",
  "TZID:America/Chicago",
  "X-LIC-LOCATION:America/Chicago",
  "BEGIN:DAYLIGHT",
  "TZOFFSETFROM:-0600",
  "TZOFFSETTO:-0500",
  "TZNAME:CDT",
  "DTSTART:19700308T020000",
  "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU",
  "END:DAYLIGHT",
  "BEGIN:STANDARD",
  "TZOFFSETFROM:-0500",
  "TZOFFSETTO:-0600",
  "TZNAME:CST",
  "DTSTART:19701101T020000",
  "RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU",
  "END:STANDARD",
  "END:VTIMEZONE",
];

const pad = (n) => String(n).padStart(2, "0");
const dateCompact = (date) => date.replace(/-/g, "");             // "2026-06-30" → "20260630"
const timeCompact = (t) => { const [h, m] = t.split(":"); return `${pad(h)}${pad(m)}00`; };

// Escape a TEXT value per RFC 5545 (order matters: backslash first).
function escapeText(s) {
  return String(s == null ? "" : s)
    .replace(/\\/g, "\\\\")
    .replace(/;/g, "\\;")
    .replace(/,/g, "\\,")
    .replace(/\r?\n/g, "\\n");
}

// Fold a content line to ≤75 octets; continuation lines begin with a single space. Careful not
// to split a multi-byte UTF-8 codepoint (e.g. the « » guillemets).
function fold(line) {
  const bytes = Buffer.from(line, "utf8");
  if (bytes.length <= 75) return line;
  const parts = [];
  let start = 0;
  let limit = 75;
  while (start < bytes.length) {
    let end = Math.min(start + limit, bytes.length);
    while (end < bytes.length && (bytes[end] & 0xc0) === 0x80) end--; // back off mid-codepoint
    parts.push(bytes.slice(start, end).toString("utf8"));
    start = end;
    limit = 74; // continuation lines carry a leading space → one fewer content byte
  }
  return parts.join("\r\n ");
}

function joinNames(list) {
  const a = (list || []).filter(Boolean);
  if (a.length <= 1) return a[0] || "";
  if (a.length === 2) return `${a[0]} and ${a[1]}`;
  return `${a.slice(0, -1).join(", ")}, and ${a[a.length - 1]}`;
}

// Add wall-clock minutes to a local date+time (DST is applied by the client via TZID, so this
// is plain wall-clock arithmetic; evening meetings never cross the 2am DST boundary).
function addMinutesLocal(date, time, minutes) {
  const [y, mo, d] = date.split("-").map(Number);
  const [h, mi] = time.split(":").map(Number);
  const dt = new Date(Date.UTC(y, mo - 1, d, h, mi + minutes));
  return `${dt.getUTCFullYear()}${pad(dt.getUTCMonth() + 1)}${pad(dt.getUTCDate())}`
    + `T${pad(dt.getUTCHours())}${pad(dt.getUTCMinutes())}00`;
}

function nextDayCompact(date) {
  const [y, mo, d] = date.split("-").map(Number);
  const dt = new Date(Date.UTC(y, mo - 1, d + 1));
  return `${dt.getUTCFullYear()}${pad(dt.getUTCMonth() + 1)}${pad(dt.getUTCDate())}`;
}

function eventLines(m, bookBySlug, nameBySlug, baseUrl) {
  const mbooks = (m.books || []).map((s) => bookBySlug.get(s)).filter(Boolean);
  const titles = (m.bookRefs && m.bookRefs.length)
    ? m.bookRefs.map((r) => r.title)
    : mbooks.map((b) => b.title);

  let summary;
  if (titles.length) {
    summary = "Book Club: " + titles.map((t) => `«${t}»`).join(" & ");
  } else {
    const types = (m.nonBookTypes && m.nonBookTypes.length) ? m.nonBookTypes : (m.types || []);
    summary = types.length ? `Book Club: ${types.join(", ")}` : "Book Club";
  }

  const desc = [];
  for (const b of mbooks) {
    const authors = joinNames(b.authors);
    desc.push(authors ? `«${b.title}» by ${authors}` : `«${b.title}»`);
  }
  const pickers = [...new Set(mbooks.flatMap(
    (b) => b.pickerNames || (b.pickerName ? [b.pickerName] : [])))];
  if (pickers.length) desc.push(`Picked by ${joinNames(pickers)}.`);
  const hosts = (m.host || []).map((s) => nameBySlug.get(s)).filter(Boolean);
  if (hosts.length) desc.push(`Hosted by ${joinNames(hosts)}.`);
  const topics = [...new Set(mbooks.map((b) => b.topic).filter(Boolean))];
  if (topics.length) desc.push(`Topic: ${topics.join(", ")}.`);
  if (m.notes) desc.push(m.notes);
  for (const b of mbooks) desc.push(`${baseUrl}/books/${b.slug}/`);

  const out = [
    "BEGIN:VEVENT",
    `UID:meeting-${m.meetingId}@rwbookclub.com`,
    `DTSTAMP:${dateCompact(m.date)}T000000Z`, // stable (meeting date) → no per-build churn
  ];
  if (m.startTime) {
    out.push(`DTSTART;TZID=America/Chicago:${dateCompact(m.date)}T${timeCompact(m.startTime)}`);
    out.push(`DTEND;TZID=America/Chicago:${addMinutesLocal(m.date, m.startTime, DEFAULT_DURATION_MIN)}`);
  } else {
    out.push(`DTSTART;VALUE=DATE:${dateCompact(m.date)}`);
    out.push(`DTEND;VALUE=DATE:${nextDayCompact(m.date)}`);
  }
  out.push(`SUMMARY:${escapeText(summary)}`);
  if (desc.length) out.push(`DESCRIPTION:${escapeText(desc.join("\n"))}`);
  if (m.location) out.push(`LOCATION:${escapeText(m.location)}`);
  if (mbooks.length) out.push(`URL:${baseUrl}/books/${mbooks[0].slug}/`);
  out.push("END:VEVENT");
  return out;
}

module.exports = class {
  data() {
    return { permalink: "/meetings.ics", eleventyExcludeFromCollections: true };
  }

  render({ meetings = [], books = [], members = [], site = {} }) {
    const baseUrl = (site.url || "").replace(/\/$/, "");
    const bookBySlug = new Map(books.map((b) => [b.slug, b]));
    const nameBySlug = new Map(members.map((m) => [m.slug, m.name]));

    const lines = [
      "BEGIN:VCALENDAR",
      "VERSION:2.0",
      `PRODID:${PRODID}`,
      "CALSCALE:GREGORIAN",
      "METHOD:PUBLISH",
      "X-WR-CALNAME:R/W Book Club",
      "X-WR-TIMEZONE:America/Chicago",
      ...VTIMEZONE,
    ];
    for (const m of meetings) {
      if (!m.date) continue; // can't place an undated meeting on a calendar
      lines.push(...eventLines(m, bookBySlug, nameBySlug, baseUrl));
    }
    lines.push("END:VCALENDAR");

    return lines.map(fold).join("\r\n") + "\r\n";
  }
};
